"""
commercial.sales_tracking
===========================
Sales Tracking (Seção 48). Rastreia impression/view/click/lead/
checkout/sale/refund. Tipos obrigatórios: REAL, ESTIMATED, FORECAST,
UNKNOWN. Nunca inventa venda/receita/conversão/comissão.

MICROCORREÇÃO 4 (auditoria — reprovado na 3ª rodada):
- item 1: identidade de idempotência com colunas próprias
  (domain='sales_event', operation='record_sales_event', channel_id,
  project_id='', idempotency_key) — nunca mais colide com outros
  domínios (ver `core/models/db.py`).
- item 2: `record_event` sem `idempotency_key` persiste no banco ANTES
  de tocar o cache em memória.
- item 3: com `db_path` configurado, `summarize`/`all_events` consultam
  o banco diretamente a cada chamada (fonte de verdade), nunca apenas o
  cache carregado uma vez no `__init__`.

Serviço interno; o caminho recomendado para chamadores externos/API é
sempre `core.governance.facade.GovernedFacade`.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field

from core.governance.idempotency import IdempotencyConflict, stable_fingerprint
from core.models import db as db_module
from core.models.entities import SalesEvent, ValueType

_VALID_EVENT_TYPES = {"impression", "view", "click", "lead", "checkout", "sale", "refund"}
_DOMAIN = "sales_event"
_OPERATION = "record_sales_event"


class InvalidEventType(Exception):
    pass


class UnverifiedAmountRejected(Exception):
    """Levantado quando alguém tenta registrar amount sem declarar value_type,
    o que equivaleria a inventar receita/venda (proibido pela Seção 48)."""


@dataclass
class SalesSummary:
    impressions: int = 0
    views: int = 0
    clicks: int = 0
    leads: int = 0
    checkouts: int = 0

    sales_real: int = 0
    sales_estimated: int = 0
    sales_forecast: int = 0
    sales_unknown: int = 0
    refunds_real: int = 0
    refunds_estimated: int = 0
    refunds_forecast: int = 0
    refunds_unknown: int = 0

    gross_revenue_real: float = 0.0
    gross_revenue_estimated: float = 0.0
    gross_revenue_forecast: float = 0.0
    refunds_amount_real: float = 0.0
    refunds_amount_estimated: float = 0.0
    refunds_amount_forecast: float = 0.0

    @property
    def sales(self) -> int:
        return self.sales_real

    @property
    def refunds(self) -> int:
        return self.refunds_real

    @property
    def real_revenue(self) -> float:
        return self.gross_revenue_real - self.refunds_amount_real

    @property
    def estimated_revenue(self) -> float:
        return self.gross_revenue_estimated - self.refunds_amount_estimated

    @property
    def forecast_revenue(self) -> float:
        return self.gross_revenue_forecast - self.refunds_amount_forecast


ChannelSelector = str | set[str] | frozenset | None


class SalesTracker:
    def __init__(self, *, db_path: str | None = None) -> None:
        self._events: list[SalesEvent] = []
        self._events_by_id: dict[str, SalesEvent] = {}
        self._idempotency_index: dict[tuple, tuple[str, str]] = {}
        self._lock = threading.RLock()
        self._db_path = db_path
        if self._db_path:
            self._load_from_db()

    # ------------------------------------------------------------------
    # Persistência / leitura
    # ------------------------------------------------------------------

    def _row_to_event(self, row: sqlite3.Row) -> SalesEvent:
        keys = row.keys()
        return SalesEvent(
            event_id=row["event_id"], channel_id=row["channel_id"], event_type=row["event_type"],
            value_type=ValueType(row["value_type"]), amount=row["amount"], currency=row["currency"],
            lead_id=row["lead_id"], offer_id=row["offer_id"],
            product_id=(row["product_id"] or None) if "product_id" in keys else None,
            campaign_id=(row["campaign_id"] or None) if "campaign_id" in keys else None,
            funnel_id=(row["funnel_id"] or None) if "funnel_id" in keys else None,
            platform=(row["platform"] or None) if "platform" in keys else None,
            traffic_source=(row["traffic_source"] or None) if "traffic_source" in keys else None,
            created_at=row["created_at"],
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM sales_events").fetchall():
                event = self._row_to_event(row)
                self._events.append(event)
                self._events_by_id[event.event_id] = event
            for row in conn.execute(
                "SELECT * FROM idempotency_records WHERE domain = ? AND operation = ?", (_DOMAIN, _OPERATION)
            ).fetchall():
                key = (row["channel_id"], row["project_id"], row["idempotency_key"])
                self._idempotency_index[key] = (row["record_id"], row["fingerprint"])

    @staticmethod
    def _insert_event_sql(conn: sqlite3.Connection, event: SalesEvent) -> None:
        conn.execute(
            "INSERT INTO sales_events "
            "(event_id, channel_id, lead_id, offer_id, event_type, value_type, amount, currency, "
            " product_id, campaign_id, funnel_id, platform, traffic_source, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event.event_id, event.channel_id, event.lead_id, event.offer_id, event.event_type,
             event.value_type.value, event.amount, event.currency,
             event.product_id or "", event.campaign_id or "", event.funnel_id or "",
             event.platform or "", event.traffic_source or "", event.created_at),
        )

    def _fetch_events_from_db(self, channel_selector: ChannelSelector) -> list[SalesEvent]:
        with db_module.connect(self._db_path) as conn:
            if channel_selector is None:
                rows = conn.execute("SELECT * FROM sales_events").fetchall()
            elif isinstance(channel_selector, (set, frozenset)):
                if not channel_selector:
                    return []
                placeholders = ",".join("?" * len(channel_selector))
                rows = conn.execute(
                    f"SELECT * FROM sales_events WHERE channel_id IN ({placeholders})", tuple(channel_selector)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sales_events WHERE channel_id = ?", (channel_selector,)
                ).fetchall()
        return [self._row_to_event(r) for r in rows]

    # ------------------------------------------------------------------
    # ATLAS+RADAR (item 2 — desempenho multidimensional): filtragem por
    # canal/produto/campanha/funil/plataforma/fonte de tráfego, cada um
    # opcional (None = sem filtro), string (valor exato) ou set (qualquer
    # um dos valores). Nunca fabrica dado: dimensões nunca registradas
    # simplesmente não aparecem em `distinct_values`.
    # ------------------------------------------------------------------

    _DIMENSION_FIELDS = ("channel_id", "product_id", "campaign_id", "funnel_id", "platform", "traffic_source")

    @staticmethod
    def _matches(event: SalesEvent, filters: dict) -> bool:
        for field_name, selector in filters.items():
            if selector is None:
                continue
            value = getattr(event, field_name)
            if isinstance(selector, (set, frozenset)):
                if value not in selector:
                    return False
            elif value != selector:
                return False
        return True

    def _fetch_events_filtered(self, **filters) -> list[SalesEvent]:
        active_filters = {k: v for k, v in filters.items() if v is not None}
        if self._db_path:
            # busca a base pelo filtro de canal (se houver) via SQL, e
            # aplica os demais filtros em Python — mantém a lógica de
            # correspondência num único lugar (`_matches`), evitando
            # duplicar regras de filtro entre SQL e memória.
            base = self._fetch_events_from_db(active_filters.get("channel_id"))
            remaining = {k: v for k, v in active_filters.items() if k != "channel_id"}
        else:
            # modo em memória: SEM atalho de SQL — todos os filtros,
            # incluindo channel_id, passam por `_matches` igualmente.
            base = list(self._events)
            remaining = active_filters
        return [e for e in base if self._matches(e, remaining)]

    @staticmethod
    def _aggregate(events: list[SalesEvent]) -> "SalesSummary":
        summary = SalesSummary()
        counters = {
            "impression": "impressions", "view": "views", "click": "clicks",
            "lead": "leads", "checkout": "checkouts",
        }
        for event in events:
            if event.event_type in counters:
                attr = counters[event.event_type]
                setattr(summary, attr, getattr(summary, attr) + 1)
                continue

            if event.event_type == "sale":
                if event.value_type == ValueType.REAL:
                    summary.sales_real += 1
                    if event.amount is not None:
                        summary.gross_revenue_real += event.amount
                elif event.value_type == ValueType.ESTIMATED:
                    summary.sales_estimated += 1
                    if event.amount is not None:
                        summary.gross_revenue_estimated += event.amount
                elif event.value_type == ValueType.FORECAST:
                    summary.sales_forecast += 1
                    if event.amount is not None:
                        summary.gross_revenue_forecast += event.amount
                else:
                    summary.sales_unknown += 1
            elif event.event_type == "refund":
                if event.value_type == ValueType.REAL:
                    summary.refunds_real += 1
                    if event.amount is not None:
                        summary.refunds_amount_real += event.amount
                elif event.value_type == ValueType.ESTIMATED:
                    summary.refunds_estimated += 1
                    if event.amount is not None:
                        summary.refunds_amount_estimated += event.amount
                elif event.value_type == ValueType.FORECAST:
                    summary.refunds_forecast += 1
                    if event.amount is not None:
                        summary.refunds_amount_forecast += event.amount
                else:
                    summary.refunds_unknown += 1

        return summary

    def summarize_filtered(
        self, *, channel_id: ChannelSelector = None, product_id: ChannelSelector = None,
        campaign_id: ChannelSelector = None, funnel_id: ChannelSelector = None,
        platform: ChannelSelector = None, traffic_source: ChannelSelector = None,
    ) -> "SalesSummary":
        events = self._fetch_events_filtered(
            channel_id=channel_id, product_id=product_id, campaign_id=campaign_id,
            funnel_id=funnel_id, platform=platform, traffic_source=traffic_source,
        )
        return self._aggregate(events)

    def distinct_values(self, dimension: str, **filters) -> list[str]:
        """Valores REALMENTE registrados para uma dimensão, dentro dos
        filtros dados — nunca uma lista fixa/inventada. Vazio quando
        nada foi registrado."""
        if dimension not in self._DIMENSION_FIELDS:
            raise ValueError(f"dimensão desconhecida: {dimension}")
        events = self._fetch_events_filtered(**filters)
        values = {getattr(e, dimension) for e in events if getattr(e, dimension)}
        return sorted(values)

    # ------------------------------------------------------------------
    # Operações
    # ------------------------------------------------------------------

    def record_event(
        self,
        *,
        channel_id: str,
        event_type: str,
        value_type: ValueType = ValueType.UNKNOWN,
        amount: float | None = None,
        lead_id: str | None = None,
        offer_id: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        funnel_id: str | None = None,
        platform: str | None = None,
        traffic_source: str | None = None,
        idempotency_key: str | None = None,
    ) -> SalesEvent:
        if event_type not in _VALID_EVENT_TYPES:
            raise InvalidEventType(f"tipo de evento inválido: {event_type}")

        if amount is not None and value_type == ValueType.UNKNOWN:
            raise UnverifiedAmountRejected(
                "valor monetário informado sem value_type definido "
                "(REAL/ESTIMATED/FORECAST) — não é permitido inventar receita"
            )

        value_type_value = value_type.value if isinstance(value_type, ValueType) else value_type
        fingerprint = stable_fingerprint(
            channel_id, event_type, value_type_value, amount, lead_id, offer_id,
            product_id, campaign_id, funnel_id, platform, traffic_source,
        )
        event = SalesEvent.create(
            channel_id, event_type, value_type=value_type, amount=amount, lead_id=lead_id, offer_id=offer_id,
            product_id=product_id, campaign_id=campaign_id, funnel_id=funnel_id,
            platform=platform, traffic_source=traffic_source,
        )

        with self._lock:
            if not idempotency_key:
                # item 2: persiste ANTES de tocar o cache.
                if self._db_path:
                    with db_module.connect(self._db_path) as conn:
                        self._insert_event_sql(conn, event)
                self._events.append(event)
                self._events_by_id[event.event_id] = event
                return event

            index_key = (channel_id, "", idempotency_key)

            if self._db_path:
                return self._record_event_durable(index_key, event, fingerprint)

            existing = self._idempotency_index.get(index_key)
            if existing is not None:
                existing_id, existing_fp = existing
                if existing_fp != fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency_key '{idempotency_key}' já usada no canal '{channel_id}' "
                        "com conteúdo diferente — reutilização incompatível recusada"
                    )
                return self._events_by_id[existing_id]

            self._events.append(event)
            self._events_by_id[event.event_id] = event
            self._idempotency_index[index_key] = (event.event_id, fingerprint)
            return event

    def _record_event_durable(self, index_key: tuple, event: SalesEvent, fingerprint: str) -> SalesEvent:
        channel_id, project_id, idempotency_key = index_key
        conn = db_module.get_autocommit_connection(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_event_sql(conn, event)
            conn.execute(
                "INSERT INTO idempotency_records "
                "(domain, operation, channel_id, project_id, idempotency_key, record_id, fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_DOMAIN, _OPERATION, channel_id, project_id, idempotency_key, event.event_id, fingerprint, time.time()),
            )
            conn.commit()  # MICROCORREÇÃO 5 (item 2): commit DENTRO do try
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            finally:
                conn.close()
            return self._resolve_event_conflict(index_key, fingerprint)
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
            self._events.append(event)
            self._events_by_id[event.event_id] = event
            self._idempotency_index[index_key] = (event.event_id, fingerprint)
            return event

    def _resolve_event_conflict(self, index_key: tuple, fingerprint: str) -> SalesEvent:
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
                    f"idempotency_key '{idempotency_key}' colidiu mas o registro correspondente "
                    "não foi encontrado — estado inesperado, abortando com segurança"
                )
            existing_record_id, existing_fp = row["record_id"], row["fingerprint"]
            if existing_fp != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency_key '{idempotency_key}' já usada no canal '{channel_id}' "
                    "com conteúdo diferente — reutilização incompatível recusada"
                )
            existing_row = row_conn.execute(
                "SELECT * FROM sales_events WHERE event_id = ?", (existing_record_id,)
            ).fetchone()
        finally:
            row_conn.close()
        existing_event = self._row_to_event(existing_row)
        already_present = existing_event.event_id in self._events_by_id
        self._events_by_id[existing_event.event_id] = existing_event
        if not already_present:
            self._events.append(existing_event)
        self._idempotency_index[index_key] = (existing_record_id, existing_fp)
        return existing_event

    def summarize(self, channel_id: ChannelSelector = None) -> SalesSummary:
        if self._db_path:
            events = self._fetch_events_from_db(channel_id)
        elif channel_id is None:
            events = self._events
        elif isinstance(channel_id, (set, frozenset)):
            events = [e for e in self._events if e.channel_id in channel_id]
        else:
            events = [e for e in self._events if e.channel_id == channel_id]
        return self._aggregate(events)

    def all_events(self) -> list[SalesEvent]:
        if self._db_path:
            return self._fetch_events_from_db(None)
        return list(self._events)
