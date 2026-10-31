"""
atlas.radar.scheduler
========================
ATLAS+RADAR — correção residual (execução recorrente do Radar).

O Radar não pode depender apenas de chamada manual de
`AtlasDirector.scan_opportunities()`. `RadarScheduler` acopla o Radar à
infraestrutura de jobs JÁ EXISTENTE e já endurecida
(`core.jobs.queue.JobQueue`) — nenhum agendador novo, nenhum serviço
pago, nenhum dado inventado:

- Prevenção de execuções concorrentes duplicadas: cada ciclo devido
  vira um Job com idempotency_key determinística do ciclo
  (radar_scan:<bucket>). Duas chamadas simultâneas de run_if_due() —
  na mesma instância ou em instâncias diferentes — resolvem para o
  MESMO Job, e JobQueue.start() (já corrigido na Microcorreção 5, item
  3: reserva atômica de capacidade + versão) garante que só UMA delas
  realmente executa o scan.
- Sobrevivência a restart: tanto o estado do agendamento
  (RadarScheduleStore, tabela radar_schedule) quanto os Jobs do radar
  são persistidos via o MESMO db_path do resto do sistema.
- Nunca executa ação crítica: run_if_due() chama exclusivamente
  CommercialOpportunityRadar.scan() (leitura/descoberta) — nenhuma
  publicação, gasto, contratação, criação de conta ou conexão externa
  é sequer alcançável a partir daqui.
- Evidência: o scan agendado usa o MESMO CommercialOpportunityRadar do
  scan manual — dados MOCK continuam classificados UNKNOWN.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from atlas.radar.engine import CommercialOpportunityRadar
from core.jobs.queue import JobQueue
from core.models import db as db_module

_SCHEDULE_ID = "default"
_JOB_TYPE = "atlas_radar_scan"


class RadarScheduleDisabled(Exception):
    """Levantado ao tentar rodar um scan agendado com o agendamento
    desativado — chamador deve usar `AtlasDirector.scan_opportunities()`
    diretamente para uma varredura manual avulsa."""


@dataclass
class RadarScheduleState:
    schedule_id: str = _SCHEDULE_ID
    enabled: bool = False
    interval_seconds: float = 3600.0
    last_run_at: float | None = None
    last_run_status: str = "NEVER_RUN"
    next_run_at: float | None = None
    updated_at: float = field(default_factory=time.time)


class RadarScheduleStore:
    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._state = RadarScheduleState()
        if self._db_path:
            self._load_from_db()

    def _row_to_state(self, row) -> RadarScheduleState:
        return RadarScheduleState(
            schedule_id=row["schedule_id"], enabled=bool(row["enabled"]),
            interval_seconds=row["interval_seconds"], last_run_at=row["last_run_at"],
            last_run_status=row["last_run_status"], next_run_at=row["next_run_at"],
            updated_at=row["updated_at"],
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM radar_schedule WHERE schedule_id = ?", (_SCHEDULE_ID,)
            ).fetchone()
        if row is not None:
            self._state = self._row_to_state(row)

    def _persist(self, state: RadarScheduleState) -> None:
        if not self._db_path:
            return
        with db_module.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO radar_schedule "
                "(schedule_id, enabled, interval_seconds, last_run_at, last_run_status, next_run_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (state.schedule_id, int(state.enabled), state.interval_seconds, state.last_run_at,
                 state.last_run_status, state.next_run_at, state.updated_at),
            )

    def get(self) -> RadarScheduleState:
        if self._db_path:
            self._load_from_db()
        return self._state

    def set_enabled(self, enabled: bool, *, interval_seconds: float | None = None) -> RadarScheduleState:
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

    def record_run(self, *, status: str, now: float, next_run_at: float | None) -> RadarScheduleState:
        state = self.get()
        state.last_run_at = now
        state.last_run_status = status
        state.next_run_at = next_run_at
        state.updated_at = now
        self._state = state
        self._persist(state)
        return state


class RadarScheduler:
    def __init__(
        self, *, job_queue: JobQueue, radar: CommercialOpportunityRadar, db_path: str | None = None,
    ) -> None:
        self.job_queue = job_queue
        self.radar = radar
        self.store = RadarScheduleStore(db_path=db_path)

    def status(self) -> RadarScheduleState:
        return self.store.get()

    def enable(self, *, interval_seconds: float = 3600.0) -> RadarScheduleState:
        return self.store.set_enabled(True, interval_seconds=interval_seconds)

    def disable(self) -> RadarScheduleState:
        return self.store.set_enabled(False)

    @staticmethod
    def _cycle_key(now: float, interval_seconds: float) -> str:
        bucket = int(now // interval_seconds)
        return f"radar_scan:{bucket}"

    def run_if_due(self, *, niche: str | None = None, now: float | None = None):
        now = now if now is not None else time.time()
        state = self.store.get()
        if not state.enabled:
            return None
        if state.next_run_at is not None and now < state.next_run_at:
            return None

        cycle_key = self._cycle_key(now, state.interval_seconds)
        job = self.job_queue.enqueue(job_type=_JOB_TYPE, idempotency_key=cycle_key)
        # `try_start` (não `start`) — precisamos saber com certeza se
        # FOMOS NÓS que reservamos o slot, nunca apenas observar que o
        # job já está RUNNING por outra chamada/instância (o que
        # levaria a executar o mesmo scan duas vezes).
        started = self.job_queue.try_start(job.job_id)

        if started is None:
            # outra instância/chamada já está executando (ou já
            # completou) este ciclo, ou não há capacidade — nunca
            # duplica, apenas devolve o estado atual do job.
            return self.job_queue.get(job.job_id)

        try:
            opportunities = self.radar.scan(niche=niche)
            self.job_queue.complete(job.job_id, {"opportunities_found": len(opportunities)})
            self.store.record_run(status="SUCCESS", now=now, next_run_at=now + state.interval_seconds)
        except Exception as exc:
            self.job_queue.fail(job.job_id, str(exc))
            self.store.record_run(status="FAILED", now=now, next_run_at=now + state.interval_seconds)
            raise
        return self.job_queue.get(job.job_id)

    def force_run(self, *, niche: str | None = None):
        state = self.store.get()
        if not state.enabled:
            raise RadarScheduleDisabled("agendamento do radar está desativado")
        return self.run_if_due(niche=niche, now=time.time() if state.next_run_at is None else state.next_run_at)
