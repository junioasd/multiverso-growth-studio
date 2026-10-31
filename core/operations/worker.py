"""
core.operations.worker — worker EXPLÍCITO da JobQueue (`python main.py worker`).
Ciclo: heartbeat → recuperação de jobs travados → (kill switch?) → drenagem
dos tipos conhecidos (ticks de planos do NAVIGATOR e observações do RADAR).
Não duplica: `JobQueue.try_start` é atômico entre processos e as ações externas
passam pelo claim anti-replay do NavigatorReal. Cada job processado é auditado.
"""
from __future__ import annotations

import logging
import os
import socket
import time

from core.models import db as db_module
from core.operations import kill_switch

log = logging.getLogger("growth_studio.worker")


def heartbeat(db_path: str, worker_id: str) -> None:
    """Persist heartbeat with bounded retries on transient SQLite writer contention."""
    import sqlite3

    for attempt in range(3):
        try:
            with db_module.connect(db_path) as conn:
                conn.execute("INSERT INTO worker_heartbeats (worker_id, host, pid, at) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(worker_id) DO UPDATE SET host=excluded.host, pid=excluded.pid, at=excluded.at",
                             (worker_id, socket.gethostname(), os.getpid(), time.time()))
            return
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or attempt == 2:
                raise
            log.warning("heartbeat SQLite contention (worker=%s, attempt=%s/3)", worker_id, attempt + 1)
            time.sleep(0.15 * (attempt + 1))


def last_heartbeat(db_path: str) -> float | None:
    with db_module.connect(db_path) as conn:
        return conn.execute("SELECT MAX(at) FROM worker_heartbeats").fetchone()[0]


def run_worker_cycle(*, db_path: str, runner, director=None, audit=None, worker_id: str = "worker-1",
                     stuck_job_seconds: float = 300.0) -> dict:
    from core.jobs.queue import JobQueue
    from navigator.planning import process_advance_jobs

    heartbeat(db_path, worker_id)
    queue = JobQueue(db_path=db_path)
    recovered = [j.job_id for j in queue.recover_stuck_jobs(timeout_seconds=stuck_job_seconds)]
    blocked = kill_switch.block_reason(db_path)
    if blocked:
        log.warning("ciclo do worker sem drenagem: %s", blocked)
        return {"recovered": recovered, "processed": [], "blocked": kill_switch.BLOCK_CODE}
    processed = list(process_advance_jobs(queue, runner))
    if director is not None:
        from radar_web.intelligence import process_observation_jobs
        from atlas.gateway_worker import AtlasGatewayWorker

        processed += list(process_observation_jobs(queue, director))

        gateway_worker = AtlasGatewayWorker(
            job_queue=queue,
            atlas=director,
        )
        for job in list(queue.all()):
            if job.job_type != "ATLAS_TASK":
                continue
            if getattr(job.state, "value", str(job.state)) not in ("PENDING", "QUEUED", "RETRYING"):
                continue
            started = queue.try_start(job.job_id)
            if started is None:
                continue
            gateway_worker._execute(started)
            processed.append((job.job_id, "ATLAS_TASK"))
    for item in processed:
        log.info("job processado: %s", item)
        if audit is not None:
            audit.record(actor=worker_id, action="worker_job_processed", resource=str(item[0]), outcome="SUCCESS",
                         details={"result": str(item[1])})
    return {"recovered": recovered, "processed": processed, "blocked": None}
