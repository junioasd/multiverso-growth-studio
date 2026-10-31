"""
core.jobs.queue
================
Job Queue (Seção 56). Implementa:
- idempotência com escopo (por canal/projeto/tipo de operação)
- retry com backoff
- timeout
- capacity control (limite de jobs RUNNING simultâneos)
- circuit breaker conceitual por job_type
- safe recovery (jobs travados em RUNNING além do timeout voltam para RETRYING)

MICROCORREÇÃO 5 (auditoria — reprovado na 4ª rodada):

- item 2 (FALHAS NO COMMIT): todo caminho persistente agora faz BEGIN +
  gravação(ões) + COMMIT dentro do MESMO try/except — uma falha no
  commit em si sempre aciona rollback + close, nunca deixa transação
  aberta. Erro de limpeza nunca mascara o erro original.

- item 3 (ATUALIZAÇÕES E CAPACIDADE ATÔMICAS ENTRE INSTÂNCIAS):
  `start()` verifica capacidade, reserva a vaga e grava a transição
  para RUNNING em UM ÚNICO UPDATE condicional (checagem de capacidade
  via subquery na própria cláusula WHERE) — nunca mais "checar depois
  escrever" como dois passos separados. Duas instâncias concorrentes
  nunca conseguem exceder `max_concurrent_running`, mesmo com conexões
  independentes de verdade (não depende só de lock em memória).
  `complete`/`fail`/`cancel` usam UPDATE condicionado à versão lida
  (`WHERE job_id=? AND version=?`), com retry re-lendo o estado fresco
  em caso de conflito — e recusam agir sobre um job que já está em
  estado terminal (COMPLETED/FAILED/CANCELLED), impedindo que uma
  atualização obsoleta reverta um resultado já confirmado.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from core.governance.idempotency import IdempotencyConflict, stable_fingerprint
from core.models import db as db_module
from core.models.entities import Job, JobState, new_id

_DOMAIN = "job"
_TERMINAL_STATES = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
_STARTABLE_STATES = {JobState.QUEUED, JobState.WAITING, JobState.RETRYING}
_MAX_UPDATE_RETRIES = 5


class CircuitOpenError(Exception):
    pass


class JobNotFound(KeyError):
    pass


class InvalidJobTransition(Exception):
    """Levantado quando se tenta mutar um job que já está em estado
    terminal — nunca reverte um resultado já confirmado."""


class ConcurrentModificationError(Exception):
    """Contenção muito alta: uma atualização condicional não convergiu
    após várias tentativas."""


def _payload_fingerprint(payload: dict | None) -> str:
    try:
        return json.dumps(payload or {}, sort_keys=True, default=str)
    except TypeError:
        return repr(payload)


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_after_seconds: float = 60.0
    _failures: int = 0
    _opened_at: float | None = None

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.time()

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.time() - self._opened_at >= self.reset_after_seconds:
            self._opened_at = None
            self._failures = 0
            return False
        return True


class JobQueue:
    def __init__(
        self, *, max_concurrent_running: int = 10, default_timeout_seconds: float = 300.0,
        db_path: str | None = None,
    ):
        self._jobs: dict[str, Job] = {}
        self._idempotency_index: dict[tuple, tuple[str, str]] = {}
        self._running_started_at: dict[str, float] = {}
        self.max_concurrent_running = max_concurrent_running
        self.default_timeout_seconds = default_timeout_seconds
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.RLock()
        self._db_path = db_path
        if self._db_path:
            self._load_from_db()

    # ------------------------------------------------------------------
    # Persistência / leitura
    # ------------------------------------------------------------------

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM jobs").fetchall():
                job = self._row_to_job(row)
                self._jobs[job.job_id] = job
                if job.state == JobState.RUNNING:
                    self._running_started_at[job.job_id] = job.updated_at
            for row in conn.execute(
                "SELECT * FROM idempotency_records WHERE domain = ?", (_DOMAIN,)
            ).fetchall():
                key = (row["channel_id"], row["project_id"], row["idempotency_key"], row["operation"])
                self._idempotency_index[key] = (row["record_id"], row["fingerprint"])

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"], job_type=row["job_type"], idempotency_key=row["idempotency_key"] or "",
            project_id=row["project_id"], channel_id=row["channel_id"], state=JobState(row["state"]),
            attempts=row["attempts"], max_attempts=row["max_attempts"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"], version=row["version"] if "version" in row.keys() else 0,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _insert_job_sql(conn: sqlite3.Connection, job: Job) -> None:
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, project_id, channel_id, job_type, state, idempotency_key, attempts, max_attempts, "
            " payload, result, error, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.job_id, job.project_id, job.channel_id, job.job_type, job.state.value, job.idempotency_key,
             job.attempts, job.max_attempts, json.dumps(job.payload, default=str),
             json.dumps(job.result, default=str) if job.result is not None else None,
             job.error, job.version, job.created_at, job.updated_at),
        )

    def _fetch_job_from_db(self, job_id: str) -> Job | None:
        with db_module.connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row is not None else None

    def _get_current(self, job_id: str) -> Job:
        if self._db_path:
            job = self._fetch_job_from_db(job_id)
            if job is None:
                raise JobNotFound(job_id)
            return job
        if job_id not in self._jobs:
            raise JobNotFound(job_id)
        return self._jobs[job_id]

    # ------------------------------------------------------------------
    # Operações
    # ------------------------------------------------------------------

    def _breaker_for(self, job_type: str) -> CircuitBreaker:
        return self._breakers.setdefault(job_type, CircuitBreaker())

    def enqueue(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict | None = None,
        project_id: str | None = None,
        channel_id: str | None = None,
        max_attempts: int = 3,
    ) -> Job:
        fingerprint = stable_fingerprint(job_type, channel_id, project_id, _payload_fingerprint(payload))
        job = Job.create(
            job_type, idempotency_key, payload=payload or {}, project_id=project_id, channel_id=channel_id,
            max_attempts=max_attempts, state=JobState.QUEUED,
        )

        with self._lock:
            index_key = (channel_id or "", project_id or "", idempotency_key, job_type)

            if self._db_path:
                return self._enqueue_durable(index_key, job, fingerprint)

            existing = self._idempotency_index.get(index_key)
            if existing is not None:
                existing_id, existing_fp = existing
                if existing_fp != fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency_key '{idempotency_key}' já usada neste escopo "
                        f"(canal={channel_id}, projeto={project_id}, tipo={job_type}) "
                        "com payload diferente — reutilização incompatível recusada"
                    )
                return self._jobs[existing_id]

            self._jobs[job.job_id] = job
            self._idempotency_index[index_key] = (job.job_id, fingerprint)
            return job

    def _enqueue_durable(self, index_key: tuple, job: Job, fingerprint: str) -> Job:
        channel_id, project_id, idempotency_key, job_type = index_key
        conn = db_module.get_autocommit_connection(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_job_sql(conn, job)
            conn.execute(
                "INSERT INTO idempotency_records "
                "(domain, operation, channel_id, project_id, idempotency_key, record_id, fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_DOMAIN, job_type, channel_id, project_id, idempotency_key, job.job_id, fingerprint, time.time()),
            )
            conn.commit()  # item 2: commit DENTRO do try
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            finally:
                conn.close()
            return self._resolve_job_conflict(index_key, fingerprint)
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
            self._jobs[job.job_id] = job
            self._idempotency_index[index_key] = (job.job_id, fingerprint)
            return job

    def _resolve_job_conflict(self, index_key: tuple, fingerprint: str) -> Job:
        channel_id, project_id, idempotency_key, job_type = index_key
        row_conn = db_module.get_connection(self._db_path)
        try:
            row = row_conn.execute(
                "SELECT record_id, fingerprint FROM idempotency_records "
                "WHERE domain=? AND operation=? AND channel_id=? AND project_id=? AND idempotency_key=?",
                (_DOMAIN, job_type, channel_id, project_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise IdempotencyConflict(
                    f"idempotency_key já usada neste escopo (canal={channel_id}, projeto={project_id}, "
                    f"tipo={job_type}) mas o registro correspondente não foi encontrado — abortando com segurança"
                )
            existing_job_id, existing_fp = row["record_id"], row["fingerprint"]
            if existing_fp != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency_key já usada neste escopo (canal={channel_id}, projeto={project_id}, "
                    f"tipo={job_type}) com payload diferente — reutilização incompatível recusada"
                )
            existing_row = row_conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (existing_job_id,)
            ).fetchone()
        finally:
            row_conn.close()
        existing_job = self._row_to_job(existing_row)
        self._jobs[existing_job.job_id] = existing_job
        if existing_job.state == JobState.RUNNING and existing_job.job_id not in self._running_started_at:
            self._running_started_at[existing_job.job_id] = existing_job.updated_at
        self._idempotency_index[index_key] = (existing_job_id, existing_fp)
        return existing_job

    def _count_running(self) -> int:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as n FROM jobs WHERE state = ?", (JobState.RUNNING.value,)
                ).fetchone()
            return row["n"]
        return sum(1 for j in self._jobs.values() if j.state == JobState.RUNNING)

    # ------------------------------------------------------------------
    # start(): checar capacidade + reservar + transicionar, atomicamente
    # ------------------------------------------------------------------

    def _try_atomic_start(self, current: Job) -> Job | None:
        """UM ÚNICO UPDATE que checa capacidade (subquery no WHERE),
        checa versão (evita corrida com outra transição concorrente) e
        checa estado (só parte de estados iniciáveis) — tudo avaliado
        atomicamente pelo SQLite dentro de uma única transação de
        escrita. `cursor.rowcount == 0` cobre TODOS os motivos de falha
        (capacidade cheia, versão mudou, estado não iniciável) sem
        distinguir qual — o chamador relê o estado fresco para decidir
        o que fazer a seguir."""
        new_attempts = current.attempts + 1
        new_updated_at = time.time()
        new_version = current.version + 1
        conn = db_module.get_autocommit_connection(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" * len(_STARTABLE_STATES))
            cursor = conn.execute(
                f"UPDATE jobs SET state=?, attempts=?, updated_at=?, version=? "
                f"WHERE job_id=? AND version=? AND state IN ({placeholders}) "
                f"AND (SELECT COUNT(*) FROM jobs WHERE state=?) < ?",
                (
                    JobState.RUNNING.value, new_attempts, new_updated_at, new_version,
                    current.job_id, current.version,
                    *[s.value for s in _STARTABLE_STATES],
                    JobState.RUNNING.value, self.max_concurrent_running,
                ),
            )
            conn.commit()
            matched = cursor.rowcount > 0
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

        if not matched:
            return None
        updated = dataclasses.replace(
            current, state=JobState.RUNNING, attempts=new_attempts, updated_at=new_updated_at, version=new_version,
        )
        self._jobs[updated.job_id] = updated
        return updated

    def try_start(self, job_id: str) -> Job | None:
        """Como `start()`, mas com retorno INEQUÍVOCO: só devolve um
        `Job` quando ESTA CHAMADA especificamente conseguiu reservar a
        vaga e transicionar para RUNNING — nunca o estado meramente
        OBSERVADO (já RUNNING por outra chamada/instância). Devolve
        `None` quando não conseguiu (capacidade cheia, versão mudou,
        circuit breaker aberto, ou o job não está mais em estado
        iniciável) — sem marcar WAITING nem mutar nada.

        Existe porque `start()` (acima) devolve o estado FRESCO do job
        mesmo quando NÃO foi esta chamada que o iniciou — correto para
        quem só quer saber "qual é o estado atual", mas perigoso para
        quem precisa saber "fui EU que ganhei a corrida" (ex:
        `atlas.radar.scheduler.RadarScheduler`, que só deve executar o
        trabalho quando genuinamente reservou o slot — tratar um
        RUNNING meramente observado como reserva própria levaria a
        executar o mesmo trabalho duas vezes)."""
        with self._lock:
            current = self._get_current(job_id)
            breaker = self._breaker_for(current.job_type)
            if breaker.is_open():
                return None

            if not self._db_path:
                # MICROCORREÇÃO (ATLAS EXPANSION 1 — proteção
                # multi-instância — falha real reproduzida por
                # auditoria independente): o caminho em memória
                # transicionava para RUNNING INCONDICIONALMENTE,
                # nunca checando se `current.state` já era um estado
                # iniciável (`_STARTABLE_STATES`) — diferente do
                # caminho durável (`_try_atomic_start`, abaixo), que
                # sempre validou isso. Um job já `RUNNING` podia ser
                # "reiniciado" por uma segunda chamada de
                # `try_start()`, incrementando `attempts` de novo sem
                # nenhuma proteção — abrindo a porta para duas
                # instâncias (ou duas chamadas) tratarem a MESMA
                # tarefa como "iniciada por mim" simultaneamente.
                # Corrigido: mesma checagem do caminho durável.
                if current.state not in _STARTABLE_STATES:
                    return None
                if self._count_running() >= self.max_concurrent_running:
                    return None
                updated = dataclasses.replace(
                    current, state=JobState.RUNNING, attempts=current.attempts + 1, updated_at=time.time(),
                )
                self._jobs[updated.job_id] = updated
                self._running_started_at[job_id] = time.time()
                return updated

            updated = self._try_atomic_start(current)
            if updated is not None:
                self._running_started_at[job_id] = time.time()
            return updated

    def start(self, job_id: str) -> Job:
        with self._lock:
            current = self._get_current(job_id)
            breaker = self._breaker_for(current.job_type)

            if breaker.is_open():
                return self._conditional_job_update(
                    job_id, target_state=JobState.WAITING, error="circuit breaker aberto para este job_type",
                    allow_from_terminal=False,
                )

            if not self._db_path:
                # modo em memória: self._lock já serializa totalmente.
                if self._count_running() >= self.max_concurrent_running:
                    updated = dataclasses.replace(current, state=JobState.WAITING, updated_at=time.time())
                    self._jobs[updated.job_id] = updated
                    return updated
                updated = dataclasses.replace(
                    current, state=JobState.RUNNING, attempts=current.attempts + 1, updated_at=time.time(),
                )
                self._jobs[updated.job_id] = updated
                self._running_started_at[job_id] = time.time()
                return updated

            # item 3: tenta a reserva atômica; se não conseguir (capacidade
            # cheia OU alguém já mudou o job), relê o estado fresco.
            updated = self._try_atomic_start(current)
            if updated is not None:
                self._running_started_at[job_id] = time.time()
                return updated

            fresh = self._get_current(job_id)
            if fresh.state not in _STARTABLE_STATES:
                # outra instância já assumiu o job (ou ele já avançou/
                # terminou) — devolve o estado atual, não sobrescreve.
                return fresh
            # ainda iniciável mas sem vaga no momento — marca WAITING
            return self._conditional_job_update(job_id, target_state=JobState.WAITING, allow_from_terminal=False)

    # ------------------------------------------------------------------
    # complete/fail/cancel: UPDATE condicionado à versão, com proteção
    # de estado terminal
    # ------------------------------------------------------------------

    def _write_job_conditional(self, job: Job, *, expected_version: int) -> bool:
        conn = db_module.get_autocommit_connection(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE jobs SET state=?, result=?, error=?, attempts=?, updated_at=?, version=? "
                "WHERE job_id=? AND version=?",
                (job.state.value, json.dumps(job.result, default=str) if job.result is not None else None,
                 job.error, job.attempts, job.updated_at, job.version, job.job_id, expected_version),
            )
            conn.commit()  # item 2: commit DENTRO do try
            return cursor.rowcount > 0
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _conditional_job_update(
        self, job_id: str, *, target_state: JobState, allow_from_terminal: bool, result=None, error=None,
    ) -> Job:
        current = self._get_current(job_id)
        for _ in range(_MAX_UPDATE_RETRIES):
            if not allow_from_terminal and current.state in _TERMINAL_STATES:
                raise InvalidJobTransition(
                    f"job {job_id} já está em estado terminal ({current.state.value}) — "
                    "atualização recusada para não reverter um resultado já confirmado"
                )

            updated = dataclasses.replace(
                current, state=target_state, version=current.version + 1, updated_at=time.time(),
                result=result if result is not None else current.result,
                error=error if error is not None else current.error,
            )

            if self._db_path:
                success = self._write_job_conditional(updated, expected_version=current.version)
                if not success:
                    current = self._get_current(job_id)  # outra instância mudou — relê e tenta de novo
                    continue

            self._jobs[updated.job_id] = updated
            return updated

        raise ConcurrentModificationError(
            f"não foi possível atualizar o job {job_id} após {_MAX_UPDATE_RETRIES} tentativas "
            "— alta contenção entre instâncias concorrentes"
        )

    def complete(self, job_id: str, result: dict) -> Job:
        with self._lock:
            applied = self._conditional_job_update(
                job_id, target_state=JobState.COMPLETED, allow_from_terminal=False, result=result,
            )
            self._running_started_at.pop(job_id, None)
            self._breaker_for(applied.job_type).record_success()
            return applied

    def fail(self, job_id: str, error: str) -> Job:
        with self._lock:
            current = self._get_current(job_id)
            new_state = JobState.FAILED if current.attempts >= current.max_attempts else JobState.RETRYING
            applied = self._conditional_job_update(
                job_id, target_state=new_state, allow_from_terminal=False, error=error,
            )
            self._running_started_at.pop(job_id, None)
            self._breaker_for(applied.job_type).record_failure()
            return applied

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            applied = self._conditional_job_update(job_id, target_state=JobState.CANCELLED, allow_from_terminal=False)
            self._running_started_at.pop(job_id, None)
            return applied

    def recover_stuck_jobs(self, timeout_seconds: float | None = None) -> list[Job]:
        """Safe recovery: jobs RUNNING além do timeout voltam para RETRYING/FAILED."""
        with self._lock:
            # COMANDO 3/6: `or` tratava 0.0 ("recuperar agora") como ausente e
            # aplicava silenciosamente o padrão de 300s.
            timeout = self.default_timeout_seconds if timeout_seconds is None else timeout_seconds
            now = time.time()
            recovered = []
            for job_id, started_at in list(self._running_started_at.items()):
                if now - started_at > timeout:
                    try:
                        job = self.fail(job_id, "timeout: job travado em RUNNING")
                        recovered.append(job)
                    except InvalidJobTransition:
                        pass  # já terminou por outro caminho nesse meio-tempo — nada a fazer
            return recovered

    def get(self, job_id: str) -> Job:
        if self._db_path:
            job = self._fetch_job_from_db(job_id)
            if job is None:
                raise JobNotFound(job_id)
            self._jobs[job.job_id] = job
            return job
        return self._jobs[job_id]

    def by_idempotency_key(
        self, key: str, *, job_type: str, channel_id: str | None = None, project_id: str | None = None
    ) -> Job | None:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT record_id FROM idempotency_records "
                    "WHERE domain=? AND operation=? AND channel_id=? AND project_id=? AND idempotency_key=?",
                    (_DOMAIN, job_type, channel_id or "", project_id or "", key),
                ).fetchone()
            if row is None:
                return None
            return self.get(row["record_id"])
        index_key = (channel_id or "", project_id or "", key, job_type)
        existing = self._idempotency_index.get(index_key)
        return self._jobs.get(existing[0]) if existing else None

    def all(self) -> list[Job]:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM jobs").fetchall()
            return [self._row_to_job(r) for r in rows]
        return list(self._jobs.values())
