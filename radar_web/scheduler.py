"""
radar_web.scheduler
=======================
CORREÇÃO (item 3 — RADAR WEB COMERCIAL: execução contínua real).

Mesmo padrão já hardened do RADAR DE OPORTUNIDADES
(`atlas.radar.scheduler`/`atlas.radar.worker`) — reaproveita a MESMA
infraestrutura de jobs (`core.jobs.queue.JobQueue`,
`JobQueue.try_start()`, já corrigido/testado) — mas com tabela e
`job_type` PRÓPRIOS (`radar_web_schedule`/`radar_web_scan`),
SEPARADOS do RADAR DE OPORTUNIDADES: responsabilidades e estados
distintos, nunca compartilham a mesma linha de agendamento.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.jobs.queue import JobQueue
from core.models import db as db_module
from radar_web.engine import RadarWebComercial

_SCHEDULE_ID = "default"
_JOB_TYPE = "radar_web_scan"


class RadarWebScheduleDisabled(Exception):
    pass


@dataclass
class RadarWebScheduleState:
    schedule_id: str = _SCHEDULE_ID
    enabled: bool = False
    interval_seconds: float = 3600.0
    last_run_at: float | None = None
    last_run_status: str = "NEVER_RUN"
    last_run_error: str | None = None
    last_run_duration: float | None = None
    last_run_discoveries: int = 0
    next_run_at: float | None = None
    updated_at: float = field(default_factory=time.time)


class RadarWebScheduleStore:
    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._state = RadarWebScheduleState()
        if self._db_path:
            self._load_from_db()

    def _row_to_state(self, row) -> RadarWebScheduleState:
        return RadarWebScheduleState(
            schedule_id=row["schedule_id"], enabled=bool(row["enabled"]),
            interval_seconds=row["interval_seconds"], last_run_at=row["last_run_at"],
            last_run_status=row["last_run_status"], last_run_error=row["last_run_error"],
            last_run_duration=row["last_run_duration"], last_run_discoveries=row["last_run_discoveries"],
            next_run_at=row["next_run_at"], updated_at=row["updated_at"],
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM radar_web_schedule WHERE schedule_id = ?", (_SCHEDULE_ID,)
            ).fetchone()
        if row is not None:
            self._state = self._row_to_state(row)

    def _persist(self, state: RadarWebScheduleState) -> None:
        if not self._db_path:
            return
        with db_module.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO radar_web_schedule "
                "(schedule_id, enabled, interval_seconds, last_run_at, last_run_status, last_run_error, "
                " last_run_duration, last_run_discoveries, next_run_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (state.schedule_id, int(state.enabled), state.interval_seconds, state.last_run_at,
                 state.last_run_status, state.last_run_error, state.last_run_duration,
                 state.last_run_discoveries, state.next_run_at, state.updated_at),
            )

    def get(self) -> RadarWebScheduleState:
        if self._db_path:
            self._load_from_db()
        return self._state

    def set_enabled(self, enabled: bool, *, interval_seconds: float | None = None) -> RadarWebScheduleState:
        state = self.get()
        state.enabled = enabled
        if interval_seconds is not None:
            if interval_seconds <= 0:
                raise ValueError("interval_seconds precisa ser positivo")
            state.interval_seconds = interval_seconds
        state.updated_at = time.time()
        if enabled and state.next_run_at is None:
            state.next_run_at = state.updated_at
        if not enabled:
            state.next_run_at = None
        self._state = state
        self._persist(state)
        return state

    def record_run(
        self, *, status: str, now: float, next_run_at: float | None, error: str | None = None,
        duration: float | None = None, discoveries: int = 0,
    ) -> RadarWebScheduleState:
        state = self.get()
        state.last_run_at = now
        state.last_run_status = status
        state.last_run_error = error
        state.last_run_duration = duration
        state.last_run_discoveries = discoveries
        state.next_run_at = next_run_at
        state.updated_at = now
        self._state = state
        self._persist(state)
        return state


class RadarWebScheduler:
    def __init__(
        self, *, job_queue: JobQueue, radar: RadarWebComercial, db_path: str | None = None,
    ) -> None:
        self.job_queue = job_queue
        self.radar = radar
        self.store = RadarWebScheduleStore(db_path=db_path)

    def status(self) -> RadarWebScheduleState:
        return self.store.get()

    def enable(self, *, interval_seconds: float = 3600.0) -> RadarWebScheduleState:
        return self.store.set_enabled(True, interval_seconds=interval_seconds)

    def disable(self) -> RadarWebScheduleState:
        return self.store.set_enabled(False)

    @staticmethod
    def _cycle_key(now: float, interval_seconds: float) -> str:
        bucket = int(now // interval_seconds)
        return f"radar_web_scan:{bucket}"

    def run_if_due(self, *, niche: str | None = None, now: float | None = None):
        """Execução AUTOMÁTICA (chamada pelo worker) — respeita
        `next_run_at`. Nunca duplica: usa `JobQueue.try_start()`
        (retorno inequívoco — só `None` quando a chamada NÃO venceu a
        reserva, nunca confundido com "já rodando por outra
        instância")."""
        now = now if now is not None else time.time()
        state = self.store.get()
        if not state.enabled:
            return None
        if state.next_run_at is not None and now < state.next_run_at:
            return None
        return self._execute_cycle(niche=niche, now=now, state=state)

    def run_manual(self, *, niche: str | None = None):
        """CORREÇÃO (item 3 — execução manual sem duplicar a
        automática): usa o MESMO ciclo/`cycle_key` do agendamento
        automático corrente — se uma execução automática já reservou
        este ciclo, a chamada manual apenas observa o resultado
        (nunca dispara uma segunda execução real para o mesmo
        ciclo)."""
        now = time.time()
        state = self.store.get()
        return self._execute_cycle(niche=niche, now=now, state=state, manual=True)

    def _execute_cycle(self, *, niche, now, state, manual: bool = False):
        from core.models.entities import JobState

        interval = state.interval_seconds if state.interval_seconds > 0 else 3600.0
        cycle_key = self._cycle_key(now, interval)
        job = self.job_queue.enqueue(job_type=_JOB_TYPE, idempotency_key=cycle_key)

        # CORREÇÃO (item 3 — execução manual nunca duplica a
        # automática): se este ciclo (mesmo `cycle_key`/idempotency)
        # já foi concluído ou falhou, o job devolvido por `enqueue()`
        # já reflete isso — nunca tenta reiniciá-lo. Necessário aqui
        # porque `JobQueue.try_start()` em modo memória (sem
        # `db_path`) só protege contra concorrência RUNNING, não
        # contra reiniciar um job já terminal — checagem adicional
        # feita nesta camada, sem alterar o `JobQueue` já aprovado.
        if job.state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
            return job

        started = self.job_queue.try_start(job.job_id)

        if started is None:
            return self.job_queue.get(job.job_id)

        start_time = time.time()
        try:
            opportunities = self.radar.discover(niche=niche)
            duration = time.time() - start_time
            self.job_queue.complete(job.job_id, {"discoveries": len(opportunities)})
            self.store.record_run(
                status="SUCCESS", now=now, next_run_at=now + interval, duration=duration,
                discoveries=len(opportunities),
            )
        except Exception as exc:
            duration = time.time() - start_time
            self.job_queue.fail(job.job_id, str(exc))
            self.store.record_run(
                status="FAILED", now=now, next_run_at=now + interval, error=str(exc), duration=duration,
            )
            raise
        return self.job_queue.get(job.job_id)
