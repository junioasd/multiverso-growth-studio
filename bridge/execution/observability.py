"""
bridge.execution.observability
===================================
BLOCO FINAL 2 (item 7 — OBSERVABILIDADE). `ExecutionObservability` —
registro ESTRUTURADO (não apenas um dict genérico de auditoria) de
toda tentativa de execução externa: ação, plataforma, conta/canal,
horário, resultado, evidência, erro, retry, aprovação, custo. Cada
campo é uma coluna própria (não texto livre) — permite consultas
reais (ex: "todas as ações que falharam no Instagram este mês",
"custo total aprovado por plataforma").

Persistido em SQLite, sobrevive a restart — nunca um log apenas em
memória que se perde.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class ExecutionLogEntry:
    entry_id: str
    action: str
    platform: str
    account_or_channel: str | None
    executed_at: float
    success: bool
    mechanism_used: str | None = None
    evidence_ref: str | None = None
    error_reason: str | None = None
    retry_count: int = 0
    escalation_id: str | None = None
    cost_amount: float | None = None
    cost_currency: str | None = None

    @staticmethod
    def new(
        *, action: str, platform: str, success: bool, account_or_channel: str | None = None,
        mechanism_used: str | None = None, evidence_ref: str | None = None, error_reason: str | None = None,
        retry_count: int = 0, escalation_id: str | None = None, cost_amount: float | None = None,
        cost_currency: str | None = None,
    ) -> "ExecutionLogEntry":
        return ExecutionLogEntry(
            entry_id=f"exec_{uuid.uuid4().hex[:12]}", action=action, platform=platform,
            account_or_channel=account_or_channel, executed_at=time.time(), success=success,
            mechanism_used=mechanism_used, evidence_ref=evidence_ref, error_reason=error_reason,
            retry_count=retry_count, escalation_id=escalation_id, cost_amount=cost_amount, cost_currency=cost_currency,
        )


class ExecutionObservability:
    def __init__(self, *, db_path: str | None = None) -> None:
        self.db_path = db_path
        self._entries: list = []
        if db_path:
            self._load_from_db()

    def record(self, entry: ExecutionLogEntry) -> ExecutionLogEntry:
        self._entries.append(entry)
        self._persist(entry)
        return entry

    def record_navigator_result(self, *, result, criticality: str, actor: str) -> ExecutionLogEntry:
        """Conveniência — cria um `ExecutionLogEntry` diretamente a
        partir de um `NavigatorActionResult` real (nunca duplica os
        campos manualmente em cada chamador)."""
        entry = ExecutionLogEntry.new(
            action=result.action, platform=result.platform, success=result.success,
            mechanism_used=result.mechanism_used, error_reason=None if result.success else result.reason,
            escalation_id=result.escalation_id,
        )
        return self.record(entry)

    def query(
        self, *, platform: str | None = None, success: bool | None = None, account_or_channel: str | None = None,
        since: float | None = None,
    ) -> list:
        results = list(self._entries)
        if platform is not None:
            results = [e for e in results if e.platform == platform]
        if success is not None:
            results = [e for e in results if e.success == success]
        if account_or_channel is not None:
            results = [e for e in results if e.account_or_channel == account_or_channel]
        if since is not None:
            results = [e for e in results if e.executed_at >= since]
        return results

    def total_cost_by_platform(self, *, platform: str) -> dict:
        """Soma REAL de custo registrado por plataforma — só conta
        entradas que genuinamente têm `cost_amount` (nunca assume 0
        para o que não foi informado, distinguindo 'sem custo' de
        'custo desconhecido')."""
        entries = [e for e in self._entries if e.platform == platform and e.cost_amount is not None]
        by_currency: dict = {}
        for e in entries:
            by_currency[e.cost_currency] = by_currency.get(e.cost_currency, 0.0) + e.cost_amount
        return by_currency

    def failure_rate(self, *, platform: str) -> float | None:
        entries = self.query(platform=platform)
        if not entries:
            return None
        failures = sum(1 for e in entries if not e.success)
        return failures / len(entries)

    # ------------------------------------------------------------------
    # Persistência
    # ------------------------------------------------------------------

    def _persist(self, entry: ExecutionLogEntry) -> None:
        if not self.db_path:
            return
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO execution_observability "
                "(entry_id, action, platform, account_or_channel, executed_at, success, mechanism_used, "
                "evidence_ref, error_reason, retry_count, escalation_id, cost_amount, cost_currency) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.entry_id, entry.action, entry.platform, entry.account_or_channel, entry.executed_at,
                    int(entry.success), entry.mechanism_used, entry.evidence_ref, entry.error_reason,
                    entry.retry_count, entry.escalation_id, entry.cost_amount, entry.cost_currency,
                ),
            )

    def _load_from_db(self) -> None:
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            for row in conn.execute("SELECT * FROM execution_observability ORDER BY executed_at").fetchall():
                self._entries.append(ExecutionLogEntry(
                    entry_id=row["entry_id"], action=row["action"], platform=row["platform"],
                    account_or_channel=row["account_or_channel"], executed_at=row["executed_at"],
                    success=bool(row["success"]), mechanism_used=row["mechanism_used"],
                    evidence_ref=row["evidence_ref"], error_reason=row["error_reason"],
                    retry_count=row["retry_count"], escalation_id=row["escalation_id"],
                    cost_amount=row["cost_amount"], cost_currency=row["cost_currency"],
                ))
