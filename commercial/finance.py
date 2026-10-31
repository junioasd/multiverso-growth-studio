"""
commercial.finance
====================
Finance V1 (Seção 49). Rastreia revenue, expenses, committed_cost,
realized_cost, commission, margin, operational P&L, CAC/LTV/ROI quando
mensuráveis.

MICROCORREÇÃO 4 (auditoria — reprovado na 3ª rodada):
- item 1: identidade de idempotência com colunas próprias
  (domain='finance_entry', operation='record_finance_entry', channel_id
  ou '' para GLOBAL, project_id='', idempotency_key) — nunca mais
  colide com outros domínios.
- item 2: `record` sem `idempotency_key` persiste no banco ANTES de
  tocar o cache em memória.
- item 3: com `db_path` configurado, `snapshot` consulta o banco
  diretamente a cada chamada (fonte de verdade).

Serviço interno; o caminho recomendado para chamadores externos/API é
sempre `core.governance.facade.GovernedFacade`.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field

from core.governance.idempotency import IdempotencyConflict, stable_fingerprint
from core.models import db as db_module

# COMANDO 5/6: "refund" (reembolso/chargeback) — sem ele a margem ficava
# INFLADA: receita bruta era tratada como se nunca fosse devolvida.
_VALID_ENTRY_TYPES = {"revenue", "expense", "committed_cost", "realized_cost", "commission", "refund"}
_DOMAIN = "finance_entry"
_OPERATION = "record_finance_entry"

ChannelSelector = str | set[str] | frozenset | None


class InvalidFinanceEntryType(Exception):
    pass


@dataclass
class FinanceEntry:
    entry_id: str
    entry_type: str
    amount: float
    description: str = ""
    channel_id: str | None = None
    currency: str = "BRL"
    created_at: float = field(default_factory=time.time)


@dataclass
class FinanceSnapshot:
    revenue: float = 0.0
    expenses: float = 0.0
    committed_cost: float = 0.0
    realized_cost: float = 0.0
    commission: float = 0.0
    refunds: float = 0.0

    @property
    def margin(self) -> float:
        return self.revenue - self.refunds - self.expenses - self.realized_cost - self.commission

    @property
    def net_revenue(self) -> float:
        """Faturamento LÍQUIDO de reembolsos/chargebacks (faturamento ≠ lucro)."""
        return self.revenue - self.refunds

    @property
    def operational_profit_loss(self) -> float:
        return self.margin


class FinanceLedger:
    def __init__(self, *, db_path: str | None = None) -> None:
        self._entries: list[FinanceEntry] = []
        self._entries_by_id: dict[str, FinanceEntry] = {}
        self._idempotency_index: dict[tuple, tuple[str, str]] = {}
        self._lock = threading.RLock()
        self._db_path = db_path
        if self._db_path:
            self._load_from_db()

    # ------------------------------------------------------------------
    # Persistência / leitura
    # ------------------------------------------------------------------

    def _row_to_entry(self, row: sqlite3.Row) -> FinanceEntry:
        return FinanceEntry(
            entry_id=row["entry_id"], entry_type=row["entry_type"], amount=row["amount"],
            description=row["description"] or "", channel_id=row["channel_id"],
            currency=row["currency"], created_at=row["created_at"],
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM finance_entries").fetchall():
                entry = self._row_to_entry(row)
                self._entries.append(entry)
                self._entries_by_id[entry.entry_id] = entry
            for row in conn.execute(
                "SELECT * FROM idempotency_records WHERE domain = ? AND operation = ?", (_DOMAIN, _OPERATION)
            ).fetchall():
                key = (row["channel_id"], row["project_id"], row["idempotency_key"])
                self._idempotency_index[key] = (row["record_id"], row["fingerprint"])

    @staticmethod
    def _insert_entry_sql(conn: sqlite3.Connection, entry: FinanceEntry) -> None:
        conn.execute(
            "INSERT INTO finance_entries "
            "(entry_id, channel_id, entry_type, amount, currency, description, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (entry.entry_id, entry.channel_id, entry.entry_type, entry.amount, entry.currency,
             entry.description, entry.created_at),
        )

    def _fetch_entries_from_db(self, channel_selector: ChannelSelector) -> list[FinanceEntry]:
        with db_module.connect(self._db_path) as conn:
            if channel_selector is None:
                rows = conn.execute("SELECT * FROM finance_entries").fetchall()
            elif isinstance(channel_selector, (set, frozenset)):
                if not channel_selector:
                    return []
                placeholders = ",".join("?" * len(channel_selector))
                rows = conn.execute(
                    f"SELECT * FROM finance_entries WHERE channel_id IN ({placeholders})", tuple(channel_selector)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM finance_entries WHERE channel_id = ?", (channel_selector,)
                ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # ------------------------------------------------------------------
    # Operações
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        entry_type: str,
        amount: float,
        description: str = "",
        channel_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> FinanceEntry:
        if entry_type not in _VALID_ENTRY_TYPES:
            raise InvalidFinanceEntryType(f"tipo de lançamento inválido: {entry_type}")

        scope_channel = channel_id or ""  # '' representa GLOBAL nesta tabela
        fingerprint = stable_fingerprint(scope_channel or "GLOBAL", entry_type, amount, description)
        entry = FinanceEntry(
            entry_id=f"fin_{uuid.uuid4().hex[:12]}", entry_type=entry_type, amount=amount,
            description=description, channel_id=channel_id,
        )

        with self._lock:
            if not idempotency_key:
                if self._db_path:
                    with db_module.connect(self._db_path) as conn:
                        self._insert_entry_sql(conn, entry)
                self._entries.append(entry)
                self._entries_by_id[entry.entry_id] = entry
                return entry

            index_key = (scope_channel, "", idempotency_key)

            if self._db_path:
                return self._record_durable(index_key, entry, fingerprint)

            existing = self._idempotency_index.get(index_key)
            if existing is not None:
                existing_id, existing_fp = existing
                if existing_fp != fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency_key '{idempotency_key}' já usada no escopo "
                        f"'{scope_channel or 'GLOBAL'}' com conteúdo diferente — reutilização incompatível recusada"
                    )
                return self._entries_by_id[existing_id]

            self._entries.append(entry)
            self._entries_by_id[entry.entry_id] = entry
            self._idempotency_index[index_key] = (entry.entry_id, fingerprint)
            return entry

    def _record_durable(self, index_key: tuple, entry: FinanceEntry, fingerprint: str) -> FinanceEntry:
        channel_id, project_id, idempotency_key = index_key
        conn = db_module.get_autocommit_connection(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_entry_sql(conn, entry)
            conn.execute(
                "INSERT INTO idempotency_records "
                "(domain, operation, channel_id, project_id, idempotency_key, record_id, fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_DOMAIN, _OPERATION, channel_id, project_id, idempotency_key, entry.entry_id, fingerprint, time.time()),
            )
            conn.commit()  # MICROCORREÇÃO 5 (item 2): commit DENTRO do try
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            finally:
                conn.close()
            return self._resolve_entry_conflict(index_key, fingerprint)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass  # não mascara o erro original
            finally:
                conn.close()
            raise
        else:
            conn.close()
            self._entries.append(entry)
            self._entries_by_id[entry.entry_id] = entry
            self._idempotency_index[index_key] = (entry.entry_id, fingerprint)
            return entry

    def _resolve_entry_conflict(self, index_key: tuple, fingerprint: str) -> FinanceEntry:
        channel_id, project_id, idempotency_key = index_key
        row_conn = db_module.get_connection(self._db_path)
        try:
            row = row_conn.execute(
                "SELECT record_id, fingerprint FROM idempotency_records "
                "WHERE domain=? AND operation=? AND channel_id=? AND project_id=? AND idempotency_key=?",
                (_DOMAIN, _OPERATION, channel_id, project_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise IdempotencyConflict(
                    f"idempotency_key já usada no escopo '{channel_id or 'GLOBAL'}' mas o registro "
                    "correspondente não foi encontrado — abortando com segurança"
                )
            existing_record_id, existing_fp = row["record_id"], row["fingerprint"]
            if existing_fp != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency_key já usada no escopo '{channel_id or 'GLOBAL'}' "
                    "com conteúdo diferente — reutilização incompatível recusada"
                )
            existing_row = row_conn.execute(
                "SELECT * FROM finance_entries WHERE entry_id = ?", (existing_record_id,)
            ).fetchone()
        finally:
            row_conn.close()
        existing_entry = self._row_to_entry(existing_row)
        already_present = existing_entry.entry_id in self._entries_by_id
        self._entries_by_id[existing_entry.entry_id] = existing_entry
        if not already_present:
            self._entries.append(existing_entry)
        self._idempotency_index[index_key] = (existing_record_id, existing_fp)
        return existing_entry

    def snapshot(self, channel_id: ChannelSelector = None) -> FinanceSnapshot:
        snap = FinanceSnapshot()
        if self._db_path:
            entries = self._fetch_entries_from_db(channel_id)
        elif channel_id is None:
            entries = self._entries
        elif isinstance(channel_id, (set, frozenset)):
            entries = [e for e in self._entries if e.channel_id in channel_id]
        else:
            entries = [e for e in self._entries if e.channel_id == channel_id]

        for entry in entries:
            if entry.entry_type == "revenue":
                snap.revenue += entry.amount
            elif entry.entry_type == "expense":
                snap.expenses += entry.amount
            elif entry.entry_type == "committed_cost":
                snap.committed_cost += entry.amount
            elif entry.entry_type == "realized_cost":
                snap.realized_cost += entry.amount
            elif entry.entry_type == "commission":
                snap.commission += entry.amount
            elif entry.entry_type == "refund":
                snap.refunds += entry.amount
        return snap

    def cac(self, *, marketing_spend: float, new_customers: int) -> float | str:
        if new_customers <= 0:
            return "UNKNOWN"
        return round(marketing_spend / new_customers, 2)

    def ltv(self, *, avg_revenue_per_customer: float, avg_customer_lifespan_periods: float) -> float | str:
        if avg_revenue_per_customer <= 0 or avg_customer_lifespan_periods <= 0:
            return "UNKNOWN"
        return round(avg_revenue_per_customer * avg_customer_lifespan_periods, 2)

    def roi(self, *, gain: float, cost: float) -> float | str:
        if cost <= 0:
            return "UNKNOWN"
        return round((gain - cost) / cost, 4)
