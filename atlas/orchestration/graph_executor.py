"""
atlas.orchestration.graph_executor
=======================================
ATLAS EXPANSION 1 — ORQUESTRAÇÃO EXECUTÁVEL.

Executor REAL de grafos + loops + paralelismo — construído SOBRE a
infraestrutura já existente e aprovada na V1, nunca duplicando nada
dela:

- `atlas.advanced_planning.AdvancedPlan.parallel_groups()` — já
  calcula os "níveis" de paralelismo (topological sort + agrupamento
  por nível), já rejeita ciclo/dependência desconhecida
  (`CyclicDependencyError`/`UnknownDependencyError`). Este módulo
  apenas CONSOME esse resultado — nunca reimplementa ordenação
  topológica.
- `core.coordination.mission.MissionTracker` — já valida dependências
  reais antes de `start_running()`, já tem a state machine completa
  (QUEUED→...→VERIFIED/FAILED/CANCELLED) com transições condicionadas
  a versão (nunca uma atualização obsoleta corrompe outra). Este
  módulo usa essa state machine para rastrear cada tarefa — nunca
  inventa um rastreamento paralelo.
- `core.jobs.queue.JobQueue` — já tem enqueue idempotente, `start()`
  atômico com controle de capacidade real entre instâncias, retry via
  `attempts`/`max_attempts`, circuit breaker. Este módulo delega a
  CADA tarefa individual para um `Job` — nunca reimplementa retry ou
  idempotência.
- `atlas.escalation.EscalationDesk` — já tem o fluxo completo de Owner
  Approval (fail-closed, nunca autoaprovado). Este módulo escalona
  tarefas críticas por ele — nunca implementa uma segunda aprovação.

O que este módulo ADICIONA de fato novo (a lacuna real que faltava):
um EXECUTOR que efetivamente RODA o plano — despacha cada "nível" de
`parallel_groups()` em paralelo real (`ThreadPoolExecutor`), bloqueia
o nível seguinte até o anterior terminar, aplica timeout real por
tarefa, suporta cancelamento cooperativo, e dá suporte a LOOPS com
condição de parada explícita + limite de iterações + fail-closed.

Escopo desta entrega (ATLAS EXPANSION 1): SOMENTE o motor de
orquestração. `task_runner`/`quality_gate_checker`/
`requires_owner_approval` são sempre fornecidos pelo CHAMADOR — este
módulo nunca sabe nada sobre coleta de redes sociais nem produção de
vídeo (fora de escopo desta entrega, entra nos blocos seguintes).
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from atlas.advanced_planning import AdvancedPlan, PlanTask
from atlas.escalation import ApprovalStatus, EscalationDesk
from core.coordination.mission import (
    DependencyNotSatisfied, InvalidMissionTransition, MissionAssignment, MissionNotFound, MissionStatus,
    MissionTracker,
)
from core.jobs.queue import InvalidJobTransition, JobQueue
from core.models.entities import JobState

_logger = logging.getLogger(__name__)

__all__ = [
    "GraphExecutor", "LoopLimitExceeded", "LoopSpec", "OwnerApprovalPending", "PlanCancelled",
    "PlanExecutionReport", "TaskExecutionResult", "run_loop",
]


class PlanCancelled(Exception):
    """A execução foi cancelada explicitamente antes de terminar —
    grupos/tarefas restantes nunca são iniciados após a detecção do
    cancelamento."""


class LoopLimitExceeded(Exception):
    """MICROCORREÇÃO (loops fail-closed): o limite de iterações foi
    atingido SEM a condição de parada ter sido satisfeita — nunca
    reportado como sucesso silencioso."""


class OwnerApprovalPending(Exception):
    """Levantado internamente quando uma tarefa crítica precisa de
    aprovação do Owner e nenhuma aprovação já concedida foi informada
    — a missão fica em `WAITING_OWNER`, nunca executa sem aprovação
    real."""


# ---------------------------------------------------------------------
# Loops — condição de parada explícita + limite de iterações + fail-closed
# ---------------------------------------------------------------------

@dataclass
class LoopSpec:
    """Especificação de um loop controlado. `condition(iteration)` é
    checada ANTES de cada execução do corpo — quando devolve `True`,
    o loop para com sucesso (convergiu). `max_iterations` é um limite
    DURO — nunca um loop sem teto. `body(iteration)` é o efeito real
    de uma iteração."""

    condition: Callable[[int], bool]
    body: Callable[[int], None]
    max_iterations: int

    def __post_init__(self) -> None:
        if self.max_iterations <= 0:
            raise ValueError("max_iterations precisa ser positivo — loop sem limite nunca é permitido")


def run_loop(spec: LoopSpec, *, cancel_event: threading.Event | None = None) -> int:
    """Executa `spec.body` repetidamente até `spec.condition` devolver
    `True`, respeitando `spec.max_iterations` como teto absoluto.

    FAIL-CLOSED: se o limite de iterações for atingido sem a condição
    nunca ter sido satisfeita, levanta `LoopLimitExceeded` — NUNCA
    devolve como se tivesse convergido. Cancelamento cooperativo via
    `cancel_event` — checado antes de cada iteração, levanta
    `PlanCancelled` imediatamente (o corpo em andamento, se houver,
    termina normalmente; nenhuma NOVA iteração começa).

    Devolve o número de iterações REALMENTE executadas até convergir."""
    cancel_event = cancel_event or threading.Event()
    for i in range(spec.max_iterations):
        if cancel_event.is_set():
            raise PlanCancelled(f"loop cancelado antes da iteração {i}")
        if spec.condition(i):
            return i
        spec.body(i)
    if spec.condition(spec.max_iterations):
        return spec.max_iterations
    raise LoopLimitExceeded(
        f"limite de {spec.max_iterations} iterações atingido sem a condição de parada ser satisfeita — "
        f"fail-closed, nunca reportado como convergência"
    )


# ---------------------------------------------------------------------
# Execução de plano (DAG real via parallel_groups())
# ---------------------------------------------------------------------

@dataclass
class TaskExecutionResult:
    task_id: str
    mission_id: str
    status: str  # "VERIFIED" | "FAILED" | "CANCELLED" | "WAITING_OWNER" | "SKIPPED_ALREADY_DONE"
    attempts: int = 0
    duration_seconds: float = 0.0
    result: dict | None = None
    error: str | None = None
    escalation_id: str | None = None


@dataclass
class PlanExecutionReport:
    plan_id: str
    mission_id: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    task_results: list = field(default_factory=list)
    group_durations: list = field(default_factory=list)
    cancelled: bool = False
    failed: bool = False

    @property
    def success(self) -> bool:
        return not self.cancelled and not self.failed and bool(self.task_results)

    def result_for(self, task_id: str) -> TaskExecutionResult | None:
        for r in self.task_results:
            if r.task_id == task_id:
                return r
        return None


# ------------------------------------------------------------
# Prova de liveness do detentor no Windows (FECHAMENTO 1/6).
# Isolada em funções de módulo para ser testável em qualquer SO
# com um kernel32 simulado (as decisões dependem só dos códigos).
# ------------------------------------------------------------
_IS_WINDOWS = os.name == "nt"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_win_kernel32_cache: tuple | None = None


def _win_kernel32() -> tuple:
    """(kernel32, get_last_error) com `use_last_error=True` e
    assinaturas declaradas — instância PRÓPRIA (nunca altera o
    `ctypes.windll.kernel32` compartilhado do processo)."""
    global _win_kernel32_cache
    if _win_kernel32_cache is None:
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        _ft = ctypes.POINTER(_FILETIME)
        k32.GetProcessTimes.argtypes = (wintypes.HANDLE, _ft, _ft, _ft, _ft)
        k32.GetProcessTimes.restype = wintypes.BOOL
        _win_kernel32_cache = (k32, ctypes.get_last_error)
    return _win_kernel32_cache


def _win_process_alive(pid: int, kernel32, get_last_error) -> bool:
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED => o processo EXISTE (consulta negada) =>
        # VIVO. Qualquer outro erro (tipicamente ERROR_INVALID_PARAMETER,
        # PID inexistente) => MORTO, preservando a recuperação de crash.
        return get_last_error() == _ERROR_ACCESS_DENIED
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        # Limitação conhecida e SEGURA: processo que terminou com código
        # 259 é lido como vivo (nunca converte detentor vivo).
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


# ------------------------------------------------------------
# Identidade temporal do processo detentor (FECHAMENTO 1/6 — PID
# reutilizado). Um PID sozinho não identifica um processo: após a
# morte do detentor, o SO pode entregar o MESMO PID a outro processo
# (em contêiner, o app reiniciado recebe tipicamente o mesmo PID).
# A identidade é o instante de criação do processo no relógio do SO,
# serializado como string OPACA e comparado por IGUALDADE EXATA:
# - Linux: `linux:<boot_id>:<starttime>` — `starttime` em ticks desde
#   o boot (campo 22 de /proc/<pid>/stat). Deliberadamente NÃO é hora
#   de parede: `btime` muda quando o relógio é ajustado, o que faria um
#   processo VIVO parecer outro (violação de P1). `boot_id` distingue
#   reinicializações da máquina.
# - Windows: `nt:<FILETIME de criação>` via `GetProcessTimes`
#   (100 ns, fixado na criação).
# - Outros POSIX (sem /proc): None — sem suporte; a prova volta a ser
#   só por PID (comportamento anterior).
# ------------------------------------------------------------
class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


_PROC_ROOT = "/proc"


def _linux_process_start_identity(pid: int, proc_root: str | None = None) -> str | None:
    root = proc_root or _PROC_ROOT
    try:
        with open(f"{root}/{pid}/stat", "rb") as f:
            data = f.read()
        with open(f"{root}/sys/kernel/random/boot_id", encoding="ascii") as f:
            boot_id = f.read().strip()
    except OSError:
        return None
    try:
        # o campo 2 (comm) vem entre parênteses e pode conter espaços e
        # ')' — os campos seguintes começam após o ÚLTIMO ')'.
        fields = data[data.rindex(b")") + 2:].split()
        starttime = int(fields[19])  # campo 22 do stat (índice 19 aqui)
    except (ValueError, IndexError):
        return None
    if not boot_id:
        return None
    return f"linux:{boot_id}:{starttime}"


def _win_process_start_identity(pid: int, kernel32) -> str | None:
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exited, kernel, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return f"nt:{value}" if value else None
    finally:
        kernel32.CloseHandle(handle)


def _process_start_identity(pid: int) -> str | None:
    """Identidade temporal do processo `pid` agora, ou None se não
    puder ser lida (processo sumiu, acesso negado, plataforma sem
    suporte). None NUNCA é tratado como prova de troca de processo."""
    if pid is None or pid <= 0:
        return None
    if _IS_WINDOWS:
        try:
            kernel32, _ = _win_kernel32()
            return _win_process_start_identity(pid, kernel32)
        except OSError:
            return None
    return _linux_process_start_identity(pid)


_self_identity_cache: dict = {}


def _current_process_start_identity() -> str | None:
    """Identidade do PRÓPRIO processo, gravada como detentor. Cache por
    PID (um filho de `fork` tem PID novo e recalcula).

    CORREÇÃO (fechamento 1/6): SOMENTE identidades obtidas com sucesso
    são guardadas. Antes, `None` também era guardado — uma única falha
    transitória na primeira leitura (ex.: EMFILE/EINTR ao abrir
    /proc/<pid>/stat) desligava a defesa contra PID reutilizado pelo
    resto da vida do processo, em silêncio (toda aquisição passava a
    gravar NULL, a semântica do baseline). Agora a leitura é repetida na
    próxima aquisição."""
    pid = os.getpid()
    cached = _self_identity_cache.get(pid)
    if cached is not None:
        return cached
    identity = _process_start_identity(pid)
    if identity is not None:
        _self_identity_cache[pid] = identity
    return identity


class GraphExecutor:
    """Motor real de execução de `AdvancedPlan` — despacha cada nível
    de `parallel_groups()` em paralelo real, bloqueia o nível seguinte
    até o anterior terminar (ou falhar — fail-closed, nunca avança
    sobre uma falha não tratada), com timeout real por tarefa,
    cancelamento cooperativo, retry via `JobQueue`, e Owner Approval
    real via `EscalationDesk` para tarefas marcadas como críticas pelo
    chamador.

    NUNCA duplica: delega rastreamento de estado a `MissionTracker`,
    retry/idempotência/capacidade a `JobQueue`, aprovação a
    `EscalationDesk`, ordenação/paralelismo estrutural a
    `AdvancedPlan.parallel_groups()`.

    MICROCORREÇÃO (proteção multi-instância real — falha real
    reproduzida por auditoria independente): a exclusão de execução
    por `(mission_id, task_id)` — "alguém já está rodando/rodou isto,
    nunca inicie de novo" — é agora um LEASE real:
    - Com `db_path`: um lease PERSISTENTE em SQLite
      (`graph_executor_task_leases`), adquirido por `INSERT` atômico
      com `PRIMARY KEY (mission_id, task_id)` — sobrevive a restart,
      visível para QUALQUER instância apontando para o mesmo banco.
    - Sem `db_path`: um lease COMPARTILHADO entre instâncias no MESMO
      processo (registro de classe, nunca de instância — a falha
      original era exatamente o registro ser local à instância),
      chaveado também pela identidade do `JobQueue` para nunca
      confundir mundos em memória genuinamente diferentes.

    Em nenhum dos dois casos uma segunda execução da MESMA
    `(mission_id, task_id)` é permitida enquanto a primeira não tiver
    um desfecho CONFIRMADO — nunca por suposição de tempo decorrido.

    CORREÇÃO P1 (invariante dura de liveness): com `db_path`, a
    conversão de um `IN_PROGRESS` expirado para `UNKNOWN_OUTCOME`
    exige PROVA de que o processo detentor (`holder_pid`, gravado na
    aquisição) não existe mais — verificada via SO. Frescor de
    `leased_at`/`holder_seen_at` sozinho NUNCA converte um detentor
    vivo: um heartbeat atrasado, lento ou até falhando não produz
    conversão enquanto o processo existir.

    INVARIANTE ARQUITETURAL — SINGLE-HOST (FECHAMENTO 1/6): o modo
    durável é suportado SOMENTE com todas as instâncias no MESMO host.
    Não é uma escolha desta camada: o banco é um arquivo SQLite aberto
    em modo WAL (`core.models.db.get_connection`), e WAL exige memória
    compartilhada (`-shm`) entre os processos — não funciona sobre
    sistema de arquivos de rede (restrição documentada do SQLite). Por
    isso a identidade LOCAL do detentor — `holder_pid` + `holder_proc_start`
    (instante de criação do processo) — é suficiente.
    Multi-host exigiria outro backend de lease e outra identidade de
    detentor (ex.: host + instante de criação do processo); não é
    suportado. Guardada por `tests/test_fechamento_1_6_holder_liveness.py`."""

    # Lease compartilhado EM MEMÓRIA — nível de CLASSE (nunca de
    # instância), para que múltiplas instâncias de `GraphExecutor` no
    # mesmo processo, apontando para o mesmo `JobQueue`, vejam o
    # MESMO estado de exclusão — essa era exatamente a lacuna da
    # versão anterior (`_inflight` era `self.`, não compartilhado).
    _memory_leases_lock = threading.Lock()
    _memory_leases: dict = {}

    def __init__(
        self, *, mission_tracker: MissionTracker, job_queue: JobQueue, escalation_desk: EscalationDesk | None = None,
        max_workers: int = 4, db_path: str | None = None, gate_in_progress_ttl_seconds: float = 60.0,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers precisa ser positivo")
        if gate_in_progress_ttl_seconds <= 0:
            raise ValueError("gate_in_progress_ttl_seconds precisa ser positivo")
        self.mission_tracker = mission_tracker
        self.job_queue = job_queue
        self.escalation_desk = escalation_desk
        self.max_workers = max_workers
        self.db_path = db_path
        # MICROCORREÇÃO (ATLAS EXPANSION 1 — recuperação de crash real
        # em GATE_IN_PROGRESS — falha real reproduzida por auditoria
        # independente, comprovada com um SUBPROCESSO real morto via
        # `os._exit()`): sem TTL, um `GATE_IN_PROGRESS` cujo detentor
        # morre abruptamente (processo/máquina, não apenas uma exceção
        # tratável) fica órfão PARA SEMPRE — nenhuma instância futura,
        # por mais saudável que seja, conseguia reivindicar de novo.
        # `leased_at` (já existente) agora participa da detecção de
        # expiração — passado esse prazo sem resolução, um
        # `GATE_IN_PROGRESS` é elegível para reclaim atômico (compare-
        # and-set condicionado ao MESMO `leased_at` lido, nunca
        # assumindo que nada mudou nesse meio-tempo).
        self.gate_in_progress_ttl_seconds = gate_in_progress_ttl_seconds
        # Rastreamento LOCAL (por instância) apenas das threads que
        # ESTA instância especificamente iniciou — usado só para
        # `wait_for_inflight_completion()` (coordenação de teste/
        # shutdown determinística). NUNCA a fonte da decisão de
        # exclusão — essa vem sempre do lease (memória compartilhada
        # de classe, ou SQLite persistente).
        self._local_threads_lock = threading.Lock()
        self._local_threads: dict = {}

    def wait_for_inflight_completion(self, *, timeout: float = 10.0) -> int:
        """Aguarda (de forma limitada) toda thread que ESTA instância
        iniciou e cujo desfecho ainda era desconhecido terminar de
        verdade. NUNCA chamado automaticamente por `execute_plan()`;
        existe para testes e para coordenação explícita de shutdown
        determinística (nunca depender de `time.sleep()` arbitrário).
        Devolve quantas ainda restaram vivas ao final do prazo (0 =
        tudo limpo)."""
        deadline = time.time() + timeout
        with self._local_threads_lock:
            entries = list(self._local_threads.items())
        still_alive = 0
        for key, thread in entries:
            remaining = max(0.0, deadline - time.time())
            thread.join(timeout=remaining)
            if thread.is_alive():
                still_alive += 1
            else:
                with self._local_threads_lock:
                    self._local_threads.pop(key, None)
        return still_alive

    # ------------------------------------------------------------
    # CORREÇÃO P1 (invariante dura — runner vivo NUNCA é convertido
    # para `UNKNOWN_OUTCOME` por atraso do heartbeat): prova
    # DETERMINÍSTICA de que o processo detentor do lease não existe
    # mais. Frescor de `leased_at`/`holder_seen_at` é evidência
    # PROBABILÍSTICA (sempre existe uma janela em que a amostra parece
    # velha — contenção SQLite, scheduler, GC); a existência do
    # processo detentor é um fato verificável via SO. A conversão por
    # TTL só prossegue quando esta prova confirma a morte.
    #  ------------------------------------------------------------

    def _holder_alive(self, pid: int, proc_start: str | None = None) -> bool:
        """Prova de liveness do detentor (CORRECAO P1b). Delega ao
        helper estatico; existe como metodo de instancia para que a
        prova de morte fique disponivel em todo o ciclo do lease,
        incluindo o reclaim de `GATE_IN_PROGRESS`."""
        return self._holder_identity_alive(pid, proc_start)

    @staticmethod
    def _holder_identity_alive(pid: int, recorded_proc_start: str | None) -> bool:
        """FECHAMENTO 1/6 — o detentor REGISTRADO (PID + identidade
        temporal) ainda está vivo?
        - PID inexistente => MORTO;
        - identidade registrada NULL (linha legada ou plataforma sem
          suporte) => só o PID decide (comportamento anterior);
        - identidade atual ilegível (acesso negado, corrida com o fim do
          processo) => VIVO — sem prova de troca, P1 prevalece;
        - identidades iguais => VIVO (é o mesmo processo);
        - identidades diferentes => o PID foi REUTILIZADO por outro
          processo; o detentor registrado MORREU (o lease não fica preso
          a um processo alheio nem ao próprio app reiniciado)."""
        if not GraphExecutor._holder_process_alive(pid):
            return False
        if recorded_proc_start is None:
            return True
        current = _process_start_identity(pid)
        if current is None:
            return True
        if current != recorded_proc_start:
            _logger.warning(
                "graph_executor: PID %s está vivo, mas é OUTRO processo (identidade registrada %s, atual %s) "
                "— PID reutilizado; o detentor registrado morreu",
                pid, recorded_proc_start, current,
            )
            return False
        return True
    @staticmethod
    def _holder_process_alive(pid: int) -> bool:
        """Devolve True se um processo com este PID existe AGORA.

        Apenas stdlib. Semântica das respostas da consulta ao SO:
        - processo inexistente (POSIX `ESRCH`; Windows `OpenProcess` com
          `ERROR_INVALID_PARAMETER`) => MORTO — preserva a recuperação de
          crash real;
        - consulta NEGADA (POSIX `EPERM`; Windows `ERROR_ACCESS_DENIED`)
          => VIVO: a negação prova que o processo EXISTE;
        - handle obtido mas `GetExitCodeProcess` falhou => VIVO (o objeto
          de processo existe e não há prova de término).

        CORREÇÃO FECHAMENTO 1/6 (causa-raiz): no Windows, `OpenProcess`
        negado devolvia False (MORTO) — um detentor vivo cuja consulta
        fosse negada podia ter o lease convertido/reclamado, violando
        P1/P1b, e divergia do ramo POSIX (`PermissionError` => vivo).
        A leitura do erro agora usa `WinDLL(use_last_error=True)` +
        `ctypes.get_last_error()` (confiável), e as assinaturas declaram
        `HANDLE` (antes `restype` padrão `c_int` truncava o handle).

        Esta função responde apenas "existe processo com este PID". A
        prova de que é o MESMO processo que detém o lease (defesa contra
        PID reutilizado) fica em `_holder_identity_alive`. Válido apenas
        no MESMO host (ver invariante single-host da classe)."""
        if pid is None or pid <= 0:
            return False
        if _IS_WINDOWS:
            try:
                kernel32, get_last_error = _win_kernel32()
                return _win_process_alive(pid, kernel32, get_last_error)
            except OSError:
                return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # existe, mas pertence a outro usuário — processo VIVO.
            return True
        except OSError:
            return False

    # ------------------------------------------------------------
    # Lease de execução — atômico, real, sobrevive a restart quando
    # há `db_path`; compartilhado entre instâncias no processo quando
    # não há.
    # ------------------------------------------------------------

    def _acquire_lease(self, mission_id: str, task_id: str) -> tuple:
        """Tenta adquirir o direito EXCLUSIVO de executar
        `(mission_id, task_id)` agora. Devolve
        `(status, lease_token_ou_None, outcome_ou_None, error_ou_None)`:
        - `"ACQUIRED"` — ganhou; `lease_token` deve ser usado para
          resolver o lease ao final (sucesso, falha, ou timeout).
        - `"IN_PROGRESS"` — outra execução (desta OU de outra
          instância) está rodando agora — NUNCA inicia uma segunda.
        - `"UNKNOWN_OUTCOME"` — uma execução anterior estourou o
          timeout e seu desfecho nunca foi confirmado — NUNCA
          retentado automaticamente, mesmo depois de um restart.
        - `"OUTCOME_AVAILABLE_PENDING_GATE"` — uma execução anterior
          estourou o timeout, mas a thread de fundo terminou DEPOIS
          com um resultado real — esse resultado ainda NUNCA passou
          pelo Quality Gate (conclusão tardia do `task_runner` NUNCA
          equivale a conclusão VALIDADA). `lease_token` aqui é um
          token NOVO, reivindicado atomicamente só para este
          chamador — ele (e só ele) é responsável por avaliar o gate
          e finalizar; `outcome` contém o resultado real, para
          avaliação, NUNCA para reexecução.
        - `"COMPLETED"` — já existe um resultado REAL e JÁ VALIDADO
          (gate aprovado, ou nenhum gate aplicável) persistido —
          `outcome` contém o resultado; nunca reexecuta.
        - `"FAILED"` — já falhou definitivamente antes — `error`
          contém o motivo."""
        if self.db_path:
            return self._acquire_lease_durable(mission_id, task_id)
        return self._acquire_lease_memory(mission_id, task_id)

    def _acquire_lease_durable(self, mission_id: str, task_id: str) -> tuple:
        from core.models import db as db_module

        token = uuid.uuid4().hex
        with db_module.connect(self.db_path) as conn:
            try:
                # CORREÇÃO P1: a aquisição registra QUEM detém o lease
                # (`holder_pid`) e o primeiro sinal de vida
                # (`holder_seen_at`) — a prova determinística de
                # liveness usada pela conversão por TTL.
                now = time.time()
                conn.execute(
                    "INSERT INTO graph_executor_task_leases "
                    "(mission_id, task_id, lease_token, status, leased_at, generation, holder_pid, holder_seen_at, "
                    "holder_proc_start) "
                    "VALUES (?, ?, ?, 'IN_PROGRESS', ?, 0, ?, ?, ?)",
                    (mission_id, task_id, token, now, os.getpid(), now, _current_process_start_identity()),
                )
                return "ACQUIRED", token, None, None
            except sqlite3.IntegrityError:
                return self._read_and_maybe_reclaim_lease_durable(conn, mission_id, task_id)

    def _read_and_maybe_reclaim_lease_durable(self, conn, mission_id: str, task_id: str) -> tuple:
        """Lê o estado ATUAL de um lease já existente e decide o que
        fazer — inclusive tentando reclaim atômico de um
        `GATE_IN_PROGRESS` EXPIRADO (recuperação de crash real —
        falha real reproduzida por auditoria independente, comprovada
        com um subprocesso morto via `os._exit()` durante a avaliação
        do gate).

        Laço LIMITADO (nunca recursão irrestrita) — cada iteração é
        uma tentativa real contra o banco; perder uma corrida releva
        o estado mais recente e tenta de novo, até um teto pequeno,
        suficiente para qualquer corrida realista entre algumas
        instâncias."""
        for _ in range(10):
            row = conn.execute(
                "SELECT status, outcome_json, error, leased_at, holder_pid, holder_seen_at "
                "FROM graph_executor_task_leases WHERE mission_id = ? AND task_id = ?",
                (mission_id, task_id),
            ).fetchone()
            if row is None:
                # corrida extremamente rara: a linha sumiu entre o
                # INSERT falhar e este SELECT (nunca deveria
                # acontecer, DELETE não é usado nesta tabela) — trata
                # como IN_PROGRESS por segurança, nunca assume
                # ACQUIRED sem uma inserção que realmente sucedeu.
                return "IN_PROGRESS", None, None, None
            if row["status"] == "COMPLETED":
                return "COMPLETED", None, (json.loads(row["outcome_json"]) if row["outcome_json"] else {}), None
            if row["status"] == "FAILED":
                return "FAILED", None, None, row["error"]

            if row["status"] == "OUTCOME_AVAILABLE_PENDING_GATE":
                # MICROCORREÇÃO CRÍTICA (falha real reproduzida por
                # auditoria independente — reivindicação NÃO
                # exclusiva): compare-and-set genuíno — o `UPDATE`
                # TRANSICIONA `status` para `'GATE_IN_PROGRESS'` (valor
                # exclusivo) E incrementa `generation` (fencing) — a
                # MESMA instrução rodada por um segundo chamador
                # encontra `status` já diferente e `rowcount == 0`.
                new_token = uuid.uuid4().hex
                # CORRECAO P1b (holder consistente): o reclamante do gate
                # passa a ser o DETENTOR registrado — sem isso, o holder_pid
                # ficava apontando para o adquirente ORIGINAL da fase de
                # execucao (possivelmente vivo) enquanto o detentor real do
                # gate (este processo) era outro; a prova de liveness do
                # proximo reclaim julgaria o processo ERRADO.
                now = time.time()
                cursor = conn.execute(
                    "UPDATE graph_executor_task_leases SET lease_token = ?, status = 'GATE_IN_PROGRESS', "
                    "leased_at = ?, generation = generation + 1, holder_pid = ?, holder_seen_at = ?, "
                    "holder_proc_start = ? "
                    "WHERE mission_id = ? AND task_id = ? "
                    "AND status = 'OUTCOME_AVAILABLE_PENDING_GATE'",
                    (new_token, now, os.getpid(), now, _current_process_start_identity(), mission_id, task_id),
                )
                if cursor.rowcount == 1:
                    outcome = json.loads(row["outcome_json"]) if row["outcome_json"] else {}
                    # devolve o status SEMÂNTICO (o que o chamador deve
                    # FAZER — avaliar o gate) — o valor ARMAZENADO
                    # (`GATE_IN_PROGRESS`) é o que bloqueia outros
                    # chamadores, nunca o que este vitorioso precisa saber.
                    return "OUTCOME_AVAILABLE_PENDING_GATE", new_token, outcome, None
                # outro chamador ganhou a corrida entre o SELECT e o
                # UPDATE — relê o estado atual, nunca assume.
                continue

            if row["status"] == "GATE_IN_PROGRESS":
                elapsed = time.time() - row["leased_at"]
                if elapsed <= self.gate_in_progress_ttl_seconds:
                    # ainda dentro do TTL -- alguém genuinamente pode
                    # estar avaliando o gate agora; nunca reclama cedo
                    # demais.
                    return "GATE_IN_PROGRESS", None, None, row["error"]
                # CORRECAO P1b (mesma invariante dura da ramificacao
                # `IN_PROGRESS`): o reclaim por TTL nunca decide sobre
                # uma amostra unica de `leased_at` — antes do CAS, exige
                # (1) RE-READ de confirmacao (estado avancou => recomeca
                # o laco) e (2) PROVA de morte do detentor: sinal de vida
                # recente (`holder_seen_at`) ou processo vivo (`holder_pid`
                # via SO) => o detentor esta AVALIANDO O GATE, apenas
                # atrasado — NUNCA e reclamado. Apenas detentor provadamente
                # morto (ou linha legada, ambos NULL) segue para o CAS.
                # MICROCORREÇÃO CRÍTICA (recuperação de crash real —
                # falha real reproduzida por auditoria independente):
                # `GATE_IN_PROGRESS` expirado — o detentor original
                # pode ter morrido abruptamente (processo/máquina,
                # nunca apenas uma exceção tratável — essa já é
                # resolvida por `_handle_gate_evaluation_error`).
                # Reclaim ATÔMICO via compare-and-set: condicionado a
                # `status` E `leased_at` ainda serem EXATAMENTE os que
                # acabamos de ler — se outra instância já reclamou (ou
                # o detentor original, afinal, resolveu) entre nossa
                # leitura e este UPDATE, `leased_at` já mudou e
                # `rowcount` será 0 — nunca duas instâncias reclamam o
                # mesmo lease expirado ao mesmo tempo. `generation` é
                # incrementado — qualquer instância com o token/
                # generation antigo (o processo morto) é incapaz de
                # finalizar ou sobrescrever depois — seu token não
                # bate mais em nenhum `_resolve_lease()` futuro.
                # CORREÇÃO P1: o reclaim troca o detentor — o novo
                # detentor passa a ser ESTE processo (prova de liveness
                # daqui em diante é sobre quem de fato detém agora).
                reconfirmed = conn.execute(
                    "SELECT status, leased_at, holder_pid, holder_seen_at, holder_proc_start, error "
                    "FROM graph_executor_task_leases WHERE mission_id = ? AND task_id = ?",
                    (mission_id, task_id),
                ).fetchone()
                if (
                    reconfirmed is None
                    or reconfirmed["status"] != "GATE_IN_PROGRESS"
                    or reconfirmed["leased_at"] != row["leased_at"]
                ):
                    continue
                seen = reconfirmed["holder_seen_at"]
                if seen is not None and (time.time() - seen) <= self.gate_in_progress_ttl_seconds:
                    _logger.warning(
                        "graph_executor: lease de gate (mission_id=%s, task_id=%s) expirou por `leased_at`, MAS o "
                        "detentor tem sinal de vida recente — detentor VIVO avaliando o gate, nunca reclamado "
                        "(invariante P1b)",
                        mission_id, task_id,
                    )
                    return "GATE_IN_PROGRESS", None, None, reconfirmed["error"]
                holder_pid = reconfirmed["holder_pid"]
                if holder_pid is not None and self._holder_alive(holder_pid, reconfirmed["holder_proc_start"]):
                    _logger.warning(
                        "graph_executor: lease de gate (mission_id=%s, task_id=%s) expirou por `leased_at`, MAS o "
                        "processo detentor (pid=%s) esta VIVO — heartbeat provavelmente atrasado/falhando; "
                        "nunca reclamado (invariante P1b)",
                        mission_id, task_id, holder_pid,
                    )
                    return "GATE_IN_PROGRESS", None, None, reconfirmed["error"]
                now = time.time()
                new_token = uuid.uuid4().hex
                cursor = conn.execute(
                    "UPDATE graph_executor_task_leases SET lease_token = ?, leased_at = ?, "
                    "generation = generation + 1, holder_pid = ?, holder_seen_at = ?, holder_proc_start = ? "
                    "WHERE mission_id = ? AND task_id = ? "
                    "AND status = 'GATE_IN_PROGRESS' AND leased_at = ?",
                    (new_token, now, os.getpid(), now, _current_process_start_identity(), mission_id, task_id,
                     row["leased_at"]),
                )
                if cursor.rowcount == 1:
                    outcome = json.loads(row["outcome_json"]) if row["outcome_json"] else {}
                    return "OUTCOME_AVAILABLE_PENDING_GATE", new_token, outcome, None
                # outra instância ganhou o reclaim (ou o detentor
                # original resolveu, afinal) — relê, nunca assume.
                continue

            if row["status"] == "IN_PROGRESS":
                # MICROCORREÇÃO CRÍTICA (falha real reproduzida por
                # auditoria independente, comprovada com subprocesso
                # real): `IN_PROGRESS` — a fase de execução do
                # `task_runner` — não tinha NENHUM mecanismo de
                # TTL/reclaim. Se o processo morresse antes do outcome
                # ser persistido (nunca sabemos se o efeito externo
                # ocorreu ou não), o lease ficava preso em
                # `IN_PROGRESS` para sempre. Corrigido com o MESMO
                # tratamento de TTL de `GATE_IN_PROGRESS` — mas com um
                # desfecho DIFERENTE e deliberado: nunca reclama como
                # `ACQUIRED` (isso reexecutaria `task_runner`,
                # prometendo exactly-once para um efeito externo
                # arbitrário — algo que este executor NUNCA promete
                # sem prova de idempotência declarada pela
                # tarefa/adaptador). Em vez disso, transiciona
                # ATOMICAMENTE para `UNKNOWN_OUTCOME` — terminal,
                # nunca retentado automaticamente — reportando
                # honestamente que o desfecho é desconhecido.
                #
                # CORREÇÃO P1 (INVARIANTE DURA — runner vivo NUNCA é
                # convertido por atraso do heartbeat): a conversão
                # exige TRÊS condições, nunca apenas o TTL:
                #   1. TTL estourado sobre `leased_at` (como antes);
                #   2. RE-READ de confirmação — se o estado avançou
                #      desde a leitura (heartbeat renovou, detentor
                #      resolveu, outra instância transicionou), recomeça
                #      o laço — elimina a conversão por amostra única;
                #   3. PROVA de morte do detentor: `holder_seen_at`
                #      fresco => detentor ativo (lease "atrasado mas
                #      vivo" — NUNCA converte); senão, o processo
                #      `holder_pid` é consultado via SO — processo
                #      vivo => NUNCA converte; morto/ausente => a
                #      conversão prossegue (recuperação de crash real
                #      preservada). Linhas legadas (ambos NULL) mantêm
                #      o comportamento anterior (TTL decide).
                # Falhas do heartbeat — lentidão, contenção, exceção
                # engolida, atraso total — erram para o lado SEGURO:
                # o lease permanece `IN_PROGRESS` enquanto o detentor
                # existir (logado como "atrasado mas vivo"), e a
                # conversão volta a significar o que sempre deveria:
                # o processo detentor MORREU de verdade e o TTL esgotou.
                elapsed = time.time() - row["leased_at"]
                if elapsed <= self.gate_in_progress_ttl_seconds:
                    return "IN_PROGRESS", None, None, row["error"]
                reconfirmed = conn.execute(
                    "SELECT status, leased_at, holder_pid, holder_seen_at, holder_proc_start, error "
                    "FROM graph_executor_task_leases WHERE mission_id = ? AND task_id = ?",
                    (mission_id, task_id),
                ).fetchone()
                if (
                    reconfirmed is None
                    or reconfirmed["status"] != "IN_PROGRESS"
                    or reconfirmed["leased_at"] != row["leased_at"]
                ):
                    continue
                seen = reconfirmed["holder_seen_at"]
                if seen is not None and (time.time() - seen) <= self.gate_in_progress_ttl_seconds:
                    _logger.warning(
                        "graph_executor: lease (mission_id=%s, task_id=%s) expirou por `leased_at`, MAS o "
                        "detentor tem sinal de vida recente (holder_seen_at) — detentor VIVO, nunca "
                        "convertido para UNKNOWN_OUTCOME por atraso de heartbeat (invariante P1)",
                        mission_id, task_id,
                    )
                    return "IN_PROGRESS", None, None, reconfirmed["error"]
                holder_pid = reconfirmed["holder_pid"]
                if holder_pid is not None and self._holder_alive(holder_pid, reconfirmed["holder_proc_start"]):
                    _logger.warning(
                        "graph_executor: lease (mission_id=%s, task_id=%s) expirou por `leased_at`, MAS o "
                        "processo detentor (pid=%s) está VIVO — heartbeat provavelmente atrasado/falhando; "
                        "nunca convertido para UNKNOWN_OUTCOME (invariante P1)",
                        mission_id, task_id, holder_pid,
                    )
                    return "IN_PROGRESS", None, None, reconfirmed["error"]
                if seen is None and holder_pid is None:
                    # linha legada (pré-migração): sem prova disponível,
                    # mantém o comportamento anterior (TTL decide).
                    pass
                elif seen is not None and (time.time() - seen) > self.gate_in_progress_ttl_seconds and holder_pid is None:
                    _logger.warning(
                        "graph_executor: lease (mission_id=%s, task_id=%s) com `holder_seen_at` velho e sem "
                        "`holder_pid` — tratando como legado, convertendo por TTL", mission_id, task_id,
                    )
                cursor = conn.execute(
                    "UPDATE graph_executor_task_leases SET status = 'UNKNOWN_OUTCOME', error = ? "
                    "WHERE mission_id = ? AND task_id = ? AND status = 'IN_PROGRESS' AND leased_at = ?",
                    (
                        "processo original pode ter morrido antes de persistir o outcome do task_runner — "
                        "desfecho genuinamente desconhecido, nunca retentado automaticamente sem prova de "
                        "idempotência",
                        mission_id, task_id, row["leased_at"],
                    ),
                )
                if cursor.rowcount == 1:
                    return "UNKNOWN_OUTCOME", None, None, (
                        "processo original pode ter morrido antes de persistir o outcome do task_runner"
                    )
                # outra instância já tratou (resolveu, ou já
                # transicionou para UNKNOWN_OUTCOME) — relê, nunca assume.
                continue

            return row["status"], None, None, row["error"]  # UNKNOWN_OUTCOME

        # teto de tentativas atingido (corrida extraordinariamente
        # persistente) — nunca gira além disso; relê uma última vez e
        # devolve o que houver, fail-closed (nunca finge ACQUIRED).
        row = conn.execute(
            "SELECT status, outcome_json, error FROM graph_executor_task_leases "
            "WHERE mission_id = ? AND task_id = ?",
            (mission_id, task_id),
        ).fetchone()
        if row is None:
            return "IN_PROGRESS", None, None, None
        if row["status"] == "COMPLETED":
            return "COMPLETED", None, (json.loads(row["outcome_json"]) if row["outcome_json"] else {}), None
        if row["status"] == "FAILED":
            return "FAILED", None, None, row["error"]
        return row["status"], None, None, row["error"]

    def _acquire_lease_memory(self, mission_id: str, task_id: str) -> tuple:
        key = (id(self.job_queue), mission_id, task_id)
        with GraphExecutor._memory_leases_lock:
            existing = GraphExecutor._memory_leases.get(key)
            if existing is None:
                token = uuid.uuid4().hex
                GraphExecutor._memory_leases[key] = {
                    "token": token, "status": "IN_PROGRESS", "outcome": None, "error": None,
                    "leased_at": time.time(), "generation": 0,
                }
                return "ACQUIRED", token, None, None
            status = existing["status"]
            if status == "COMPLETED":
                return "COMPLETED", None, existing["outcome"], None
            if status == "FAILED":
                return "FAILED", None, None, existing["error"]
            if status == "OUTCOME_AVAILABLE_PENDING_GATE":
                # MICROCORREÇÃO: mesma disciplina do caminho durável —
                # transiciona `status` para `GATE_IN_PROGRESS` (nunca
                # só troca o token) e incrementa `generation` — sob o
                # MESMO lock (já serializa totalmente em memória),
                # garantindo consistência de estado com o caminho
                # SQLite, mesmo sem o mesmo risco de corrida entre
                # conexões distintas.
                new_token = uuid.uuid4().hex
                existing["token"] = new_token
                existing["status"] = "GATE_IN_PROGRESS"
                existing["leased_at"] = time.time()
                existing["generation"] = existing.get("generation", 0) + 1
                return "OUTCOME_AVAILABLE_PENDING_GATE", new_token, existing["outcome"], None
            if status == "GATE_IN_PROGRESS":
                # MICROCORREÇÃO CRÍTICA (recuperação de crash real —
                # mesma disciplina do caminho durável, para
                # consistência): mesmo em memória, uma thread que
                # reivindicou e nunca resolveu (travou de vez, sem
                # nunca terminar) não deveria bloquear para sempre.
                elapsed = time.time() - existing.get("leased_at", time.time())
                if elapsed > self.gate_in_progress_ttl_seconds:
                    new_token = uuid.uuid4().hex
                    existing["token"] = new_token
                    existing["leased_at"] = time.time()
                    existing["generation"] = existing.get("generation", 0) + 1
                    return "OUTCOME_AVAILABLE_PENDING_GATE", new_token, existing["outcome"], None
                return "GATE_IN_PROGRESS", None, None, existing.get("error")
            if status == "IN_PROGRESS":
                # MICROCORREÇÃO CRÍTICA (falha real reproduzida por
                # auditoria independente): mesma disciplina do
                # caminho durável — um `IN_PROGRESS` sem nenhuma
                # resolução por tempo demais (uma thread que travou
                # de vez, sem nunca terminar, dentro do MESMO
                # processo) nunca reclama como `ACQUIRED` (nunca
                # reexecutaria `task_runner` sem prova de
                # idempotência) — transiciona para `UNKNOWN_OUTCOME`,
                # terminal, nunca retentado automaticamente.
                elapsed = time.time() - existing.get("leased_at", time.time())
                if elapsed > self.gate_in_progress_ttl_seconds:
                    existing["status"] = "UNKNOWN_OUTCOME"
                    existing["error"] = (
                        "thread original pode ter travado antes de persistir o outcome do task_runner"
                    )
                    return "UNKNOWN_OUTCOME", None, None, existing["error"]
                return "IN_PROGRESS", None, None, existing.get("error")
            return status, None, None, existing.get("error")  # UNKNOWN_OUTCOME

    def _resolve_lease(
        self, mission_id: str, task_id: str, *, token: str, status: str, outcome: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Marca o desfecho REAL do lease — `status` um de
        `"COMPLETED"`/`"FAILED"`/`"UNKNOWN_OUTCOME"`/
        `"OUTCOME_AVAILABLE_PENDING_GATE"`. Condicionado ao `token`
        (só quem genuinamente detém o lease pode resolvê-lo) — nunca
        sobrescreve um lease que outra execução já assumiu."""
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE graph_executor_task_leases SET status = ?, outcome_json = ?, error = ?, "
                    "resolved_at = ? WHERE mission_id = ? AND task_id = ? AND lease_token = ?",
                    (
                        status, json.dumps(outcome) if outcome is not None else None, error, time.time(),
                        mission_id, task_id, token,
                    ),
                )
            return
        key = (id(self.job_queue), mission_id, task_id)
        with GraphExecutor._memory_leases_lock:
            entry = GraphExecutor._memory_leases.get(key)
            if entry is not None and entry["token"] == token:
                entry["status"] = status
                entry["outcome"] = outcome
                entry["error"] = error

    def _persist_outcome_and_claim_gate(self, mission_id: str, task_id: str, *, token: str, outcome: dict) -> None:
        """MICROCORREÇÃO CRÍTICA (unificação do caminho FRESCO com o
        mecanismo durável já validado — falha real reproduzida por
        auditoria independente): persiste ATOMICAMENTE o outcome
        recém-obtido do `task_runner` (dentro do timeout, nunca um
        outcome tardio) E transiciona o lease DIRETAMENTE para
        `GATE_IN_PROGRESS` — ANTES de qualquer avaliação do Quality
        Gate começar. Condicionado ao `lease_token` ATUAL (a mesma
        instância que ainda detém a reivindicação original,
        `ACQUIRED`) — nunca sobrescreve um lease que, por algum
        motivo extraordinário, já não pertence mais a esta instância.

        A partir deste ponto, o outcome está PERSISTIDO e
        RECUPERÁVEL — mesmo que o processo morra abruptamente durante
        a avaliação do gate logo em seguida (o efeito real do
        `task_runner` já ocorreu), uma instância futura o encontrará
        através do MESMO mecanismo de claim exclusivo + heartbeat +
        TTL + reclaim já validado para o caminho de outcome tardio —
        nunca reexecuta `task_runner`, nunca perde o outcome. Antes
        desta correção, o outcome só existia em memória local até a
        avaliação terminar — um crash real nesse meio-tempo deixava o
        lease preso em `IN_PROGRESS` (status sem NENHUM mecanismo de
        TTL/reclaim aplicável), perdendo o outcome para sempre —
        muito pior que o cenário do outcome tardio (já corrigido em
        rodadas anteriores)."""
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                # CORREÇÃO P1: a transição troca a fase (não o detentor,
                # que continua sendo este processo) — renova
                # `holder_seen_at` para a prova de liveness da fase de
                # gate.
                conn.execute(
                    "UPDATE graph_executor_task_leases SET status = 'GATE_IN_PROGRESS', outcome_json = ?, "
                    "leased_at = ?, generation = generation + 1, holder_seen_at = ? "
                    "WHERE mission_id = ? AND task_id = ? AND lease_token = ?",
                    (json.dumps(outcome), time.time(), time.time(), mission_id, task_id, token),
                )
            return
        key = (id(self.job_queue), mission_id, task_id)
        with GraphExecutor._memory_leases_lock:
            entry = GraphExecutor._memory_leases.get(key)
            if entry is not None and entry["token"] == token:
                entry["status"] = "GATE_IN_PROGRESS"
                entry["outcome"] = outcome
                entry["leased_at"] = time.time()
                entry["generation"] = entry.get("generation", 0) + 1


    def execute_plan(
        self, plan: AdvancedPlan, *, task_runner: Callable[[PlanTask], dict], actor: str,
        mission_id: str | None = None, timeout_per_task: float = 30.0,
        cancel_event: threading.Event | None = None,
        requires_owner_approval: Callable[[PlanTask], bool] | None = None,
        approved_escalation_ids: dict | None = None,
        quality_gate_checker: Callable[[PlanTask, dict], bool] | None = None,
    ) -> PlanExecutionReport:
        """Executa `plan` de ponta a ponta. `task_runner(task) -> dict`
        é o efeito real de UMA tarefa (nunca definido aqui — este
        módulo é agnóstico de domínio). `mission_id` estável entre
        chamadas (RESUMPTION): reusar o mesmo `mission_id` para o
        mesmo `plan` faz com que tarefas já `VERIFIED` sejam puladas
        (nunca re-executadas — nunca duplica efeito externo já
        confirmado) e tarefas que ficaram travadas em estado
        intermediário (processo anterior morreu no meio) sejam
        retomadas via o MESMO `Job` idempotente do `JobQueue` (nunca
        reseta o contador de tentativas).

        FAIL-CLOSED: se um grupo tiver qualquer tarefa não-`VERIFIED`
        ao final (falhou de vez, ou ficou aguardando Owner), os grupos
        SEGUINTES nunca são iniciados — a falha/pendência não é
        escondida."""
        # CORREÇÃO: valida o grafo (ciclo/dependência desconhecida)
        # ANTES de qualquer execução — `parallel_groups()` já faz
        # isso internamente (via `topological_order()`), nunca
        # reimplementado aqui.
        groups = plan.parallel_groups()

        mission_id = mission_id or f"plan_{plan.plan_id}"
        cancel_event = cancel_event or threading.Event()
        approved_escalation_ids = approved_escalation_ids or {}
        report = PlanExecutionReport(plan_id=plan.plan_id, mission_id=mission_id)

        for group in groups:
            if cancel_event.is_set():
                report.cancelled = True
                break

            group_start = time.time()
            group_results = self._execute_group(
                group, plan=plan, mission_id=mission_id, actor=actor, task_runner=task_runner,
                timeout_per_task=timeout_per_task, cancel_event=cancel_event,
                requires_owner_approval=requires_owner_approval, approved_escalation_ids=approved_escalation_ids,
                quality_gate_checker=quality_gate_checker,
            )
            report.group_durations.append(time.time() - group_start)
            report.task_results.extend(group_results)

            if cancel_event.is_set():
                report.cancelled = True
                break
            if any(r.status not in ("VERIFIED", "SKIPPED_ALREADY_DONE") for r in group_results):
                # CORREÇÃO (fail-closed): um grupo com qualquer tarefa
                # não verificada (FAILED/WAITING_OWNER/CANCELLED) para
                # a orquestração aqui — nunca avança para o próximo
                # nível sobre uma dependência não genuinamente cumprida.
                report.failed = True
                break

        report.finished_at = time.time()
        return report

    def _execute_group(
        self, group: list, *, plan: AdvancedPlan, mission_id: str, actor: str, task_runner: Callable,
        timeout_per_task: float, cancel_event: threading.Event,
        requires_owner_approval: Callable | None, approved_escalation_ids: dict,
        quality_gate_checker: Callable | None,
    ) -> list:
        """Despacha TODAS as tarefas do grupo simultaneamente via
        `ThreadPoolExecutor` — paralelismo REAL, nunca sequencial
        disfarçado. Espera o grupo inteiro antes de devolver."""
        results = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures: dict = {}
            for task in group:
                if cancel_event.is_set():
                    break
                future = pool.submit(
                    self._execute_single_task, task, plan=plan, mission_id=mission_id, actor=actor,
                    task_runner=task_runner, timeout_per_task=timeout_per_task, cancel_event=cancel_event,
                    requires_owner_approval=requires_owner_approval,
                    approved_escalation_ids=approved_escalation_ids, quality_gate_checker=quality_gate_checker,
                )
                futures[future] = task
            # CORREÇÃO (shutdown limpo): `ThreadPoolExecutor` como
            # context manager sempre chama `shutdown(wait=True)` ao
            # sair — nunca deixa uma thread do pool órfã além do
            # escopo deste método, mesmo se uma tarefa levantar.
            for future in futures:
                results.append(future.result())
        return results

    def _execute_single_task(
        self, task: PlanTask, *, plan: AdvancedPlan, mission_id: str, actor: str, task_runner: Callable,
        timeout_per_task: float, cancel_event: threading.Event, requires_owner_approval: Callable | None,
        approved_escalation_ids: dict, quality_gate_checker: Callable | None,
    ) -> TaskExecutionResult:
        start = time.time()

        if cancel_event.is_set():
            return TaskExecutionResult(
                task_id=task.task_id, mission_id=mission_id, status="CANCELLED",
                duration_seconds=time.time() - start, error="cancelado antes de iniciar",
            )

        # ------------------------------------------------------------
        # RESUMPTION (nunca duplica efeito já confirmado)
        # ------------------------------------------------------------
        try:
            existing = self.mission_tracker.get(mission_id, task.task_id)
        except MissionNotFound:
            existing = None

        if existing is not None and existing.status == MissionStatus.VERIFIED:
            return TaskExecutionResult(
                task_id=task.task_id, mission_id=mission_id, status="SKIPPED_ALREADY_DONE",
                attempts=0, duration_seconds=time.time() - start, result=existing.result,
            )
        if existing is not None and existing.status in (MissionStatus.FAILED, MissionStatus.CANCELLED):
            # CORREÇÃO (fail-closed): uma tarefa já definitivamente
            # falhada/cancelada NUNCA é retentada silenciosamente numa
            # nova chamada de `execute_plan()` com o mesmo
            # `mission_id` — exigiria uma decisão explícita do
            # chamador (novo `mission_id`, ou uma API de retry manual
            # fora de escopo desta entrega).
            return TaskExecutionResult(
                task_id=task.task_id, mission_id=mission_id, status=existing.status.value,
                duration_seconds=time.time() - start, error=existing.error,
            )

        mission = existing or self._create_mission(plan=plan, task=task, mission_id=mission_id, actor=actor)

        # ------------------------------------------------------------
        # OWNER APPROVAL (fail-closed — nunca executa sem aprovação real)
        # ------------------------------------------------------------
        if requires_owner_approval is not None and requires_owner_approval(task):
            approval_check = self._ensure_owner_approval(
                task=task, mission=mission, actor=actor, approved_escalation_ids=approved_escalation_ids,
            )
            if approval_check is not None:
                return approval_check

        # ------------------------------------------------------------
        # DISPATCH + RETRY (via JobQueue — nunca reimplementado aqui)
        # ------------------------------------------------------------
        return self._dispatch_with_retry(
            task=task, mission=mission, mission_id=mission_id, actor=actor, task_runner=task_runner,
            timeout_per_task=timeout_per_task, cancel_event=cancel_event, quality_gate_checker=quality_gate_checker,
            start=start,
        )

    def _create_mission(self, *, plan: AdvancedPlan, task: PlanTask, mission_id: str, actor: str) -> MissionAssignment:
        # CORREÇÃO: `MissionAssignment.dependencies` usa a semântica de
        # `MissionTracker` — uma lista de OUTROS `mission_id` (missões
        # inteiras diferentes) que precisam estar `VERIFIED`. Isso é
        # um namespace DIFERENTE de `task.depends_on` (que referencia
        # `task_id` DENTRO do MESMO plano/mission_id). Popular
        # `dependencies` com `task.depends_on` aqui faria
        # `start_running()` procurar por missões inexistentes
        # (`list_by_mission(<task_id>)`) e levantar
        # `DependencyNotSatisfied` incorretamente, travando a missão
        # em QUEUED para sempre. A dependência INTRA-plano já é
        # estruturalmente garantida por `parallel_groups()` — este
        # executor só inicia o grupo N+1 depois do grupo N inteiro ter
        # sido `VERIFIED` — então `dependencies=[]` aqui é correto,
        # nunca uma omissão.
        mission = MissionAssignment.new(
            agent=actor, objective=f"{plan.objective} :: {task.description}", mission_id=mission_id,
            task_id=task.task_id, dependencies=[], success_criteria=list(task.completion_criteria),
        )
        return self.mission_tracker.create(mission)

    def _ensure_owner_approval(
        self, *, task: PlanTask, mission: MissionAssignment, actor: str, approved_escalation_ids: dict,
    ) -> TaskExecutionResult | None:
        approved_id = approved_escalation_ids.get(task.task_id)
        if approved_id is not None and self.escalation_desk is not None:
            item = self.escalation_desk.get(approved_id)
            if item.status == ApprovalStatus.APPROVED:
                return None  # aprovado -- segue para dispatch
        # ainda não aprovado -- escalona (se ainda não escalonado) e
        # marca a missão como aguardando o Owner, nunca executa.
        if self.escalation_desk is not None:
            item = self.escalation_desk.escalate(
                reason=f"Tarefa crítica '{task.task_id}' requer aprovação explícita do Owner",
                context={"task_id": task.task_id, "mission_id": mission.mission_id, "description": task.description},
                actor=actor, operation="graph_executor_task", risk="HIGH",
                idempotency_key=f"{mission.mission_id}:{task.task_id}:owner_approval",
            )
            escalation_id = item.escalation_id
        else:
            escalation_id = None
        try:
            self.mission_tracker.transition(mission.mission_id, task.task_id, status=MissionStatus.WAITING_OWNER)
        except InvalidMissionTransition:
            pass  # já estava nesse estado (ou terminal) -- nunca mascara, só não repete a transição
        return TaskExecutionResult(
            task_id=task.task_id, mission_id=mission.mission_id, status="WAITING_OWNER",
            error="aguardando aprovação explícita do Owner — nunca executado sem ela",
            escalation_id=escalation_id,
        )

    def _dispatch_with_retry(
        self, *, task: PlanTask, mission: MissionAssignment, mission_id: str, actor: str, task_runner: Callable,
        timeout_per_task: float, cancel_event: threading.Event, quality_gate_checker: Callable | None,
        start: float,
    ) -> TaskExecutionResult:
        max_attempts = max(1, task.max_retries + 1)
        job = self.job_queue.enqueue(
            job_type="graph_executor_task", idempotency_key=f"{mission_id}:{task.task_id}",
            payload={"task_id": task.task_id, "mission_id": mission_id}, project_id=mission_id,
            max_attempts=max_attempts,
        )

        # MICROCORREÇÃO (proteção multi-instância real — falha real
        # reproduzida por auditoria independente): o LEASE (memória
        # compartilhada de classe, ou SQLite persistente com
        # `db_path`) é a ÚNICA fonte de verdade sobre "alguém já está
        # executando ou já executou isto" — adquirido UMA VEZ aqui,
        # cobrindo TODOS os retries internos deste loop (sequenciais,
        # seguros — nunca duas threads concorrentes sob o MESMO
        # lease). Nunca gira esperando, nunca reexecuta sem prova
        # real: cada desfecho abaixo é imediato.
        lease_status, lease_token, cached_outcome, cached_error = self._acquire_lease(mission_id, task.task_id)

        if lease_status == "COMPLETED":
            return self._finalize_cached_completed(
                task=task, mission_id=mission_id, outcome=cached_outcome, job=job, start=start,
            )
        if lease_status == "FAILED":
            try:
                self.mission_tracker.transition(mission_id, task.task_id, status=MissionStatus.FAILED, error=cached_error)
            except InvalidMissionTransition:
                pass
            return TaskExecutionResult(
                task_id=task.task_id, mission_id=mission_id, status="FAILED", attempts=job.attempts,
                duration_seconds=time.time() - start, error=cached_error,
            )
        if lease_status in ("IN_PROGRESS", "UNKNOWN_OUTCOME", "GATE_IN_PROGRESS"):
            # CORREÇÃO CRÍTICA (item 4 — nunca gira indefinidamente,
            # nunca reexecuta silenciosamente; e microcorreção —
            # `GATE_IN_PROGRESS` é o estado exclusivo que bloqueia
            # qualquer chamador que PERDEU a corrida pela avaliação
            # do Quality Gate de um outcome tardio): outra execução
            # (desta OU de OUTRA instância, OU de antes de um
            # restart) já detém o direito exclusivo sobre esta tarefa
            # — devolve IMEDIATAMENTE, sem loop de espera algum, e
            # NUNCA avalia o gate por conta própria.
            return TaskExecutionResult(
                task_id=task.task_id, mission_id=mission_id, status="TIMED_OUT_UNKNOWN_OUTCOME",
                attempts=job.attempts, duration_seconds=time.time() - start,
                error=cached_error or (
                    f"lease em '{lease_status}' — outra execução em andamento ou desfecho desconhecido; "
                    f"nunca reexecutado sem confirmação real de término"
                ),
            )
        if lease_status == "OUTCOME_AVAILABLE_PENDING_GATE":
            # MICROCORREÇÃO CRÍTICA (falha real reproduzida por
            # auditoria independente — conclusão tardia NUNCA equivale
            # a conclusão VALIDADA): um `task_runner` de uma execução
            # anterior terminou com sucesso DEPOIS que o chamador
            # daquela vez já tinha desistido por timeout — esse
            # resultado ainda NUNCA passou pelo Quality Gate. Avalia o
            # gate AGORA, obrigatoriamente, antes de qualquer
            # `COMPLETED`/`VERIFIED` — `task_runner` NUNCA é
            # reexecutado aqui (`cached_outcome` já é o resultado
            # real).
            return self._finalize_pending_gate_outcome(
                task=task, mission_id=mission_id, outcome=cached_outcome, job=job, start=start,
                lease_token=lease_token, quality_gate_checker=quality_gate_checker, task_runner=task_runner,
                timeout_per_task=timeout_per_task, cancel_event=cancel_event,
            )

        # lease_status == "ACQUIRED" -- direito EXCLUSIVO de executar
        # (e retentar internamente) esta tarefa agora.
        return self._run_exclusive_retry_loop(
            task=task, mission_id=mission_id, job=job, task_runner=task_runner, timeout_per_task=timeout_per_task,
            cancel_event=cancel_event, quality_gate_checker=quality_gate_checker, start=start,
            lease_token=lease_token,
        )

    def _run_exclusive_retry_loop(
        self, *, task: PlanTask, mission_id: str, job, task_runner: Callable, timeout_per_task: float,
        cancel_event: threading.Event, quality_gate_checker: Callable | None, start: float, lease_token: str,
    ) -> TaskExecutionResult:
        """Executa (e retenta internamente, sequencial e seguro) sob
        um `lease_token` já EXCLUSIVAMENTE detido pelo chamador —
        usado tanto para uma primeira execução (`lease_status ==
        "ACQUIRED"`) quanto para uma tentativa NOVA e genuína após um
        outcome tardio ter sido rejeitado pelo Quality Gate (nunca
        reexecuta o outcome REJEITADO em si — sempre uma chamada
        fresca de `task_runner`)."""
        last_error = None
        lease_resolved = False
        try:
            while True:
                if cancel_event.is_set():
                    try:
                        self.job_queue.cancel(job.job_id)
                    except InvalidJobTransition:
                        pass
                    try:
                        self.mission_tracker.transition(mission_id, task.task_id, status=MissionStatus.CANCELLED)
                    except InvalidMissionTransition:
                        pass
                    self._resolve_lease(
                        mission_id, task.task_id, token=lease_token, status="FAILED", error="cancelado durante execução",
                    )
                    lease_resolved = True
                    return TaskExecutionResult(
                        task_id=task.task_id, mission_id=mission_id, status="CANCELLED", attempts=job.attempts,
                        duration_seconds=time.time() - start, error="cancelado durante execução",
                    )

                started_job = self.job_queue.try_start(job.job_id)
                if started_job is None:
                    # com o lease exclusivo já garantido, isso só
                    # aconteceria por um estado inesperado do job
                    # (nunca por concorrência real, que o lease já
                    # impede) — nunca gira esperando: trata como falha
                    # honesta e resolve o lease.
                    job = self.job_queue.get(job.job_id)
                    if job.state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
                        break
                    last_error = f"job em estado inesperado ({job.state}) apesar do lease exclusivo adquirido"
                    self._resolve_lease(mission_id, task.task_id, token=lease_token, status="FAILED", error=last_error)
                    lease_resolved = True
                    return TaskExecutionResult(
                        task_id=task.task_id, mission_id=mission_id, status="FAILED", attempts=job.attempts,
                        duration_seconds=time.time() - start, error=last_error,
                    )
                job = started_job

                try:
                    if self.mission_tracker.get(mission_id, task.task_id).status == MissionStatus.QUEUED:
                        # CORREÇÃO: a state machine de `MissionTracker`
                        # exige QUEUED -> PLANNED -> RUNNING (nunca
                        # QUEUED -> RUNNING direto).
                        self.mission_tracker.transition(mission_id, task.task_id, status=MissionStatus.PLANNED)
                except InvalidMissionTransition:
                    pass
                try:
                    self.mission_tracker.start_running(mission_id, task.task_id, agent_principal=None)
                except (InvalidMissionTransition, DependencyNotSatisfied):
                    pass

                run_status, outcome, error = self._run_with_timeout_tracked(
                    mission_id=mission_id, task_id=task.task_id, lease_token=lease_token, task_runner=task_runner,
                    task=task, timeout_seconds=timeout_per_task,
                )

                if run_status == "TIMED_OUT_UNKNOWN_OUTCOME":
                    # MICROCORREÇÃO CRÍTICA (liveness real para
                    # `IN_PROGRESS` — falha real reproduzida por
                    # auditoria independente): a versão anterior
                    # resolvia o lease para `UNKNOWN_OUTCOME`
                    # IMEDIATAMENTE aqui, apenas porque O PRÓPRIO
                    # `timeout_per_task` desta chamada expirou — mas
                    # isso IGNORAVA completamente a proteção do
                    # heartbeat: se a thread real ainda estiver
                    # genuinamente viva (rodando além do
                    # `timeout_per_task`, mas dentro do `TTL` graças
                    # ao heartbeat), sobrescrever o lease aqui
                    # destruiria essa proteção, fazendo O PRÓPRIO
                    # chamador reportar `UNKNOWN_OUTCOME` para um
                    # trabalho que estava perfeitamente vivo. Corrigido:
                    # NUNCA toca o lease aqui — deixa exatamente como
                    # `_run_with_timeout_tracked()`/o heartbeat o
                    # deixaram. Se a thread real ainda está viva, o
                    # heartbeat continua renovando `leased_at` (nunca
                    # parece expirado); se genuinamente morreu, uma
                    # chamada FUTURA (desta ou de outra instância)
                    # verá o TTL expirado sem heartbeat e converterá
                    # para `UNKNOWN_OUTCOME` de verdade, atomicamente,
                    # na aquisição do lease.
                    lease_resolved = True  # nunca deixa o `finally` também tocar o lease aqui
                    return TaskExecutionResult(
                        task_id=task.task_id, mission_id=mission_id, status="TIMED_OUT_UNKNOWN_OUTCOME",
                        attempts=job.attempts, duration_seconds=time.time() - start, error=error,
                    )

                if run_status == "OK":
                    if task.quality_gate is not None:
                        # MICROCORREÇÃO CRÍTICA (unificação do
                        # caminho fresco com o mecanismo durável já
                        # validado — falha real reproduzida por
                        # auditoria independente): ANTES desta
                        # correção, o outcome recém-obtido só existia
                        # em memória LOCAL até a avaliação do gate
                        # terminar — se o processo morresse
                        # ABRUPTAMENTE durante o gate (o efeito real
                        # do `task_runner` já tinha ocorrido!), o
                        # outcome nunca fora persistido e o lease
                        # ficava preso em `IN_PROGRESS` — status SEM
                        # NENHUM mecanismo de TTL/reclaim aplicável —
                        # perdido para sempre, muito pior que o
                        # cenário já corrigido do caminho tardio.
                        # Corrigido: persiste o outcome ATOMICAMENTE
                        # e transiciona o lease DIRETO para
                        # `GATE_IN_PROGRESS` (mesmo `lease_token`,
                        # `generation` avançada) ANTES de sequer
                        # começar a avaliar o gate — a partir daqui, a
                        # avaliação usa o MESMO mecanismo já validado
                        # para o caminho tardio (claim exclusivo +
                        # heartbeat + TTL + reclaim), delegando
                        # diretamente para
                        # `_finalize_pending_gate_outcome()` — nunca
                        # um caminho separado e mais frágil.
                        self._persist_outcome_and_claim_gate(
                            mission_id, task.task_id, token=lease_token, outcome=outcome,
                        )
                        lease_resolved = True  # a partir daqui, _finalize_pending_gate_outcome() é responsável pelo lease em TODOS os seus desfechos
                        return self._finalize_pending_gate_outcome(
                            task=task, mission_id=mission_id, outcome=outcome, job=job, start=start,
                            lease_token=lease_token, quality_gate_checker=quality_gate_checker,
                            task_runner=task_runner, timeout_per_task=timeout_per_task, cancel_event=cancel_event,
                        )

                    # sem `quality_gate` aplicável -- MICROCORREÇÃO
                    # CRÍTICA (falha real reproduzida por auditoria
                    # independente, comprovada com subprocesso real):
                    # ANTES desta correção, o outcome só era
                    # persistido no lease como ÚLTIMO passo — se o
                    # processo morresse ABRUPTAMENTE em QUALQUER ponto
                    # entre o `task_runner` retornar sucesso e essa
                    # última linha (durante a sincronização de
                    # Mission/Job, por exemplo), o lease ficava preso
                    # em `IN_PROGRESS` com `outcome_json=NULL` — e
                    # `IN_PROGRESS`, até esta correção, não tinha
                    # NENHUM mecanismo de TTL/reclaim (só
                    # `GATE_IN_PROGRESS` tinha) — perdido para sempre,
                    # mesmo o efeito real já tendo ocorrido. Corrigido:
                    # persiste o outcome e resolve o lease para
                    # `COMPLETED` PRIMEIRO (atômico, condicionado ao
                    # `lease_token` atual) — SÓ DEPOIS sincroniza
                    # Mission/Job, que na pior hipótese (crash logo
                    # após esta linha) ficam dessincronizados
                    # TEMPORARIAMENTE, mas nunca perdidos: uma retomada
                    # futura encontra o lease `COMPLETED` com o outcome
                    # real e usa `_finalize_cached_completed()` para
                    # reconciliar Mission/Job sem jamais reexecutar
                    # `task_runner`.
                    self._resolve_lease(mission_id, task.task_id, token=lease_token, status="COMPLETED", outcome=outcome)
                    lease_resolved = True
                    verified = self._advance_mission_to_completed_and_verify(mission_id, task.task_id, outcome)
                    try:
                        fresh_job = self.job_queue.get(job.job_id)
                        if fresh_job.state not in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
                            job = self.job_queue.complete(job.job_id, outcome)
                        else:
                            job = fresh_job
                    except Exception:
                        pass
                    return TaskExecutionResult(
                        task_id=task.task_id, mission_id=mission_id, status=verified.status.value,
                        attempts=job.attempts, duration_seconds=time.time() - start, result=outcome,
                    )

                # run_status == "ERROR" -- falha real e definitiva do
                # task_runner (nunca um timeout).
                last_error = error
                job = self.job_queue.fail(job.job_id, error)
                if job.state == JobState.FAILED:
                    try:
                        self.mission_tracker.transition(mission_id, task.task_id, status=MissionStatus.FAILED, error=error)
                    except InvalidMissionTransition:
                        pass
                    self._resolve_lease(mission_id, task.task_id, token=lease_token, status="FAILED", error=error)
                    lease_resolved = True
                    return TaskExecutionResult(
                        task_id=task.task_id, mission_id=mission_id, status="FAILED", attempts=job.attempts,
                        duration_seconds=time.time() - start, error=error,
                    )
                try:
                    self.mission_tracker.transition(mission_id, task.task_id, status=MissionStatus.RETRYING, error=error)
                except InvalidMissionTransition:
                    pass
                # continua o loop -- SEGURO, ainda sob o MESMO lease exclusivo
        finally:
            if not lease_resolved:
                # SEGURANÇA (fail-closed): qualquer saída inesperada
                # deste bloco (exceção de programação não prevista)
                # NUNCA deixa o lease preso em `IN_PROGRESS` sem
                # sinalizar — marca desfecho desconhecido
                # explicitamente, nunca finge que terminou bem.
                self._resolve_lease(
                    mission_id, task.task_id, token=lease_token, status="UNKNOWN_OUTCOME",
                    error="erro interno inesperado durante a execução",
                )

        return TaskExecutionResult(
            task_id=task.task_id, mission_id=mission_id, status="FAILED", attempts=job.attempts,
            duration_seconds=time.time() - start, error=last_error or "job não pôde ser iniciado",
        )

    def _handle_gate_unavailable(
        self, *, task: PlanTask, mission_id: str, outcome: dict, job, start: float, lease_token: str,
    ) -> TaskExecutionResult:
        """MICROCORREÇÃO CRÍTICA (fail-open — falha real reproduzida
        por auditoria independente): `task.quality_gate` está
        declarado, mas nenhum `quality_gate_checker` foi fornecido
        nesta chamada — NUNCA aprova implicitamente, NUNCA marca
        Job/Mission como `COMPLETED`/`VERIFIED`, NUNCA reexecuta
        `task_runner` (o `outcome` já é o efeito real). O lease é
        devolvido para `OUTCOME_AVAILABLE_PENDING_GATE` (usando o
        MESMO `lease_token` que este chamador detém no momento —
        seja o token original de `ACQUIRED`, seja o token de
        `GATE_IN_PROGRESS` reivindicado por uma retomada) — para que
        uma chamada FUTURA, com o checker genuinamente disponível,
        possa reivindicar e avaliar de verdade. Job/Mission
        permanecem exatamente como estavam (tipicamente `RUNNING`) —
        estado honesto de "outcome disponível, ainda não validado",
        nunca mascarado."""
        self._resolve_lease(
            mission_id, task.task_id, token=lease_token, status="OUTCOME_AVAILABLE_PENDING_GATE", outcome=outcome,
        )
        return TaskExecutionResult(
            task_id=task.task_id, mission_id=mission_id, status="QUALITY_GATE_UNAVAILABLE", attempts=job.attempts,
            duration_seconds=time.time() - start, result=outcome,
            error=(
                f"Quality Gate '{task.quality_gate}' está declarado para esta tarefa, mas nenhum "
                f"quality_gate_checker foi fornecido nesta chamada — outcome preservado, nunca aprovado "
                f"implicitamente (fail-closed)"
            ),
        )

    def _handle_gate_evaluation_error(
        self, *, task: PlanTask, mission_id: str, outcome: dict, job, start: float, lease_token: str,
        exc: Exception,
    ) -> TaskExecutionResult:
        """MICROCORREÇÃO CRÍTICA (lease órfão em `GATE_IN_PROGRESS` —
        falha real reproduzida por auditoria independente): o
        `quality_gate_checker` foi FORNECIDO, mas levantou uma
        exceção durante a avaliação (ex: erro de infraestrutura,
        timeout de rede dentro do próprio checker, bug do checker).
        Antes, essa exceção propagava sem nunca resolver o lease —
        ele ficava preso em `GATE_IN_PROGRESS` PARA SEMPRE, e uma
        instância FUTURA com um checker saudável encontrava
        `GATE_IN_PROGRESS` (estado exclusivo, bloqueante) e nunca
        conseguia reivindicar/finalizar o outcome já obtido de verdade
        — mesmo o `task_runner` já tendo rodado com sucesso.

        Corrigido — FAIL-CLOSED e RECUPERÁVEL: NUNCA marca
        Job/Mission como `COMPLETED`/`VERIFIED`, NUNCA reexecuta
        `task_runner` (o `outcome` já é o efeito real). O lease é
        devolvido ATOMICAMENTE para `OUTCOME_AVAILABLE_PENDING_GATE`
        (usando o MESMO `lease_token` que este chamador detém no
        momento) — nunca fica órfão em `GATE_IN_PROGRESS` — para que
        uma chamada FUTURA, com um checker saudável, possa
        reivindicar e reavaliar de verdade. O erro real é registrado
        (telemetria) e devolvido explicitamente no resultado, nunca
        engolido silenciosamente."""
        error_message = (
            f"Quality Gate '{task.quality_gate}' levantou exceção durante a avaliação do outcome de "
            f"'{task.task_id}': {type(exc).__name__}: {exc}"
        )
        _logger.warning(
            "graph_executor: quality_gate_checker levantou exceção ao avaliar outcome "
            "(mission_id=%s, task_id=%s, quality_gate=%s): %s: %s — lease devolvido para "
            "OUTCOME_AVAILABLE_PENDING_GATE, outcome preservado, nunca reexecutado",
            mission_id, task.task_id, task.quality_gate, type(exc).__name__, exc,
        )
        self._resolve_lease(
            mission_id, task.task_id, token=lease_token, status="OUTCOME_AVAILABLE_PENDING_GATE", outcome=outcome,
            error=error_message,
        )
        return TaskExecutionResult(
            task_id=task.task_id, mission_id=mission_id, status="QUALITY_GATE_EVALUATION_ERROR",
            attempts=job.attempts, duration_seconds=time.time() - start, result=outcome, error=error_message,
        )

    def _renew_lease_heartbeat(self, mission_id: str, task_id: str, *, token: str) -> None:
        """MICROCORREÇÃO CRÍTICA (liveness real — falha real
        reproduzida por auditoria independente): sem heartbeat, uma
        execução SAUDÁVEL mas mais lenta que `gate_in_progress_ttl_
        seconds` fazia OUTRA instância considerar o lease morto,
        reclamar/converter, e agir sobre o MESMO trabalho
        simultaneamente. Renova ATOMICAMENTE `leased_at` de um lease
        `GATE_IN_PROGRESS` OU `IN_PROGRESS` — condicionado ao `token`
        ATUAL:
        - Se o token bate (esta instância genuinamente ainda detém o
          lease): `leased_at` avança, o lease nunca parece expirado
          para outra instância enquanto o heartbeat continuar.
        - Se o token NÃO bate mais (outra instância já reclamou —
          nunca deveria acontecer enquanto o heartbeat estiver
          rodando corretamente, mas seria o caso de um crash real
          seguido de reclaim): a renovação é um no-op silencioso —
          NUNCA sobrescreve um lease que já foi assumido por outra
          instância, nunca finge deter algo que não detém mais."""
        if self.db_path:
            from core.models import db as db_module

            # CORREÇÃO P1: (a) renova `leased_at` E `holder_seen_at` no
            # MESMO UPDATE — um write, uma linha, sem segunda conexão;
            # (b) NUNCA levanta: antes, uma exceção aqui matava a
            # thread de heartbeat SILENCIOSAMENTE e o detentor vivo
            # ficava desprotegido. Uma renovação mal-sucedida é
            # registrada e o loop de chamada continua tentando — e a
            # prova de liveness por PID (checada na conversão) cobre a
            # invariante independentemente do sucesso da renovação.
            now = time.time()
            try:
                with db_module.connect(self.db_path) as conn:
                    conn.execute(
                        "UPDATE graph_executor_task_leases SET leased_at = ?, holder_seen_at = ? "
                        "WHERE mission_id = ? AND task_id = ? AND lease_token = ? "
                        "AND status IN ('GATE_IN_PROGRESS', 'IN_PROGRESS')",
                        (now, now, mission_id, task_id, token),
                    )
            except Exception as exc:
                _logger.warning(
                    "graph_executor: renovação de heartbeat falhou (mission_id=%s, task_id=%s): %s: %s — "
                    "o loop continua tentando; a invariante P1 não depende deste sucesso",
                    mission_id, task_id, type(exc).__name__, exc,
                )
            return
        key = (id(self.job_queue), mission_id, task_id)
        with GraphExecutor._memory_leases_lock:
            entry = GraphExecutor._memory_leases.get(key)
            if entry is not None and entry["token"] == token and entry["status"] in ("GATE_IN_PROGRESS", "IN_PROGRESS"):
                entry["leased_at"] = time.time()

    def _evaluate_quality_gate_with_heartbeat(
        self, task: PlanTask, outcome: dict, quality_gate_checker: Callable | None, *, mission_id: str,
        task_id: str, lease_token: str,
    ) -> tuple:
        """Chama `_evaluate_quality_gate()` (que invoca o
        `quality_gate_checker` do chamador, uma função SÍNCRONA e
        bloqueante) com um HEARTBEAT periódico rodando em segundo
        plano enquanto ela executa — a cada `gate_in_progress_ttl_
        seconds / 3` (nunca mais devagar que um terço do TTL, para
        sempre renovar bem antes de expirar), `leased_at` é
        atomicamente avançado via `_renew_lease_heartbeat()` usando o
        `lease_token` ATUAL desta instância. Um checker genuinamente
        saudável, mesmo que demore MUITO mais que o TTL, nunca faz o
        lease parecer morto para outra instância — reclaim só ocorre
        de verdade quando o heartbeat CESSA (crash real, sem exceção
        tratável, sem heartbeat algum rodando) e o TTL se esgota sem
        renovação nenhuma."""
        stop_heartbeat = threading.Event()
        interval = max(0.02, self.gate_in_progress_ttl_seconds / 3.0)

        def _heartbeat_loop():
            # CORREÇÃO P1: (a) a thread de heartbeat NUNCA morre por
            # uma exceção de renovação ( `_renew_lease_heartbeat` não
            # levanta; qualquer exceção restante é engolida com log e
            # contagem — a thread segue viva enquanto o gate avaliar);
            # (b) UMA conexão SQLite é aberta para todo o ciclo e
            # reutilizada — antes, cada batida abria uma conexão nova
            # (0,1–0,2s sob contenção) e o período efetivo ficava
            # próximo do próprio TTL.
            from core.models import db as db_module

            # CORREÇÃO F1: em modo memória (`db_path=None`) não existe
            # SQLite — abrir conexão levantaria TypeError em TODA batida
            # (heartbeat inoperante e log poluído). A renovação vai pelo
            # registro em memória de `_renew_lease_heartbeat`, com a
            # mesma primeira batida imediata já aplicada ao ramo de DB.
            if not self.db_path:
                self._renew_lease_heartbeat(mission_id, task_id, token=lease_token)
                while not stop_heartbeat.wait(timeout=interval):
                    self._renew_lease_heartbeat(mission_id, task_id, token=lease_token)
                return

            # conexão de vida longa via `get_connection` (crua — o
            # commit/close ficam sob nosso controle; o context manager
            # `connect()` fecharia a cada batida, que é justamente o
            # custo que esta correção elimina).
            conn = None
            consecutive_failures = 0
            try:
                conn = db_module.get_connection(self.db_path)
            except Exception as exc:
                _logger.warning(
                    "graph_executor: heartbeat não conseguiu abrir conexão dedicada (task_id=%s): %s: %s — "
                    "cada batida tentará reconectar individualmente",
                    task_id, type(exc).__name__, exc,
                )
            # PRIMEIRA BATIDA IMEDIATA: bate ANTES de esperar o
            # primeiro intervalo. Medicao nesta maquina (sonda de
            # mudancas de leased_at, 16/16 runs): a 1a batida efetiva
            # pousava 0.41-0.56s apos a aquisicao do lease (espera-
            # -depois + custo da 1a conexao = 3-6x o intervalo
            # nominal), consumindo a janela entre aquisicao e a
            # primeira renovacao. Batendo imediatamente, o lease nunca
            # comeca a vida parecendo atrasado; a cadencia periodica
            # segue identica (espera -> bate).
            self._renew_lease_heartbeat(mission_id, task_id, token=lease_token)
            while not stop_heartbeat.wait(timeout=interval):
                try:
                    if conn is None:
                        conn = db_module.get_connection(self.db_path)
                    now = time.time()
                    conn.execute(
                        "UPDATE graph_executor_task_leases SET leased_at = ?, holder_seen_at = ? "
                        "WHERE mission_id = ? AND task_id = ? AND lease_token = ? "
                        "AND status IN ('GATE_IN_PROGRESS', 'IN_PROGRESS')",
                        (now, now, mission_id, task_id, lease_token),
                    )
                    conn.commit()
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    try:
                        if conn is not None:
                            conn.close()
                    except Exception:
                        pass
                    conn = None
                    _logger.warning(
                        "graph_executor: heartbeat do gate não renovou (task_id=%s, falha consecutiva #%s): %s: %s",
                        task_id, consecutive_failures, type(exc).__name__, exc,
                    )
            # Encerramento normal: o gate avaliou — a thread SAI DO
            # LOOP e agora fecha sua conexao dedicada.
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop, name=f"graph-gate-heartbeat-{task_id}", daemon=True,
        )
        heartbeat_thread.start()
        try:
            return self._evaluate_quality_gate(task, outcome, quality_gate_checker)
        finally:
            # CORREÇÃO (shutdown limpo — mesma disciplina já
            # estabelecida no restante deste módulo): sinaliza parada
            # e aguarda de forma limitada — nunca deixa a thread de
            # heartbeat como uma thread genuinamente órfã além deste
            # método; ela é daemon de qualquer forma, então mesmo se
            # o `join` esgotasse o prazo (jamais deveria, dado que o
            # `interval` é curto), o processo ainda nunca ficaria
            # preso por causa dela.
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=max(1.0, interval * 2))

    def _finalize_pending_gate_outcome(
        self, *, task: PlanTask, mission_id: str, outcome: dict, job, start: float, lease_token: str,
        quality_gate_checker: Callable | None, task_runner: Callable, timeout_per_task: float,
        cancel_event: threading.Event,
    ) -> TaskExecutionResult:
        """Avalia o Quality Gate sobre um outcome de conclusão TARDIA
        — `task_runner` NUNCA é reexecutado aqui para obter este
        resultado (`outcome` já é o efeito real e definitivo que já
        aconteceu). Se aprovado (ou nenhum gate aplicável): finaliza
        Job/Mission/lease como de costume. Se rejeitado: segue o
        MESMO fluxo normal de rejeição/retry/falha que uma rejeição
        recém-obtida seguiria — se houver retries disponíveis, uma
        tentativa NOVA e genuína de `task_runner` é iniciada (nunca
        reaproveita o outcome tardio rejeitado, nunca reexecuta o
        efeito ORIGINAL que já ocorreu e foi rejeitado — o novo
        `task_runner()` é uma chamada fresca e distinta).

        MICROCORREÇÃO CRÍTICA (lease órfão — falha real reproduzida
        por auditoria independente): a chamada ao `quality_gate_checker`
        (via `_evaluate_quality_gate_with_heartbeat`, que renova o
        lease atomicamente enquanto o checker roda — ver liveness
        acima) é envolvida em tratamento de exceção — se o checker
        levantar, o lease (já exclusivamente reivindicado como
        `GATE_IN_PROGRESS` neste ponto) é devolvido atomicamente para
        `OUTCOME_AVAILABLE_PENDING_GATE`, nunca deixado órfão (ver
        `_handle_gate_evaluation_error`)."""
        try:
            gate_verdict, gate_decision = self._evaluate_quality_gate_with_heartbeat(
                task, outcome, quality_gate_checker, mission_id=mission_id, task_id=task.task_id,
                lease_token=lease_token,
            )
        except Exception as exc:
            return self._handle_gate_evaluation_error(
                task=task, mission_id=mission_id, outcome=outcome, job=job, start=start, lease_token=lease_token,
                exc=exc,
            )

        if gate_decision == "UNAVAILABLE":
            # MICROCORREÇÃO CRÍTICA (fail-open — falha real
            # reproduzida por auditoria independente): gate DECLARADO
            # mas nenhum `quality_gate_checker` foi fornecido nesta
            # chamada de retomada — NUNCA aprova implicitamente. Libera
            # a reivindicação exclusiva (`GATE_IN_PROGRESS`) de volta
            # para `OUTCOME_AVAILABLE_PENDING_GATE` (usando o MESMO
            # `lease_token` que este chamador detém agora) — para que
            # uma chamada FUTURA com o checker real possa reivindicar
            # e avaliar de verdade. `task_runner` nunca é reexecutado.
            return self._handle_gate_unavailable(
                task=task, mission_id=mission_id, outcome=outcome, job=job, start=start, lease_token=lease_token,
            )

        if gate_decision == "APPROVED":
            try:
                current = self.mission_tracker.get(mission_id, task.task_id)
                if current.status != MissionStatus.VERIFIED:
                    if current.status != MissionStatus.COMPLETED:
                        self.mission_tracker.transition(mission_id, task.task_id, status=MissionStatus.COMPLETED, result=outcome)
                    evidence = {"success_criteria_met": True, "outcome": outcome}
                    current = self.mission_tracker.verify(
                        mission_id, task.task_id, evidence=evidence, quality_gate_verdict=gate_verdict,
                    )
            except (InvalidMissionTransition, MissionNotFound):
                current = self.mission_tracker.get(mission_id, task.task_id)
            try:
                fresh_job = self.job_queue.get(job.job_id)
                if fresh_job.state not in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
                    job = self.job_queue.complete(job.job_id, outcome)
                else:
                    job = fresh_job
            except Exception:
                pass
            self._resolve_lease(mission_id, task.task_id, token=lease_token, status="COMPLETED", outcome=outcome)
            return TaskExecutionResult(
                task_id=task.task_id, mission_id=mission_id, status=current.status.value, attempts=job.attempts,
                duration_seconds=time.time() - start, result=outcome,
            )

        # gate REJEITOU o outcome tardio -- fluxo normal de retry/
        # falha, NUNCA reaproveitando esse outcome como se fosse
        # válido.
        error = f"Quality Gate '{task.quality_gate}' rejeitado para outcome tardio da tarefa '{task.task_id}'"
        job = self.job_queue.fail(job.job_id, error)
        if job.state == JobState.FAILED:
            try:
                self.mission_tracker.transition(mission_id, task.task_id, status=MissionStatus.FAILED, error=error)
            except InvalidMissionTransition:
                pass
            self._resolve_lease(mission_id, task.task_id, token=lease_token, status="FAILED", error=error)
            return TaskExecutionResult(
                task_id=task.task_id, mission_id=mission_id, status="FAILED", attempts=job.attempts,
                duration_seconds=time.time() - start, error=error,
            )
        try:
            self.mission_tracker.transition(mission_id, task.task_id, status=MissionStatus.RETRYING, error=error)
        except InvalidMissionTransition:
            pass
        # retries disponíveis -- entra no MESMO loop de retry
        # exclusivo, sob o lease JÁ reivindicado por este chamador,
        # para uma tentativa NOVA e genuína (nunca reexecuta o
        # outcome tardio rejeitado).
        return self._run_exclusive_retry_loop(
            task=task, mission_id=mission_id, job=job, task_runner=task_runner, timeout_per_task=timeout_per_task,
            cancel_event=cancel_event, quality_gate_checker=quality_gate_checker, start=start,
            lease_token=lease_token,
        )

    def _advance_mission_to_completed_and_verify(self, mission_id: str, task_id: str, outcome: dict) -> MissionAssignment:
        """MICROCORREÇÃO CRÍTICA (reconciliação robusta após crash —
        falha real reproduzida por auditoria independente): avança a
        missão, a partir de QUALQUER estado intermediário válido em
        que possa estar (o processo pode ter morrido em qualquer
        ponto do fluxo normal — `QUEUED`, `PLANNED`, ou `RUNNING`),
        até `COMPLETED` → `VERIFIED` — nunca reexecuta `task_runner`,
        apenas sincroniza o rastreamento com um outcome que já é REAL
        e persistido (no lease, ou passado por quem já confirmou o
        sucesso). Idempotente: se a missão já estiver em `COMPLETED`
        ou `VERIFIED`, só completa o que faltar; nunca falha por
        tentar repetir uma transição já feita."""
        current = self.mission_tracker.get(mission_id, task_id)
        if current.status == MissionStatus.QUEUED:
            current = self.mission_tracker.transition(mission_id, task_id, status=MissionStatus.PLANNED)
        if current.status == MissionStatus.PLANNED:
            current = self.mission_tracker.transition(mission_id, task_id, status=MissionStatus.RUNNING)
        if current.status == MissionStatus.RUNNING:
            current = self.mission_tracker.transition(mission_id, task_id, status=MissionStatus.COMPLETED, result=outcome)
        if current.status == MissionStatus.COMPLETED:
            evidence = {"success_criteria_met": True, "outcome": outcome}
            current = self.mission_tracker.verify(mission_id, task_id, evidence=evidence, quality_gate_verdict=None)
        return current

    def _finalize_cached_completed(self, *, task: PlanTask, mission_id: str, outcome: dict, job, start: float) -> TaskExecutionResult:
        """Um lease já `COMPLETED` (de uma execução anterior — desta
        OU de OUTRA instância) foi encontrado na aquisição — nunca
        reexecuta `task_runner`. Só garante que Mission/Job estejam
        sincronizados com esse desfecho real (podem não estar, se a
        resolução veio de uma thread de fundo que só toca o lease,
        nunca o Mission/Job diretamente — ver `_run_with_timeout_tracked` —
        ou se o processo morreu entre resolver o lease e sincronizar
        Mission/Job — ver a correção em `_run_exclusive_retry_loop`).
        Usa `_advance_mission_to_completed_and_verify()` — robusto a
        QUALQUER estado intermediário válido em que a missão possa
        estar (nunca assume que já passou por `RUNNING`)."""
        try:
            current = self._advance_mission_to_completed_and_verify(mission_id, task.task_id, outcome)
        except (InvalidMissionTransition, MissionNotFound):
            current = self.mission_tracker.get(mission_id, task.task_id)
        try:
            fresh_job = self.job_queue.get(job.job_id)
            if fresh_job.state not in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
                job = self.job_queue.complete(job.job_id, outcome)
            else:
                job = fresh_job
        except Exception:
            pass
        return TaskExecutionResult(
            task_id=task.task_id, mission_id=mission_id, status=current.status.value, attempts=job.attempts,
            duration_seconds=time.time() - start, result=outcome,
        )

    def _run_with_timeout_tracked(
        self, *, mission_id: str, task_id: str, lease_token: str, task_runner: Callable, task: PlanTask,
        timeout_seconds: float,
    ) -> tuple:
        """Executa `task_runner(task)` com timeout REAL, via uma
        thread DAEMON dedicada — nunca `ThreadPoolExecutor` aqui (ver
        nota de shutdown limpo abaixo). O lease (`lease_token`) já foi
        adquirido pelo CHAMADOR antes desta função ser invocada — esta
        função NUNCA decide exclusão, só executa e, quando a thread
        termina DEPOIS que o chamador já desistiu por timeout, resolve
        o MESMO lease sozinha com o desfecho REAL (para que uma
        chamada futura, desta ou de outra instância, veja o resultado
        verdadeiro em vez de reexecutar).

        Devolve `(status, outcome, error)` — `status` é um de `"OK"`,
        `"ERROR"`, ou `"TIMED_OUT_UNKNOWN_OUTCOME"`.

        MICROCORREÇÃO CRÍTICA (conclusão tardia NUNCA equivale a
        conclusão VALIDADA — falha real reproduzida por auditoria
        independente): quando a thread termina COM SUCESSO depois que
        o chamador já desistiu por timeout, o lease é resolvido como
        `"OUTCOME_AVAILABLE_PENDING_GATE"` — NUNCA diretamente
        `"COMPLETED"`. Um sucesso tardio do `task_runner` ainda não
        passou pelo Quality Gate — só `_finalize_pending_gate_outcome()`
        (chamado por uma chamada FUTURA de `_dispatch_with_retry`, ao
        adquirir o lease) tem autoridade para avaliar o gate e marcar
        `COMPLETED` de verdade. Uma falha tardia (exceção real) segue
        indo direto para `"FAILED"` — sem sentido em passar por um
        Quality Gate.

        CORREÇÃO (shutdown limpo — bug real encontrado no
        desenvolvimento desta própria entrega): `concurrent.futures`
        registra um handler `atexit` que, na saída do processo, JUNTA
        TODA thread já criada por QUALQUER `ThreadPoolExecutor` do
        processo — mesmo com `shutdown(wait=False)` explícito.
        Corrigido usando `threading.Thread(daemon=True)` crua."""
        local_key = (mission_id, task_id)
        heartbeat_key = (mission_id, task_id, "heartbeat")
        done_event = threading.Event()
        gave_up_event = threading.Event()
        outcome_box: list = []

        def _target():
            try:
                outcome_box.append(("ok", task_runner(task)))
            except Exception as exc:
                outcome_box.append(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                # CORREÇÃO: sinaliza `done_event` PRIMEIRO — o loop de
                # heartbeat (abaixo) checa exatamente este evento a
                # cada iteração para saber quando parar de renovar.
                done_event.set()
                if gave_up_event.is_set():
                    # o chamador já desistiu (timeout já reportado) —
                    # SOMENTE agora esta thread é responsável por
                    # resolver o lease com o desfecho REAL. Um sucesso
                    # aqui NUNCA é validado (nunca passou pelo Quality
                    # Gate) — fica pendente até uma chamada futura
                    # avaliar o gate explicitamente.
                    kind, payload = outcome_box[0]
                    if kind == "ok":
                        self._resolve_lease(
                            mission_id, task_id, token=lease_token, status="OUTCOME_AVAILABLE_PENDING_GATE",
                            outcome=payload,
                        )
                    else:
                        self._resolve_lease(mission_id, task_id, token=lease_token, status="FAILED", error=payload)
                with self._local_threads_lock:
                    self._local_threads.pop(local_key, None)

        thread = threading.Thread(target=_target, name=f"graph-task-{task_id}", daemon=True)
        with self._local_threads_lock:
            self._local_threads[local_key] = thread
        thread.start()

        # MICROCORREÇÃO CRÍTICA (liveness real para `IN_PROGRESS` —
        # falha real reproduzida por auditoria independente): sem
        # isto, um `task_runner` SAUDÁVEL mas mais lento que
        # `gate_in_progress_ttl_seconds` (porém ainda dentro de
        # `timeout_seconds`) fazia outra instância considerar o lease
        # morto, convertê-lo para `UNKNOWN_OUTCOME`, e devolver
        # `TIMED_OUT_UNKNOWN_OUTCOME` — enquanto esta execução
        # terminava depois com sucesso e sobrescrevia para
        # `COMPLETED`, gerando estado falso/contraditório (duas
        # respostas diferentes para a mesma tarefa). Corrigido: uma
        # thread de heartbeat renova `leased_at` atomicamente
        # (condicionada ao `lease_token` ATUAL — nunca sobrescreve um
        # lease que outra instância genuinamente já assumiu) enquanto
        # a thread REAL do `task_runner` estiver viva — desde o
        # início da execução, não só após `timeout_seconds` expirar.
        # O heartbeat continua rodando mesmo DEPOIS do chamador
        # desistir de esperar (`gave_up_event`), enquanto a thread
        # real ainda não tiver terminado — cessa SOMENTE quando
        # `done_event` é sinalizado (a thread real terminou) ou o
        # processo morre (thread daemon).
        heartbeat_interval = max(0.02, self.gate_in_progress_ttl_seconds / 3.0)

        def _heartbeat_loop():
            # CORREÇÃO P1: mesma disciplina do heartbeat do gate —
            # (a) a thread NUNCA morre por uma exceção de renovação
            # (antes, uma exceção encerrava o loop silenciosamente e o
            # detentor vivo ficava desprotegido); (b) UMA conexão
            # SQLite é aberta para todo o ciclo e reutilizada — o
            # custo por batida deixa de ser 0,1–0,2s de setup de
            # conexão (causa medida da janela ≈ TTL) e volta a ser
            # milissegundos; (c) renova `leased_at` E `holder_seen_at`
            # — a conversão por TTL consulta a prova de liveness por
            # PID, então nem um atraso total do heartbeat converte um
            # detentor vivo. Encerra SOMENTE quando `done_event` é
            # sinalizado (a thread real terminou) ou o processo morre
            # (thread daemon).
            from core.models import db as db_module

            # CORREÇÃO F1: mesma disciplina do ramo do gate — em modo
            # memória (`db_path=None`) a renovação é feita no registro
            # em memória, sem tentar abrir conexão SQLite alguma.
            if not self.db_path:
                self._renew_lease_heartbeat(mission_id, task_id, token=lease_token)
                while not done_event.wait(timeout=heartbeat_interval):
                    self._renew_lease_heartbeat(mission_id, task_id, token=lease_token)
                return

            # conexão de vida longa via `get_connection` (crua — o
            # commit/close ficam sob nosso controle; ver nota no
            # heartbeat do gate).
            conn = None
            consecutive_failures = 0
            try:
                conn = db_module.get_connection(self.db_path)
            except Exception as exc:
                _logger.warning(
                    "graph_executor: heartbeat não conseguiu abrir conexão dedicada (task_id=%s): %s: %s — "
                    "cada batida tentará reconectar individualmente",
                    task_id, type(exc).__name__, exc,
                )
            stopped = False
            try:
                # PRIMEIRA BATIDA IMEDIATA: mesma disciplina do
                # heartbeat do gate (ver acima) — bate antes de esperar
                # o primeiro intervalo; cadencia periodica inalterada.
                self._renew_lease_heartbeat(mission_id, task_id, token=lease_token)
                while not done_event.wait(timeout=heartbeat_interval):
                    try:
                        if conn is None:
                            conn = db_module.get_connection(self.db_path)
                        now = time.time()
                        conn.execute(
                            "UPDATE graph_executor_task_leases SET leased_at = ?, holder_seen_at = ? "
                            "WHERE mission_id = ? AND task_id = ? AND lease_token = ? "
                            "AND status IN ('GATE_IN_PROGRESS', 'IN_PROGRESS')",
                            (now, now, mission_id, task_id, lease_token),
                        )
                        conn.commit()
                        consecutive_failures = 0
                    except Exception as exc:
                        consecutive_failures += 1
                        try:
                            if conn is not None:
                                conn.close()
                        except Exception:
                            pass
                        conn = None
                        _logger.warning(
                            "graph_executor: heartbeat não renovou (mission_id=%s, task_id=%s, falha "
                            "consecutiva #%s): %s: %s",
                            mission_id, task_id, consecutive_failures, type(exc).__name__, exc,
                        )
                # Encerramento normal: a thread real terminou — a
                # thread de heartbeat SAI DO LOOP e agora registra sua
                # saida limpa do registro de threads locais.
                stopped = True
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if stopped:
                    with self._local_threads_lock:
                        self._local_threads.pop(heartbeat_key, None)

        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop, name=f"graph-task-heartbeat-{task_id}", daemon=True,
        )
        with self._local_threads_lock:
            self._local_threads[heartbeat_key] = heartbeat_thread
        heartbeat_thread.start()

        finished_in_time = done_event.wait(timeout=timeout_seconds)
        if not finished_in_time:
            # estreita a janela de corrida: reconfirma antes de
            # desistir de vez — se terminou bem na borda do timeout,
            # usa o resultado real em vez de declarar desconhecido.
            if done_event.is_set():
                finished_in_time = True
        if not finished_in_time:
            gave_up_event.set()
            # CORREÇÃO: NUNCA para o heartbeat aqui — ele continua
            # renovando o lease enquanto a thread real (que já excedeu
            # `timeout_seconds`, mas ainda não terminou) permanecer
            # genuinamente viva. Só o PRÓPRIO `_heartbeat_loop` decide
            # parar, ao observar `done_event` sinalizado.
            return "TIMED_OUT_UNKNOWN_OUTCOME", None, f"timeout: tarefa não concluiu em {timeout_seconds}s"

        kind, payload = outcome_box[0]
        # SINCRONIZACAO DO HEARTBEAT: a thread de heartbeat sai do loop
        # no instante em que done_event e sinalizado (a thread real
        # terminou) e fecha sua conexao dedicada no `finally`. Este join
        # LIMITADO apenas garante que o fechamento aconteca ANTES do
        # retorno — no Windows, um handle SQLite vivo apos o retorno faz
        # o remove_db_file do tearDown falhar com WinError 32 (arquivo
        # em uso). Custo tipico: milissegundos (a thread ja esta saindo
        # quando o evento dispara). O caminho de timeout NAO tem join:
        # ali o contrato e o heartbeat continuar enquanto a thread real
        # viver.
        heartbeat_thread.join(timeout=0.5)
        return ("OK", payload, None) if kind == "ok" else ("ERROR", None, payload)

    @staticmethod
    def _evaluate_quality_gate(task: PlanTask, outcome: dict, quality_gate_checker: Callable | None) -> tuple:
        """Devolve `(gate_verdict, decision)` — `decision` é um de
        `"APPROVED"`/`"REJECTED"`/`"UNAVAILABLE"`.

        MICROCORREÇÃO CRÍTICA (fail-open — falha real reproduzida por
        auditoria independente): a versão anterior tratava "sem
        `quality_gate` aplicável" e "com `quality_gate` declarado mas
        `quality_gate_checker` AUSENTE nesta chamada" como o MESMO
        caso — ambos implicitamente aprovados. Reproduzido: uma
        retomada (nova instância, ou qualquer chamada) que esquecesse
        de passar `quality_gate_checker` aprovava silenciosamente uma
        tarefa cujo gate NUNCA foi genuinamente avaliado. Corrigido —
        os dois casos são DISTINTOS:
        - Sem `task.quality_gate` (nenhum gate aplicável): aprovado
          implicitamente (`gate_verdict=None`, `"APPROVED"`) — igual
          ao comportamento sempre existente de
          `MissionTracker.verify()` sem Quality Gate.
        - Com `task.quality_gate` definido MAS `quality_gate_checker`
          ausente nesta chamada: FAIL-CLOSED — `"UNAVAILABLE"`, NUNCA
          aprovado implicitamente. O chamador (`_run_exclusive_retry_loop`/
          `_finalize_pending_gate_outcome`) preserva o outcome para
          uma chamada futura com o checker disponível, nunca marca
          `COMPLETED`/`VERIFIED`, nunca reexecuta `task_runner`."""
        if task.quality_gate is None:
            return None, "APPROVED"
        if quality_gate_checker is None:
            return None, "UNAVAILABLE"
        approved = quality_gate_checker(task, outcome)
        decision = "APPROVED" if approved else "REJECTED"
        return decision, decision

