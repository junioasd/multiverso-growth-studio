"""
core.coordination.mission
=============================
EXPANSÃO — item 5 (coordenação multiagente).

Protocolo formal de delegação entre agentes. Cada `MissionAssignment`
carrega exatamente o necessário (evita envio desnecessário de contexto
e segredos — `context` passa por `sanitize_dict`) e nada além disso:
objetivo, contexto mínimo, permissões concedidas, ferramentas
permitidas, restrições, critérios de sucesso, prazo, dependências e
evidência esperada.

`MissionTracker` persiste (opcionalmente, via `db_path` — MESMO padrão
já estabelecido em `core/jobs/queue.py`/`atlas/escalation.py`) e
controla transições de estado com UPDATE condicionado a versão — nunca
uma atualização obsoleta sobrescreve uma transição já confirmada.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.models import db as db_module
from core.security.rbac import Permission, Principal, has_permission
from core.security.sanitize import sanitize_dict


class MissionStatus(str, Enum):
    QUEUED = "QUEUED"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    WAITING_OWNER = "WAITING_OWNER"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    VERIFIED = "VERIFIED"
    CANCELLED = "CANCELLED"


# CORREÇÃO (item 3 — state machine correta): só VERIFIED, FAILED e
# CANCELLED são terminais. COMPLETED NUNCA é terminal — representa
# execução concluída mas ainda pendente de VERIFICAÇÃO (critérios de
# sucesso + evidência + Quality Gate quando aplicável).
_TERMINAL_STATUSES = {MissionStatus.VERIFIED, MissionStatus.FAILED, MissionStatus.CANCELLED}

# CORREÇÃO: grafo explícito de transições permitidas — `transition()`
# agora VALIDA o destino, não apenas recusa mexer em estado terminal.
# QUEUED -> VERIFIED direto é estruturalmente IMPOSSÍVEL (não está no
# conjunto de QUEUED); COMPLETED -> VERIFIED é sempre permitido
# ("COMPLETED não deve impedir VERIFIED").
ALLOWED_TRANSITIONS: dict = {
    MissionStatus.QUEUED: {
        MissionStatus.PLANNED, MissionStatus.WAITING_DEPENDENCY, MissionStatus.WAITING_OWNER,
        MissionStatus.BLOCKED, MissionStatus.CANCELLED, MissionStatus.FAILED,
    },
    MissionStatus.PLANNED: {
        MissionStatus.RUNNING, MissionStatus.WAITING_DEPENDENCY, MissionStatus.WAITING_OWNER,
        MissionStatus.BLOCKED, MissionStatus.CANCELLED, MissionStatus.FAILED,
    },
    MissionStatus.RUNNING: {
        MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.RETRYING, MissionStatus.BLOCKED,
        MissionStatus.WAITING_DEPENDENCY, MissionStatus.WAITING_OWNER, MissionStatus.CANCELLED,
    },
    MissionStatus.WAITING_DEPENDENCY: {
        MissionStatus.PLANNED, MissionStatus.RUNNING, MissionStatus.QUEUED,
        MissionStatus.CANCELLED, MissionStatus.FAILED,
    },
    MissionStatus.WAITING_OWNER: {
        MissionStatus.PLANNED, MissionStatus.RUNNING, MissionStatus.QUEUED,
        MissionStatus.CANCELLED, MissionStatus.FAILED,
    },
    MissionStatus.BLOCKED: {
        MissionStatus.PLANNED, MissionStatus.RUNNING, MissionStatus.QUEUED,
        MissionStatus.CANCELLED, MissionStatus.FAILED,
    },
    MissionStatus.RETRYING: {MissionStatus.RUNNING, MissionStatus.FAILED, MissionStatus.CANCELLED},
    # COMPLETED -> VERIFIED é sempre permitido; COMPLETED -> FAILED cobre
    # o caso em que a VERIFICAÇÃO reprova o resultado (evidência
    # insuficiente, critério não atendido, Quality Gate reprovado).
    MissionStatus.COMPLETED: {MissionStatus.VERIFIED, MissionStatus.FAILED},
    MissionStatus.VERIFIED: set(),
    MissionStatus.FAILED: set(),
    MissionStatus.CANCELLED: set(),
}


class MissionNotFound(KeyError):
    pass


class InvalidMissionTransition(Exception):
    """Levantado ao tentar mudar o status de uma missão já em estado
    terminal (VERIFIED/FAILED/CANCELLED), ou para um destino fora do
    grafo de transições permitidas — nunca reverte um resultado já
    confirmado, e nunca pula etapa (ex: QUEUED -> VERIFIED direto)."""


class ConcurrentMissionUpdate(Exception):
    """Duas atualizações concorrentes para a mesma missão — nunca
    resolvida silenciosamente; o chamador deve reler e tentar de novo."""


class DependencyNotSatisfied(Exception):
    """CORREÇÃO (item 3 — enforcement real): uma tarefa não pode
    iniciar (`start_running`) se uma dependência obrigatória ainda não
    estiver VERIFIED."""


class ToolNotAllowed(Exception):
    """CORREÇÃO: uma ferramenta fora de `tools_allowed` é bloqueada —
    nunca silenciosamente permitida."""


class PermissionMissing(Exception):
    """CORREÇÃO: uma permissão exigida pela missão que o agente
    executor não possui impede a execução."""


class RestrictionViolated(Exception):
    """CORREÇÃO: uma restrição explícita da missão sendo violada pelo
    contexto de execução impede o início."""


@dataclass
class MissionAssignment:
    mission_id: str
    task_id: str
    agent: str
    objective: str
    context: dict = field(default_factory=dict)
    permissions: list = field(default_factory=list)
    tools_allowed: list = field(default_factory=list)
    restrictions: list = field(default_factory=list)
    success_criteria: list = field(default_factory=list)
    deadline: float | None = None
    dependencies: list = field(default_factory=list)
    evidence_expected: list = field(default_factory=list)
    status: MissionStatus = MissionStatus.QUEUED
    result: dict | None = None
    error: str | None = None
    version: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def new(
        *, agent: str, objective: str, mission_id: str | None = None, task_id: str | None = None, **kwargs,
    ) -> "MissionAssignment":
        return MissionAssignment(
            mission_id=mission_id or f"mis_{uuid.uuid4().hex[:12]}",
            task_id=task_id or f"task_{uuid.uuid4().hex[:12]}",
            agent=agent, objective=objective,
            context=sanitize_dict(kwargs.pop("context", {}) or {}),
            **kwargs,
        )


class MissionTracker:
    def __init__(self, *, db_path: str | None = None) -> None:
        self._missions: dict[tuple, MissionAssignment] = {}
        self._db_path = db_path
        self._lock = threading.RLock()
        if self._db_path:
            self._load_from_db()

    # ------------------------------------------------------------------
    # Persistência
    # ------------------------------------------------------------------

    def _row_to_mission(self, row: sqlite3.Row) -> MissionAssignment:
        return MissionAssignment(
            mission_id=row["mission_id"], task_id=row["task_id"], agent=row["agent"],
            objective=row["objective"], context=json.loads(row["context"]),
            permissions=json.loads(row["permissions"]), tools_allowed=json.loads(row["tools_allowed"]),
            restrictions=json.loads(row["restrictions"]), success_criteria=json.loads(row["success_criteria"]),
            deadline=row["deadline"], dependencies=json.loads(row["dependencies"]),
            evidence_expected=json.loads(row["evidence_expected"]), status=MissionStatus(row["status"]),
            result=json.loads(row["result"]) if row["result"] else None, error=row["error"],
            version=row["version"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM missions").fetchall():
                m = self._row_to_mission(row)
                self._missions[(m.mission_id, m.task_id)] = m

    @staticmethod
    def _insert_sql(conn: sqlite3.Connection, m: MissionAssignment) -> None:
        conn.execute(
            "INSERT INTO missions (mission_id, task_id, agent, objective, context, permissions, "
            " tools_allowed, restrictions, success_criteria, deadline, dependencies, evidence_expected, "
            " status, result, error, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.mission_id, m.task_id, m.agent, m.objective, json.dumps(m.context, default=str),
             json.dumps(m.permissions, default=str), json.dumps(m.tools_allowed, default=str),
             json.dumps(m.restrictions, default=str), json.dumps(m.success_criteria, default=str),
             m.deadline, json.dumps(m.dependencies, default=str), json.dumps(m.evidence_expected, default=str),
             m.status.value, json.dumps(m.result, default=str) if m.result is not None else None,
             m.error, m.version, m.created_at, m.updated_at),
        )

    def _fetch_from_db(self, mission_id: str, task_id: str) -> MissionAssignment | None:
        with db_module.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM missions WHERE mission_id=? AND task_id=?", (mission_id, task_id)
            ).fetchone()
        return self._row_to_mission(row) if row is not None else None

    # ------------------------------------------------------------------
    # Operações
    # ------------------------------------------------------------------

    def create(self, mission: MissionAssignment) -> MissionAssignment:
        with self._lock:
            key = (mission.mission_id, mission.task_id)
            if self._db_path:
                conn = db_module.get_autocommit_connection(self._db_path)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    self._insert_sql(conn, mission)
                    conn.commit()
                except sqlite3.IntegrityError:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    finally:
                        conn.close()
                    existing = self._fetch_from_db(*key)
                    if existing is None:
                        raise
                    return existing
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    finally:
                        conn.close()
                    raise
                else:
                    conn.close()
                    self._missions[key] = mission
                    return mission
            if key in self._missions:
                return self._missions[key]
            self._missions[key] = mission
            return mission

    def get(self, mission_id: str, task_id: str) -> MissionAssignment:
        if self._db_path:
            m = self._fetch_from_db(mission_id, task_id)
            if m is None:
                raise MissionNotFound((mission_id, task_id))
            self._missions[(mission_id, task_id)] = m
            return m
        key = (mission_id, task_id)
        if key not in self._missions:
            raise MissionNotFound(key)
        return self._missions[key]

    def transition(
        self, mission_id: str, task_id: str, *, status: MissionStatus, result: dict | None = None,
        error: str | None = None,
    ) -> MissionAssignment:
        with self._lock:
            current = self.get(mission_id, task_id)
            if current.status in _TERMINAL_STATUSES:
                raise InvalidMissionTransition(
                    f"missão ({mission_id}, {task_id}) já está em estado terminal "
                    f"({current.status.value}) — não pode ser transicionada de novo"
                )
            allowed = ALLOWED_TRANSITIONS.get(current.status, set())
            if status not in allowed:
                raise InvalidMissionTransition(
                    f"não é possível ir de '{current.status.value}' para '{status.value}' — "
                    f"transições permitidas a partir daqui: {[s.value for s in allowed]}"
                )

            new_version = current.version + 1
            updated_fields = dict(
                status=status, result=result if result is not None else current.result,
                error=error if error is not None else current.error,
                version=new_version, updated_at=time.time(),
            )

            if not self._db_path:
                for k, v in updated_fields.items():
                    setattr(current, k, v)
                return current

            conn = db_module.get_autocommit_connection(self._db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "UPDATE missions SET status=?, result=?, error=?, version=?, updated_at=? "
                    "WHERE mission_id=? AND task_id=? AND version=?",
                    (status.value, json.dumps(result, default=str) if result is not None else (
                        json.dumps(current.result, default=str) if current.result is not None else None
                    ), updated_fields["error"], new_version, updated_fields["updated_at"],
                     mission_id, task_id, current.version),
                )
                conn.commit()
                matched = cursor.rowcount > 0
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                finally:
                    conn.close()
                raise
            else:
                conn.close()

            if not matched:
                raise ConcurrentMissionUpdate(
                    f"missão ({mission_id}, {task_id}) mudou entre a leitura e a escrita — releia e tente de novo"
                )
            fresh = self._fetch_from_db(mission_id, task_id)
            self._missions[(mission_id, task_id)] = fresh
            return fresh

    # ------------------------------------------------------------------
    # CORREÇÃO (item 3 — enforcement real de dependências/permissões/
    # ferramentas/restrições)
    # ------------------------------------------------------------------

    def start_running(
        self, mission_id: str, task_id: str, *, requested_tools: list | None = None,
        agent_principal: Principal | None = None, context_flags: set | None = None,
    ) -> MissionAssignment:
        """Só transiciona para RUNNING depois de validar de verdade:
        dependências satisfeitas (VERIFIED), ferramentas solicitadas
        dentro de `tools_allowed`, permissões exigidas presentes no
        agente executor, e nenhuma restrição violada pelo contexto de
        execução."""
        current = self.get(mission_id, task_id)

        for dep_mission_id in current.dependencies:
            dep_tasks = self.list_by_mission(dep_mission_id)
            if not dep_tasks or any(t.status != MissionStatus.VERIFIED for t in dep_tasks):
                raise DependencyNotSatisfied(
                    f"dependência '{dep_mission_id}' ainda não está VERIFIED — "
                    f"missão ({mission_id}, {task_id}) não pode iniciar"
                )

        if requested_tools and current.tools_allowed:
            not_allowed = [t for t in requested_tools if t not in current.tools_allowed]
            if not_allowed:
                raise ToolNotAllowed(f"ferramenta(s) não permitida(s) para esta missão: {not_allowed}")

        if agent_principal is not None and current.permissions:
            for perm_name in current.permissions:
                try:
                    perm = Permission[perm_name]
                except KeyError:
                    raise PermissionMissing(f"permissão desconhecida exigida pela missão: '{perm_name}'")
                if not has_permission(agent_principal, perm):
                    raise PermissionMissing(
                        f"agente '{agent_principal.subject}' não possui a permissão exigida: '{perm_name}'"
                    )

        if context_flags and current.restrictions:
            violated = set(context_flags) & set(current.restrictions)
            if violated:
                raise RestrictionViolated(f"restrições violadas ao iniciar a missão: {sorted(violated)}")

        return self.transition(mission_id, task_id, status=MissionStatus.RUNNING)

    def verify(
        self, mission_id: str, task_id: str, *, evidence: dict, quality_gate_verdict: str | None = None,
    ) -> MissionAssignment:
        """CORREÇÃO: VERIFIED exige execução concluída (só alcançável a
        partir de COMPLETED — garantido pelo grafo), critérios de
        sucesso confirmados, evidência não vazia, e Quality Gate
        aprovado quando informado. Nunca declara VERIFIED sem prova."""
        current = self.get(mission_id, task_id)
        if current.status != MissionStatus.COMPLETED:
            raise InvalidMissionTransition(
                f"verify() exige a missão em COMPLETED — estado atual: '{current.status.value}'"
            )
        if not evidence:
            raise ValueError("verificação exige evidência — nunca declarar sucesso sem prova")
        if current.success_criteria and not evidence.get("success_criteria_met"):
            raise ValueError(
                "esta missão define success_criteria, mas a evidência não confirma "
                "'success_criteria_met' — verificação recusada"
            )
        if quality_gate_verdict is not None and quality_gate_verdict != "APPROVED":
            raise InvalidMissionTransition(
                f"Quality Gate não aprovado ('{quality_gate_verdict}') — não é possível VERIFICAR"
            )
        merged_result = {**(current.result or {}), "verification_evidence": evidence}
        return self.transition(mission_id, task_id, status=MissionStatus.VERIFIED, result=merged_result)

    def list_by_status(self, status: MissionStatus) -> list:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM missions WHERE status=?", (status.value,)).fetchall()
            return [self._row_to_mission(r) for r in rows]
        return [m for m in self._missions.values() if m.status == status]

    def list_by_mission(self, mission_id: str) -> list:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchall()
            return [self._row_to_mission(r) for r in rows]
        return [m for m in self._missions.values() if m.mission_id == mission_id]

    def all(self) -> list:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM missions").fetchall()
            return [self._row_to_mission(r) for r in rows]
        return list(self._missions.values())
