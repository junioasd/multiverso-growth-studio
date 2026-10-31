"""
navigator.execution_log
===========================
NAVIGATOR — trilha de execução persistida.

CORREÇÃO (item 1): `execution_id`/`attempt_id` persistidos e lidos —
cada tentativa fica rastreável individualmente, sobrevivendo a
restart.
"""

from __future__ import annotations

import json
import sqlite3

from core.models import db as db_module
from navigator.models import ExecutionLogEntry


class NavigatorExecutionLog:
    def __init__(self, *, db_path: str | None = None) -> None:
        self._entries: list[ExecutionLogEntry] = []
        self._db_path = db_path
        if self._db_path:
            self._load_from_db()

    def _row_to_entry(self, row: sqlite3.Row) -> ExecutionLogEntry:
        keys = row.keys()
        return ExecutionLogEntry(
            entry_id=row["entry_id"], mission_id=row["mission_id"], task_id=row["task_id"],
            platform=row["platform"], account_context=row["account_context"],
            task_description=row["task_description"], action=row["action"], result=row["result"],
            evidence=json.loads(row["evidence"]) if row["evidence"] else {}, error=row["error"],
            retry_count=row["retry_count"], approval_used=row["approval_used"], executor=row["executor"],
            idempotency_key=row["idempotency_key"] if "idempotency_key" in keys else None,
            fingerprint=row["fingerprint"] if "fingerprint" in keys else None,
            execution_id=row["execution_id"] if "execution_id" in keys else None,
            attempt_id=row["attempt_id"] if "attempt_id" in keys else None,
            created_at=row["created_at"],
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM navigator_execution_log").fetchall():
                self._entries.append(self._row_to_entry(row))

    def record(self, entry: ExecutionLogEntry) -> ExecutionLogEntry:
        self._entries.append(entry)
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO navigator_execution_log "
                    "(entry_id, mission_id, task_id, platform, account_context, task_description, action, "
                    " result, evidence, error, retry_count, approval_used, executor, idempotency_key, "
                    " fingerprint, execution_id, attempt_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (entry.entry_id, entry.mission_id, entry.task_id, entry.platform, entry.account_context,
                     entry.task_description, entry.action, entry.result, json.dumps(entry.evidence, default=str),
                     entry.error, entry.retry_count, entry.approval_used, entry.executor,
                     entry.idempotency_key, entry.fingerprint, entry.execution_id, entry.attempt_id,
                     entry.created_at),
                )
        return entry

    def update(self, entry: ExecutionLogEntry) -> ExecutionLogEntry:
        """Atualiza uma entrada JÁ existente (usado ao CONSUMIR uma
        aprovação que estava com um registro `ESCALATED` anterior)."""
        for i, existing in enumerate(self._entries):
            if existing.entry_id == entry.entry_id:
                self._entries[i] = entry
                break
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                conn.execute(
                    "UPDATE navigator_execution_log SET result=?, evidence=?, error=?, retry_count=?, "
                    "approval_used=? WHERE entry_id=?",
                    (entry.result, json.dumps(entry.evidence, default=str), entry.error, entry.retry_count,
                     entry.approval_used, entry.entry_id),
                )
        return entry

    def all(self) -> list:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM navigator_execution_log").fetchall()
            return [self._row_to_entry(r) for r in rows]
        return list(self._entries)

    def by_mission(self, mission_id: str) -> list:
        return [e for e in self.all() if e.mission_id == mission_id]

    def by_platform(self, platform: str) -> list:
        return [e for e in self.all() if e.platform == platform]

    def find_by_idempotency_key(self, idempotency_key: str) -> ExecutionLogEntry | None:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM navigator_execution_log WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
            return self._row_to_entry(row) if row is not None else None
        for entry in self._entries:
            if entry.idempotency_key == idempotency_key:
                return entry
        return None

    def by_approval(self, approval_id: str) -> list:
        return [e for e in self.all() if e.approval_used == approval_id]

    def by_task(self, *, mission_id: str, task_id: str, action: str) -> list:
        """CORREÇÃO (item 1): todas as tentativas JÁ registradas para
        uma tarefa+ação específica — usado para detectar se uma nova
        chamada com `attempt_id` é genuinamente a PRIMEIRA tentativa ou
        um RETRY de uma tentativa anterior (decide se a checagem de
        retry-safety se aplica)."""
        return [e for e in self.by_mission(mission_id) if e.task_id == task_id and e.action == action]
