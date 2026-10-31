"""
core.operations.scheduler — scheduler operacional EXPLÍCITO (`python main.py scheduler`).
Cada tarefa tem intervalo configurável; a execução de uma janela é REIVINDICADA
atomicamente em `scheduler_ticks` (PK tick_name+slot): dois schedulers (ou um
reinício no meio da janela) nunca rodam a mesma janela duas vezes — sem
duplicação e sem overlap. Tarefas que levam a ação externa respeitam o kill
switch (registradas SKIPPED_KILL_SWITCH); recuperação de jobs é interna e roda.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import time

from core.models import db as db_module
from core.operations import kill_switch

log = logging.getLogger("growth_studio.scheduler")
DEFAULT_INTERVALS = {"recover_stuck_jobs": 60.0, "operation_runs": 30.0}


class Scheduler:
    def __init__(self, db_path: str, *, intervals: dict | None = None, stuck_job_seconds: float = 300.0) -> None:
        self.db_path = db_path
        self.intervals = dict(DEFAULT_INTERVALS, **(intervals or {}))
        self.stuck_job_seconds = stuck_job_seconds

    def _db_retry(self, operation, label: str):
        """Retry only transient SQLite lock contention, with bounded delay."""
        for attempt in range(4):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not (getattr(exc, 'sqlite_errorcode', None) in (5, 6) or
                        'locked' in str(exc).lower() or 'busy' in str(exc).lower()):
                    raise
                if attempt == 3:
                    raise
                delay = min(0.15 * (2 ** attempt), 0.6) + random.uniform(0, 0.08)
                log.warning('SQLite contention in %s; retry %s/3', label, attempt + 1)
                time.sleep(delay)

    def _claim(self, name: str, slot: int, now: float) -> bool:
        def attempt():
            with db_module.connect(self.db_path) as conn:
                cur = conn.execute("INSERT OR IGNORE INTO scheduler_ticks (tick_name, slot, started_at, status) "
                                   "VALUES (?, ?, ?, 'RUNNING')", (name, slot, now))
                return cur.rowcount == 1
        return self._db_retry(attempt, '_claim')

    def _finish(self, name, slot, status, detail):
        def attempt():
            with db_module.connect(self.db_path) as conn:
                conn.execute("UPDATE scheduler_ticks SET status = ?, finished_at = ?, detail = ? WHERE tick_name = ? AND slot = ?",
                             (status, time.time(), json.dumps(detail, ensure_ascii=False, default=str)[:4000], name, slot))
        return self._db_retry(attempt, '_finish')

    def _safe_finish(self, name, slot, status, detail):
        try:
            self._finish(name, slot, status, detail)
            return True
        except Exception:
            log.exception('finish failed for %s slot %s; no replay of task', name, slot)
            return False

    def _recover(self, slot):
        from core.jobs.queue import JobQueue

        return {"recovered": len(JobQueue(db_path=self.db_path).recover_stuck_jobs(timeout_seconds=self.stuck_job_seconds))}

    def _operation_runs(self, slot):
        from core.jobs.queue import JobQueue
        from navigator.planning import schedule_advance

        q = JobQueue(db_path=self.db_path)
        with db_module.connect(self.db_path) as conn:
            runs = [r["run_id"] for r in conn.execute(
                "SELECT run_id FROM navigator_operation_runs WHERE status IN ('PLANNED','RUNNING','WAITING_APPROVAL')")]
        for run_id in runs:
            schedule_advance(q, run_id, tick=1_000_000 + slot)   # chave idempotente por janela
        return {"scheduled_runs": runs}

    _TASKS = {"recover_stuck_jobs": ("_recover", False), "operation_runs": ("_operation_runs", True)}

    def tick(self, now: float | None = None) -> list:
        now = time.time() if now is None else now
        results = []
        for name, interval in self.intervals.items():
            if name not in self._TASKS:
                continue
            slot = int(now // interval)
            try:
                claimed = self._claim(name, slot, now)
            except Exception as exc:
                log.exception('claim failed for %s slot %s; scheduler remains alive', name, slot)
                results.append({'task': name, 'slot': slot, 'status': 'CLAIM_ERROR', 'error': str(exc)})
                continue
            if not claimed:
                results.append({"task": name, "slot": slot, "status": "ALREADY_CLAIMED"})
                continue
            method, external = self._TASKS[name]
            try:
                blocked = kill_switch.block_reason(self.db_path) if external else None
            except Exception as exc:
                log.exception('kill-switch read failed; fail closed for %s', name)
                results.append({'task': name, 'slot': slot, 'status': 'KILL_SWITCH_ERROR', 'error': str(exc)})
                continue
            if blocked:
                self._safe_finish(name, slot, "SKIPPED_KILL_SWITCH", {"reason": blocked})
                log.warning("tarefa %s janela %s ignorada: %s", name, slot, blocked)
                results.append({"task": name, "slot": slot, "status": "SKIPPED_KILL_SWITCH"})
                continue
            try:
                detail = getattr(self, method)(slot)
                self._safe_finish(name, slot, "SUCCESS", detail)
                log.info("tarefa %s janela %s concluída: %s", name, slot, detail)
                results.append({"task": name, "slot": slot, "status": "SUCCESS", "detail": detail})
            except Exception as exc:
                self._safe_finish(name, slot, "FAILED", {"error": f"{type(exc).__name__}: {exc}"})
                log.exception("tarefa %s janela %s falhou", name, slot)
                results.append({"task": name, "slot": slot, "status": "FAILED", "error": str(exc)})
        return results

    def last_activity(self) -> float | None:
        with db_module.connect(self.db_path) as conn:
            r = conn.execute("SELECT MAX(started_at) FROM scheduler_ticks").fetchone()[0]
        return r
