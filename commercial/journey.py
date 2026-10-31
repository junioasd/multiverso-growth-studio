"""
commercial.journey
====================
COMANDO 5/6 — JORNADA COMERCIAL PONTA A PONTA (pedido → pagamento →
entrega → pós-venda), ECONOMIA UNITÁRIA e ESCADA DE DISTRIBUIÇÃO.

O que já existia e é REUTILIZADO: CRM (NEW→ENGAGED→QUALIFIED→OFFERED→
CUSTOMER), `SalesTracker` (eventos impression/view/click/lead/checkout/
sale/refund com ValueType), `FinanceLedger` (agora com reembolso),
`GovernedFacade` (autorização + idempotência + auditoria) e o Gêmeo.

Faltava o PEDIDO com estados verificáveis. Regras estruturais:
- checkout iniciado NÃO é venda: só gera evento `checkout`;
- PAYMENT_PENDING (boleto/pix emitido, requisição aceita) NÃO é pagamento;
- PAID só com confirmação do gateway (id de evento + valor igual ao do
  pedido) — só então nascem o evento `sale` e a receita no livro;
- DELIVERED só a partir de PAID e com evidência de entrega;
- REFUNDED lança reembolso no livro (a margem cai de verdade);
- cada evento do gateway é processado UMA vez (retry não duplica venda,
  receita, entrega nem reembolso) e todo lançamento usa idempotency_key.
"""

from __future__ import annotations

import json
import time

from core.models import db as db_module
from core.models.entities import ValueType

CHECKOUT_STARTED, PAYMENT_PENDING, PAID, FAILED = "CHECKOUT_STARTED", "PAYMENT_PENDING", "PAID", "FAILED"
REFUNDED, DELIVERED, POST_SALE, RETAINED = "REFUNDED", "DELIVERED", "POST_SALE", "RETAINED"
_ALLOWED = {
    CHECKOUT_STARTED: {PAYMENT_PENDING, PAID, FAILED},
    PAYMENT_PENDING: {PAID, FAILED},
    FAILED: {PAYMENT_PENDING, PAID},            # recuperação de pagamento falho
    PAID: {DELIVERED, REFUNDED},
    DELIVERED: {POST_SALE, REFUNDED},
    POST_SALE: {RETAINED, REFUNDED},
    RETAINED: {REFUNDED},
    REFUNDED: set(),
}


class JourneyViolation(ValueError):
    pass


class JourneyConflict(JourneyViolation):
    """CORREÇÃO PÓS-AUDITORIA (C/D): identificador idempotente reutilizado
    com CONTEÚDO diferente — conflito de integridade, nunca retry."""


_CHECKOUT_FIELDS = ("channel_id", "amount", "lead_id", "product_id", "offer_id", "campaign_id", "traffic_source",
                    "product_cost", "commission")


def _event_fingerprint(order_id: str, new_status: str, **content) -> str:
    from core.governance.idempotency import stable_fingerprint

    return stable_fingerprint("order_event", order_id, new_status, content)


class OrderJourney:
    def __init__(self, *, facade, principal, db_path: str) -> None:
        self.facade, self.principal, self.db_path = facade, principal, db_path

    # ------------------------------------------------------------ leitura
    def get(self, order_id: str) -> dict:
        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM commercial_orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise KeyError(order_id)
        d = dict(row)
        d["history"] = json.loads(d.pop("history_json"))
        d["processed_events"] = json.loads(d.pop("processed_events_json"))
        d["event_fingerprints"] = json.loads(d.pop("event_fingerprints_json", None) or "{}")
        return d

    def all(self) -> list:
        with db_module.connect(self.db_path) as conn:
            ids = [r["order_id"] for r in conn.execute("SELECT order_id FROM commercial_orders").fetchall()]
        return [self.get(i) for i in ids]

    # ---------------------------------------------------------- transições
    def _check_replay(self, order, event_id: str, fingerprint: str) -> None:
        """Mesmo ID + mesmo conteúdo = retry idempotente. Mesmo ID +
        conteúdo diferente = CONFLITO (nunca aceito silenciosamente).

        CORREÇÃO FINAL PÓS-REAUDITORIA (bloqueador reproduzido com upgrade
        real): evento LEGADO (processado antes de `event_fingerprints_json`)
        era aceito como retry SEM prova — a equivalência era PRESUMIDA.
        Agora o fingerprint original é RECONSTRUÍDO só a partir de histórico
        PERSISTIDO (nunca do replay recebido); sem prova suficiente, falha
        FECHADO com LEGACY_EVENT_UNVERIFIABLE — mesmo para replay "idêntico"."""
        stored = order["event_fingerprints"].get(event_id)
        if stored is None:
            stored = self._legacy_fingerprint(order, event_id)
            if stored is None:
                raise JourneyConflict(
                    f"LEGACY_EVENT_UNVERIFIABLE: evento '{event_id}' do pedido {order['order_id']} foi processado "
                    f"antes do registro de fingerprints e o histórico persistido não comprova seu conteúdo — "
                    f"replay bloqueado (equivalência não pode ser presumida); requer revisão")
        if stored != fingerprint:
            raise JourneyConflict(
                f"evento '{event_id}' já processado no pedido {order['order_id']} com CONTEÚDO DIFERENTE — "
                f"replay divergente rejeitado (conflito de integridade)")

    # ---------------------------------------- reconstrução de evento legado
    def _idem_amount(self, *, domain: str, key: str, channel_id: str, index: int):
        """Valor EFETIVAMENTE lançado, lido do registro de idempotência da
        GovernedFacade (gravado pelo próprio sistema no processamento
        original). None = registro ausente ou ilegível."""
        with db_module.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT fingerprint FROM idempotency_records WHERE domain = ? AND idempotency_key = ? "
                "AND channel_id = ?", (domain, key, channel_id)).fetchall()
        if len(rows) != 1:
            return None
        try:
            value = json.loads(rows[0]["fingerprint"])[index]
            return round(float(value), 2) if value is not None else None
        except (ValueError, TypeError, IndexError):
            return None

    def _legacy_fingerprint(self, order, event_id: str):
        """Fingerprint ORIGINAL de um evento legado, reconstruído de forma
        determinística SOMENTE de dados persistidos no processamento original:
        - evidência e transição: `history_json` do pedido;
        - valor (PAID/REFUNDED): lançamento no livro (`<pedido>:revenue` /
          `<pedido>:refund`), que precisa conferir com o evento de venda
          (`<pedido>:sale` / `<pedido>:refund`).
        Qualquer lacuna ou divergência => None (não comprovável)."""
        entries = [h for h in order["history"] if h.get("event_id") == event_id]
        if len(entries) != 1 or "evidence" not in entries[0] or not entries[0].get("to"):
            return None
        entry, content = entries[0], {}
        if entry["to"] in (PAID, REFUNDED):
            oid, ch = order["order_id"], order["channel_id"]
            fin_key = f"{oid}:revenue" if entry["to"] == PAID else f"{oid}:refund"
            sale_key = f"{oid}:sale" if entry["to"] == PAID else f"{oid}:refund"
            booked = self._idem_amount(domain="finance_entry", key=fin_key, channel_id=ch, index=2)
            observed = self._idem_amount(domain="sales_event", key=sale_key, channel_id=ch, index=3)
            if booked is None or observed is None or booked != observed:
                return None
            content = {"amount": booked}
        return _event_fingerprint(order["order_id"], entry["to"], evidence=entry["evidence"], **content)

    def _transition(self, order_id: str, new_status: str, *, event_id: str, evidence: str, now: float,
                    content: dict | None = None) -> tuple:
        """(pedido, aplicado?) — evento já processado com o MESMO conteúdo
        devolve aplicado=False; com conteúdo diferente, JourneyConflict.
        CORREÇÃO PÓS-AUDITORIA (bloqueador C — reproduzido): antes, o ID
        sozinho bastava — replay com valor 1 ou 999 era aceito como retry."""
        fingerprint = _event_fingerprint(order_id, new_status, evidence=evidence, **(content or {}))
        order = self.get(order_id)
        if event_id in order["processed_events"]:
            self._check_replay(order, event_id, fingerprint)
            return order, False                                     # retry legítimo: nada se repete
        if new_status not in _ALLOWED[order["status"]]:
            raise JourneyViolation(f"{order['status']} -> {new_status} não é permitido "
                                   f"(permitidos: {sorted(_ALLOWED[order['status']])})")
        order["history"].append({"from": order["status"], "to": new_status, "event_id": event_id,
                                 "evidence": evidence, "at": now})
        order["processed_events"].append(event_id)
        order["event_fingerprints"][event_id] = fingerprint
        with db_module.connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE commercial_orders SET status = ?, history_json = ?, processed_events_json = ?, "
                "event_fingerprints_json = ?, updated_at = ? WHERE order_id = ? AND status = ?",
                (new_status, json.dumps(order["history"]), json.dumps(order["processed_events"]),
                 json.dumps(order["event_fingerprints"]), now, order_id, order["status"]))
            if cur.rowcount != 1:
                # COMANDO 6/6: outra thread aplicou antes. Se foi o MESMO evento,
                # é retry concorrente — resposta idempotente, nada se repete.
                current = self.get(order_id)
                if event_id in current["processed_events"]:
                    self._check_replay(current, event_id, fingerprint)
                    return current, False
                raise JourneyViolation("pedido alterado concorrentemente — transição não aplicada")
        order["status"] = new_status
        return order, True

    def _event(self, order, event_type: str, *, amount=None, key: str):
        self.facade.record_sales_event(
            self.principal, channel_id=order["channel_id"], event_type=event_type, value_type=ValueType.REAL,
            amount=amount, campaign_id=order["campaign_id"], product_id=order["product_id"],
            traffic_source=order["traffic_source"], idempotency_key=key)

    def _ledger(self, order, entry_type: str, amount: float, key: str):
        self.facade.record_finance_entry(self.principal, entry_type=entry_type, amount=amount,
                                         channel_id=order["channel_id"], idempotency_key=key)

    def start_checkout(self, *, order_id: str, channel_id: str, amount: float, lead_id=None, product_id=None,
                       offer_id=None, campaign_id=None, traffic_source=None, product_cost=None, commission=None,
                       now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if amount <= 0:
            raise JourneyViolation("valor do pedido deve ser positivo")
        with db_module.connect(self.db_path) as conn:
            # COMANDO 6/6 (corrida provada: 8 checkouts simultâneos geravam
            # IntegrityError): inserção atômica e idempotente.
            if True:
                conn.execute(
                    "INSERT OR IGNORE INTO commercial_orders (order_id, lead_id, product_id, offer_id, campaign_id, channel_id, "
                    "traffic_source, amount, product_cost, commission, status, history_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (order_id, lead_id, product_id, offer_id, campaign_id, channel_id, traffic_source, amount,
                     product_cost, commission, CHECKOUT_STARTED,
                     json.dumps([{"from": None, "to": CHECKOUT_STARTED, "at": now}]), now, now))
        order = self.get(order_id)
        # CORREÇÃO PÓS-AUDITORIA (bloqueador D — reproduzido): `INSERT OR
        # IGNORE` devolvia o pedido ANTERIOR mesmo com canal/valor/oferta
        # diferentes. Mesmo order_id só é idempotente com o MESMO conteúdo.
        requested = {"channel_id": channel_id, "amount": amount, "lead_id": lead_id, "product_id": product_id,
                     "offer_id": offer_id, "campaign_id": campaign_id, "traffic_source": traffic_source,
                     "product_cost": product_cost, "commission": commission}
        divergent = [f for f in _CHECKOUT_FIELDS if order[f] != requested[f]]
        if divergent:
            raise JourneyConflict(
                f"order_id '{order_id}' já existe com conteúdo DIFERENTE em {divergent} — reutilização "
                f"incompatível rejeitada (conflito de integridade)")
        self._event(order, "checkout", key=f"{order_id}:checkout")       # checkout NÃO é venda
        return order

    def payment_pending(self, *, order_id: str, gateway_event_id: str, now=None) -> dict:
        return self._transition(order_id, PAYMENT_PENDING, event_id=gateway_event_id,
                                evidence="cobrança emitida/aceita — NÃO é pagamento", now=now or time.time())[0]

    def confirm_payment(self, *, order_id: str, gateway_event_id: str, paid_amount: float, evidence: str,
                        now=None) -> dict:
        if not evidence:
            raise JourneyViolation("confirmação de pagamento exige evidência do gateway")
        order = self.get(order_id)
        if gateway_event_id not in order["processed_events"] and round(paid_amount, 2) != round(order["amount"], 2):
            raise JourneyViolation(f"valor confirmado {paid_amount} difere do pedido {order['amount']} — não é PAID")
        order, applied = self._transition(order_id, PAID, event_id=gateway_event_id, evidence=evidence,
                                          now=now or time.time(), content={"amount": round(paid_amount, 2)})
        if applied:
            self._event(order, "sale", amount=order["amount"], key=f"{order_id}:sale")
            self._ledger(order, "revenue", order["amount"], f"{order_id}:revenue")
            if order["commission"]:
                self._ledger(order, "commission", order["commission"], f"{order_id}:commission")
            if order["product_cost"]:
                self._ledger(order, "realized_cost", order["product_cost"], f"{order_id}:cost")
        return order

    def fail_payment(self, *, order_id: str, gateway_event_id: str, reason: str, now=None) -> dict:
        return self._transition(order_id, FAILED, event_id=gateway_event_id, evidence=reason,
                                now=now or time.time())[0]

    def confirm_delivery(self, *, order_id: str, delivery_event_id: str, evidence: str, now=None) -> dict:
        if not evidence:
            raise JourneyViolation("entrega exige evidência (acesso liberado, rastreio entregue...)")
        return self._transition(order_id, DELIVERED, event_id=delivery_event_id, evidence=evidence,
                                now=now or time.time())[0]

    def post_sale(self, *, order_id: str, event_id: str, note: str, now=None) -> dict:
        return self._transition(order_id, POST_SALE, event_id=event_id, evidence=note, now=now or time.time())[0]

    def refund(self, *, order_id: str, gateway_event_id: str, amount: float, reason: str, now=None) -> dict:
        order = self.get(order_id)
        if gateway_event_id not in order["processed_events"] and not (0 < amount <= order["amount"]):
            raise JourneyViolation("reembolso deve ser positivo e até o valor pago")
        order, applied = self._transition(order_id, REFUNDED, event_id=gateway_event_id, evidence=reason,
                                          now=now or time.time(), content={"amount": round(amount, 2)})
        if applied:
            self._event(order, "refund", amount=amount, key=f"{order_id}:refund")
            self._ledger(order, "refund", amount, f"{order_id}:refund")
        return order

    def mark_retained(self, *, lead_id: str, now=None) -> list:
        """RETAINED só com RECOMPRA real: o lead tem um pedido PAGO posterior."""
        now = now or time.time()
        orders = sorted((o for o in self.all() if o["lead_id"] == lead_id), key=lambda o: o["created_at"])
        paid = [o for o in orders if any(h["to"] == PAID for h in o["history"])]
        changed = []
        for older in paid[:-1]:
            if older["status"] == POST_SALE:
                changed.append(self._transition(older["order_id"], RETAINED, event_id=f"retained:{paid[-1]['order_id']}",
                                                evidence=f"recompra real: pedido {paid[-1]['order_id']} PAGO",
                                                now=now)[0])
        return changed

    def recovery_candidates(self, *, now=None, abandon_after_seconds: float = 3600.0) -> list:
        """Carrinho abandonado (checkout sem pagamento além do prazo) e
        pagamento falho. A AÇÃO de recuperação (mensagem, cupom) é externa:
        passa pelo NAVIGATOR/Owner (Comando 3) — aqui só se identifica."""
        now = time.time() if now is None else now
        out = []
        for o in self.all():
            if o["status"] == CHECKOUT_STARTED and now - o["created_at"] >= abandon_after_seconds:
                out.append({"order_id": o["order_id"], "lead_id": o["lead_id"], "motivo": "CARRINHO_ABANDONADO"})
            elif o["status"] == FAILED:
                out.append({"order_id": o["order_id"], "lead_id": o["lead_id"], "motivo": "PAGAMENTO_FALHO",
                            "detalhe": o["history"][-1]["evidence"]})
        return out


# ----------------------------------------------------------------------
# ECONOMIA UNITÁRIA — faturamento ≠ lucro
# ----------------------------------------------------------------------
def unit_economics(*, finance_snapshot, paid_orders: int, customers: int, clicks: int | None,
                   acquisition_spend: tuple | None) -> dict:
    """`acquisition_spend` = (valor, origem) — gasto de AQUISIÇÃO declarado
    com origem; sem ele, CAC é UNKNOWN (nunca estimado). LTV é o OBSERVADO
    até agora (receita líquida por cliente pagante)."""
    snap = finance_snapshot
    costs = snap.expenses + snap.realized_cost + snap.commission
    out = {
        "gross_revenue": round(snap.revenue, 2), "refunds": round(snap.refunds, 2),
        "net_revenue": round(snap.net_revenue, 2), "costs": round(costs, 2), "margin": round(snap.margin, 2),
        "margin_pct": round(snap.margin / snap.net_revenue, 4) if snap.net_revenue > 0 else "UNKNOWN",
        "refund_rate": round(snap.refunds / snap.revenue, 4) if snap.revenue > 0 else "UNKNOWN",
        "conversion_rate": round(paid_orders / clicks, 6) if clicks else "UNKNOWN",
        "roi": round(snap.margin / costs, 4) if costs > 0 else "UNKNOWN",
        "cac": "UNKNOWN", "cac_origin": None,
        "ltv_observed": round(snap.net_revenue / customers, 2) if customers > 0 else "UNKNOWN",
    }
    if acquisition_spend is not None and customers > 0:
        value, origin = acquisition_spend
        if not origin:
            raise ValueError("gasto de aquisição exige origem declarada")
        out["cac"], out["cac_origin"] = round(value / customers, 2), origin
    if snap.revenue <= 0:
        verdict = "INSUFFICIENT_DATA"
    elif snap.margin <= 0:
        verdict = "REVENUE_WITHOUT_MARGIN"              # fatura, mas destrói margem: NÃO é sucesso
    elif out["roi"] == "UNKNOWN":
        verdict = "INSUFFICIENT_DATA"
    else:
        verdict = "ECONOMIC_SUCCESS" if out["roi"] > 0 else "REVENUE_WITHOUT_MARGIN"
    out["verdict"] = verdict
    return out


# ----------------------------------------------------------------------
# ESCADA DE DISTRIBUIÇÃO — publicado → distribuído → visualizado → engajado → convertido
# ----------------------------------------------------------------------
def distribution_ladder(twin, *, campaign_id: str, publication_confirmed: bool, now=None) -> dict:
    """Cada degrau só é afirmado com FATO vigente do Gêmeo (eventos reais da
    campanha). Sem fato: DESCONHECIDO — nunca zero, nunca estimado.
    `publication_confirmed` vem da confirmação REAL do executor (CONFIRMED
    no Comando 3); requisição enviada/aceita não conta."""
    from atlas.digital_twin import ReadingStatus

    def fact(attr):
        rd = twin.reading("campaign", campaign_id, attr, now=now)
        return rd.fact_value if rd.status == ReadingStatus.CURRENT else None

    stages = {"PUBLISHED": True if publication_confirmed else None,
              "DISTRIBUTED": fact("events.impression"), "VIEWED": fact("events.view"),
              "ENGAGED": fact("events.click"), "CONVERTED": fact("events.sale")}
    reached = None
    for name, value in stages.items():
        if value:
            reached = name
        else:
            break
    return {"stages": {k: ("DESCONHECIDO" if v is None else v) for k, v in stages.items()},
            "highest_confirmed_stage": reached}
