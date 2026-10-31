"""
core.operations.cost_guard — medição e LIMITES de custo de IA/API.
- Toda chamada paga registra provedor, serviço, operação, tokens, unidades,
  custo (ou COST_UNKNOWN — nunca inventado), moeda, escopo e sucesso.
- Provedor pago SEM limite configurado => PAID_PROVIDER_NOT_AUTHORIZED (padrão).
- Estimativa desconhecida não pode ser comparada a limite => COST_UNKNOWN (bloqueia).
- Kill switch ativo => BLOCKED_BY_GLOBAL_KILL_SWITCH.
Limites: por chamada, por dia (provedor), por projeto, por campanha, total do provedor.
"""
from __future__ import annotations

import sqlite3
import time

from core.models import db as db_module
from core.operations import kill_switch

_CONFIGURED_DB: str | None = None
LIMIT_KINDS = ("per_call", "per_day", "per_project", "per_campaign", "provider_total",
               # v1.0.5: teto CONFIÁVEL por chamada definido pelo Owner. Só com ele um
               # custo real desconhecido é aceito — e a reserva é, no mínimo, esse teto.
               "unknown_cost_upper_bound")
# incidentes que afetam integridade financeira: só saem por RECONCILIAÇÃO
FINANCIAL_INCIDENT_KINDS = ("COST_OVERRUN", "INVALID_REPORTED_COST", "CURRENCY_MISMATCH_INCIDENT", "SETTLEMENT_FAILED",
                            "COST_UNKNOWN_NO_UPPER_BOUND", "SETTLEMENT_REPLAY_CONFLICT", "SETTLEMENT_STATE_CONFLICT",
                            "USAGE_COST_UNKNOWN")


class SettlementConflict(ValueError):
    """Mesma reserva liquidada de novo com conteúdo DIFERENTE (v1.0.5)."""


class ReconciliationConflict(ValueError):
    """RECONCILIATION_CONFLICT (v1.0.6): incidente já reconciliado com conteúdo diferente."""


@__import__("contextlib").contextmanager
def _immediate(db_path):
    """Transação `BEGIN IMMEDIATE` (v1.0.10): a MESMA função abre, faz COMMIT no sucesso
    (inclusive em `return` antecipado do bloco), ROLLBACK em qualquer exceção e FECHA a
    conexão — escrita serializada entre threads/processos."""
    conn = db_module.get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# v1.0.12 (P1 da auditoria cirúrgica final 1.0.11): o correlation_id é vinculado ao PAYLOAD
# OPERACIONAL completo (prompt, query, conteúdo, modelo, parâmetros...), não só aos metadados
# financeiros. Campos EXCLUÍDOS (e só estes): identidades, que não são conteúdo da requisição.
FINGERPRINT_EXCLUDED_FIELDS = (
    "correlation_id",   # é a própria identidade da chamada lógica
    "usage_event_id",   # é a identidade da ENTREGA do resultado, não da requisição
)


def _canonical(value):
    """Normalização ESTRITA para JSON canônico. Tipos preservados (1, 1.0, "1" e True são
    distintos); None presente != campo ausente; ordem de listas relevante; tupla = lista.
    Tipos ambíguos (set, bytes, objetos, chaves não-str, NaN/Inf) são RECUSADOS — nunca
    convertidos por str()/repr()."""
    import math

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("REQUEST_NOT_CANONICALIZABLE: número não finito")
        return value
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("REQUEST_NOT_CANONICALIZABLE: chave não-texto")
        return {k: _canonical(v) for k, v in value.items()}
    raise ValueError(f"REQUEST_NOT_CANONICALIZABLE: tipo {type(value).__name__} sem representação canônica")


def canonical_json(value) -> str:
    import json

    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def request_fingerprint(*, provider: str, service: str, operation: str, params: dict) -> str:
    """payload canônico -> JSON canônico -> SHA-256 (v1.0.12)."""
    import hashlib

    doc = {"v": 1, "provider": provider, "service": service, "operation": operation,
           "params": {k: v for k, v in (params or {}).items() if k not in FINGERPRINT_EXCLUDED_FIELDS}}
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def stable_policy_version(limits: list) -> str:
    """Identidade determinística do conjunto de limites vigente (v1.0.9)."""
    import hashlib
    import json

    return hashlib.sha256(json.dumps([[l["kind"], l["scope_id"], l["amount"], l["currency"]] for l in limits],
                                     sort_keys=True).encode()).hexdigest()[:16]


def _valid_upper_bound(value):
    """v1.0.6 (B1): teto confiável SÓ se número real finito ESTRITAMENTE > 0."""
    v = valid_money(value)
    return v if (v is not None and v > 0) else None
OVERRUN_TOLERANCE = 1e-9
# v1.0.9 (B2 da auditoria 1.0.8): REGRA OFICIAL DE MOEDA EFETIVA — toda análise de
# histórico usa o valor/moeda materializados por migração quando existirem.
EFFECTIVE_CURRENCY = "COALESCE(converted_currency, currency)"
EFFECTIVE_AMOUNT = "COALESCE(converted_amount, budget_charge)"
# v1.0.7 (B4): tolerância monetária da reconciliação FX — 0,01 (um centésimo) NA MOEDA DO
# ORÇAMENTO entre `final_amount` e `original_amount × fx_rate + fees + adjustments`.
FX_RECONCILIATION_TOLERANCE = 0.01
MAX_MONEY = 1e12          # v1.0.4: valores extremos são inválidos (nunca comparados a limite)

# v1.0.4 (bloqueador 4): classificação de custo EXPLÍCITA de todo provedor
# externo com I/O. Desconhecido nunca é presumido gratuito.
PROVIDER_COST_POLICY = {
    "core/agents/runtime/http_generation_provider.py": "PAID — geração remota cobrada por chamada",
    "core/agents/runtime/http_search_provider.py": "CONFIGURABLE — paid=True/False obrigatório; sem classificação => PAYMENT_CLASSIFICATION_REQUIRED",
    "core/multimodal/real_webpage_analyzer.py": "FREE — leitura HTTP de página pública, sem cobrança por chamada",
    "bridge/execution/oauth2.py": "FREE — troca/renovação de token OAuth2, sem cobrança por chamada",
    "bridge/execution/platform_adapters.py": "QUOTA — APIs oficiais com cota, sem cobrança por chamada; mídia paga governada pelo SpendGuard",
    "bridge/execution/webhooks.py": "FREE — entrega a endpoint do próprio Owner",
    "bridge/execution/browser_executor.py": "FREE — navegação",
    "core/operations/egress.py": "TRANSPORT — transporte central; a classificação é do chamador",
}

# v1.0.3 — POLÍTICA DE LIMITE (auditoria de readiness 1.0.2): o limite é
# aplicado sobre a RESERVA (estimativa informada antes da chamada). Não é um
# "teto rígido" do gasto real: se o custo real vier ACIMA do reservado (overrun)
# ou vier inválido, é aberto INCIDENTE e o provedor fica BLOQUEADO até o Owner
# revisar. Teto rígido real exige limite configurado no próprio provedor.


def valid_money(value):
    """Valor monetário válido: número real FINITO e >= 0 (bool/str/NaN/±Inf/negativo => None)."""
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) and 0 <= v <= MAX_MONEY else None


# v1.0.8 (B1 da auditoria 1.0.7): POLÍTICA OFICIAL DE MOEDAS. Formato de 3 letras não
# basta — o código precisa estar na lista aceita pelo Growth Studio. Base: BRL, USD, EUR,
# GBP. O Owner pode ampliar via MGS_EXTRA_CURRENCIES SOMENTE com códigos ISO 4217 reais
# desta lista embutida (nenhuma moeda é inventada; código fora dela é ignorado).
SUPPORTED_CURRENCIES_BASE = frozenset({"BRL", "USD", "EUR", "GBP"})
ISO_4217_EXTENSIBLE = frozenset({"JPY", "CAD", "AUD", "CHF", "MXN", "ARS", "CLP", "COP", "PEN", "UYU", "PYG", "BOB",
                                 "CNY", "INR", "NZD", "SEK", "NOK", "DKK"})


def supported_currencies() -> frozenset:
    import os

    extra = {c.strip() for c in (os.environ.get("MGS_EXTRA_CURRENCIES") or "").split(",") if c.strip()}
    return SUPPORTED_CURRENCIES_BASE | (extra & ISO_4217_EXTENSIBLE)


def valid_currency(code) -> bool:
    """Moeda ACEITA pela política (não apenas 3 letras). Sem conversão implícita."""
    import re

    return isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code) is not None and code in supported_currencies()


def configure(db_path: str | None) -> None:
    global _CONFIGURED_DB
    _CONFIGURED_DB = db_path


def configured_db() -> str | None:
    return _CONFIGURED_DB


class CostGuard:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def set_limit(self, *, principal, provider: str, kind: str, amount: float, currency: str, scope_id: str = "*") -> None:
        """v1.0.7 (B1 da auditoria 1.0.6): validação de moeda e gravação numa ÚNICA
        transação `BEGIN IMMEDIATE` — sem TOCTOU. Uma única moeda operacional por
        provedor: gravação concorrente em outra moeda recebe BUDGET_CURRENCY_CONFLICT."""
        from core.security.rbac import Role

        if getattr(principal, "role", None) != Role.OWNER:
            raise PermissionError("somente o OWNER configura limites de custo")
        if not valid_currency(currency):
            raise ValueError(f"UNSUPPORTED_CURRENCY: {currency!r} fora da política {sorted(supported_currencies())}")
        if kind not in LIMIT_KINDS or valid_money(amount) is None:
            raise ValueError(f"limite inválido: {kind}={amount}")
        if kind == "unknown_cost_upper_bound" and _valid_upper_bound(amount) is None:
            raise ValueError("unknown_cost_upper_bound exige valor ESTRITAMENTE > 0 (teto confiável)")
        conn = db_module.get_connection(self.db_path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                other = [r[0] for r in conn.execute(
                    "SELECT DISTINCT currency FROM ai_api_limits WHERE provider = ? AND currency != ?", (provider, currency))]
                foreign = self._foreign_unmigrated(conn, provider, currency)
                if other or foreign:
                    raise ValueError(f"BUDGET_CURRENCY_CONFLICT: provedor '{provider}' já opera em {other or 'outra moeda'} "
                                     f"({foreign} registro(s) financeiro(s)) — mudança exige migrate_budget_currency com FX "
                                     f"real (BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION)")
                conn.execute("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(provider, kind, scope_id) DO UPDATE SET "
                             "amount=excluded.amount, currency=excluded.currency, updated_at=excluded.updated_at, actor=excluded.actor",
                             (provider, kind, scope_id, float(amount), currency, time.time(), principal.subject))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _limits(self, provider):
        with db_module.connect(self.db_path) as conn:
            return {(r["kind"], r["scope_id"]): (r["amount"], r["currency"]) for r in conn.execute(
                "SELECT * FROM ai_api_limits WHERE provider = ?", (provider,)).fetchall()}

    # v1.0.2: o que CONTA contra o orçamento = cobranças registradas
    # (`budget_charge`) + reservas ainda abertas. Custo desconhecido nunca é
    # inventado: numa liquidação sem custo informado, a cobrança é a própria
    # reserva (conservadora) e o custo fica COST_UNKNOWN.
    @staticmethod
    def _committed(conn, where: str, params: tuple, currency: str | None = None) -> float:
        """v1.0.7 (B2/B3): soma pelo valor EFETIVO materializado de cada lançamento
        (`converted_amount`/`converted_currency` gravados NA migração; senão o valor
        nativo). Nenhuma taxa é reaplicada na leitura — migração nova nunca
        reinterpreta o passado. Nunca soma moedas diferentes."""
        if currency is None:
            raise ValueError("cálculo de orçamento exige a moeda (v1.0.6)")
        used = conn.execute(f"SELECT COALESCE(SUM(COALESCE(converted_amount, budget_charge)), 0) FROM ai_api_usage "
                            f"WHERE budget_charge IS NOT NULL AND COALESCE(converted_currency, currency) = ? AND {where}",
                            (currency,) + params).fetchone()[0]
        held = conn.execute(f"SELECT COALESCE(SUM(amount), 0) FROM ai_api_reservations WHERE status = 'RESERVED' "
                            f"AND currency = ? AND {where}", (currency,) + params).fetchone()[0]
        return float(used) + float(held)

    def _foreign_unmigrated(self, conn, provider: str, currency: str) -> int:
        """Registros financeiros do provedor cuja moeda EFETIVA difere de `currency`."""
        return conn.execute(
            "SELECT (SELECT COUNT(*) FROM ai_api_usage u WHERE u.provider = ? "
            "  AND COALESCE(u.converted_currency, u.currency) IS NOT NULL AND COALESCE(u.converted_currency, u.currency) != ? "
            "  AND COALESCE(u.budget_charge, 0) != 0) "
            "+ (SELECT COUNT(*) FROM ai_api_reservations r WHERE r.provider = ? AND r.currency != ? AND r.status IN "
            "  ('RESERVED','HELD_UNKNOWN','HELD_CONFLICT')) "
            "+ (SELECT COUNT(*) FROM ai_api_cost_incidents i WHERE i.provider = ? AND i.status = 'OPEN' AND i.currency IS NOT NULL "
            "  AND i.currency != ? AND i.kind != 'CURRENCY_MISMATCH_INCIDENT')",
            (provider, currency, provider, currency, provider, currency)).fetchone()[0]

    def _assert_currency_consistent(self, provider: str, currency: str) -> None:
        """v1.0.6 (B3): trocar a moeda do orçamento com histórico/limites em outra moeda
        exige `migrate_budget_currency` (FX real, fonte, timestamp, evidência)."""
        with db_module.connect(self.db_path) as conn:
            other_limits = conn.execute("SELECT COUNT(*) FROM ai_api_limits WHERE provider = ? AND currency != ?",
                                        (provider, currency)).fetchone()[0]
            foreign = self._foreign_unmigrated(conn, provider, currency)
        if other_limits or foreign:
            raise ValueError(f"BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION: provedor '{provider}' tem "
                             f"{other_limits} limite(s) e {foreign} registro(s) financeiro(s) em outra moeda — "
                             f"use migrate_budget_currency com FX real; nunca conversão implícita")

    def _spent(self, where: str, params: tuple, currency: str) -> float:
        with db_module.connect(self.db_path) as conn:
            return self._committed(conn, where, params, currency)

    def _decide(self, conn, *, provider, estimated_cost, currency, project, campaign, now) -> dict:
        blocked = kill_switch.block_reason(self.db_path)
        if blocked:
            return {"authorized": False, "code": kill_switch.BLOCK_CODE, "reason": blocked}
        # v1.0.8: a política de moeda vale em TODO caminho e com o motivo correto
        if not valid_currency(currency):
            return {"authorized": False, "code": "INVALID_CURRENCY",
                    "reason": f"moeda ausente/inválida/NÃO SUPORTADA ({currency!r}) — política: {sorted(supported_currencies())}"}
        limits = {(r["kind"], r["scope_id"]): (r["amount"], r["currency"]) for r in conn.execute(
            "SELECT * FROM ai_api_limits WHERE provider = ?", (provider,)).fetchall()}
        if not limits:
            return {"authorized": False, "code": "PAID_PROVIDER_NOT_AUTHORIZED",
                    "reason": f"provedor pago '{provider}' sem limite configurado pelo Owner"}
        if estimated_cost is None:
            return {"authorized": False, "code": "COST_UNKNOWN",
                    "reason": "custo estimado desconhecido — não pode ser comparado aos limites (nunca inventado)"}
        if valid_money(estimated_cost) is None:
            return {"authorized": False, "code": "INVALID_COST",
                    "reason": f"custo estimado inválido ({estimated_cost!r}): exige número real finito >= 0"}
        estimated_cost = valid_money(estimated_cost)
        open_incidents = conn.execute("SELECT COUNT(*) FROM ai_api_cost_incidents WHERE provider = ? AND status = 'OPEN'",
                                      (provider,)).fetchone()[0]
        if open_incidents:
            return {"authorized": False, "code": "COST_INCIDENT_REVIEW_REQUIRED",
                    "reason": f"{open_incidents} incidente(s) de custo aberto(s) (overrun/custo inválido) — revisão do Owner"}
        if any(c != currency for _, c in limits.values()):
            return {"authorized": False, "code": "CURRENCY_MISMATCH", "reason": "moeda do limite difere da estimativa"}
        if any(valid_money(a) is None for a, _ in limits.values()):
            # v1.0.8 (B2): limite legado fora da política monetária (ex.: 1e24) nunca governa
            return {"authorized": False, "code": "MONETARY_LIMIT_EXCEEDED",
                    "reason": f"limite persistido fora da faixa monetária [0, {MAX_MONEY:g}] — revisão do Owner"}
        if self._foreign_unmigrated(conn, provider, currency):
            return {"authorized": False, "code": "BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION",
                    "reason": "histórico financeiro em outra moeda sem migração com FX real — nunca somado 1:1"}
        unknown = conn.execute("SELECT COUNT(*) FROM ai_api_usage WHERE provider = ? AND budget_charge IS NULL",
                               (provider,)).fetchone()[0]
        if unknown:
            ids = [r[0] for r in conn.execute(
                "SELECT i.incident_id FROM ai_api_cost_incidents i JOIN ai_api_usage u ON u.usage_id = i.usage_id "
                "WHERE u.provider = ? AND u.budget_charge IS NULL AND i.status = 'OPEN'", (provider,))]
            return {"authorized": False, "code": "COST_RECONCILIATION_REQUIRED",
                    "reason": f"{unknown} uso(s) com custo desconhecido — reconcilie via reconcile_usage/reconcile_incident "
                              f"(incidentes: {ids or 'executar init_db para criar os incidentes de upgrade'})"}
        if estimated_cost <= 0:
            return {"authorized": False, "code": "INVALID_COST",
                    "reason": "estimativa ZERO não autoriza chamada paga (v1.0.5) — informe estimativa positiva"}
        day_start = now - (now % 86400)
        checks = [
            ("per_call", "*", 0.0),
            ("per_day", "*", self._committed(conn, "provider = ? AND created_at >= ?", (provider, day_start), currency)),
            ("provider_total", "*", self._committed(conn, "provider = ?", (provider,), currency)),
        ]
        if project:
            checks.append(("per_project", project, self._committed(conn, "provider = ? AND project = ?", (provider, project),
                                                                   currency)))
        if campaign:
            checks.append(("per_campaign", campaign, self._committed(conn, "provider = ? AND campaign = ?",
                                                                     (provider, campaign), currency)))
        for kind, scope, spent in checks:
            lim = limits.get((kind, scope)) or (limits.get((kind, "*")) if kind in ("per_project", "per_campaign") else None)
            if lim is not None and spent + estimated_cost > lim[0]:
                return {"authorized": False, "code": "LIMIT_EXCEEDED",
                        "reason": f"{kind}[{scope}]: gasto {spent:.4f} + estimado {estimated_cost:.4f} > limite {lim[0]:.4f} {currency}"}
        return {"authorized": True, "code": "AUTHORIZED", "reason": "dentro dos limites configurados"}

    def preview(self, *, provider: str, service: str, operation: str, estimated_cost: float | None, currency: str,
                project: str | None = None, campaign: str | None = None, now: float | None = None) -> dict:
        """PRÉVIA somente leitura — NÃO autoriza execução."""
        now = time.time() if now is None else now
        with db_module.connect(self.db_path) as conn:
            return self._decide(conn, provider=provider, estimated_cost=estimated_cost, currency=currency,
                                project=project, campaign=campaign, now=now)

    def authorize(self, *, provider: str, service: str, operation: str, estimated_cost: float | None, currency: str,
                  project: str | None = None, campaign: str | None = None, job_id: str | None = None,
                  now: float | None = None, correlation_id: str | None = None) -> dict:
        """v1.0.2: autorizar = RESERVAR atomicamente (nunca mais uma checagem
        solta seguida de registro — a corrida que permitia ultrapassar o
        limite). A reserva é liquidada por `record_usage()`/`settle()`."""
        return self.reserve(provider=provider, service=service, operation=operation, estimated_cost=estimated_cost,
                            currency=currency, project=project, campaign=campaign, job_id=job_id, now=now,
                            correlation_id=correlation_id)

    def reserve(self, *, provider: str, service: str, operation: str, estimated_cost: float | None, currency: str,
                project: str | None = None, campaign: str | None = None, job_id: str | None = None,
                now: float | None = None, correlation_id: str | None = None,
                request_fingerprint: str | None = None) -> dict:
        """v1.0.2 (auditoria de readiness, bloqueador 2): checagem de limites +
        reserva numa ÚNICA transação `BEGIN IMMEDIATE` (escrita serializada
        entre threads e processos) — duas chamadas concorrentes nunca
        ultrapassam o limite. A reserva é PERSISTENTE: se o processo morrer
        antes de liquidar, ela continua contando até o Owner reconciliar."""
        import uuid

        now = time.time() if now is None else now
        conn = db_module.get_connection(self.db_path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                corr_fp = None
                if correlation_id is not None:
                    # v1.0.10 (B1 da auditoria 1.0.9): o correlation_id é vinculado ao PAYLOAD COMPLETO
                    # da reserva — retry só é idempotente se TODOS os campos semânticos coincidirem.
                    from core.governance.idempotency import stable_fingerprint

                    if request_fingerprint is not None:
                        # v1.0.12: identidade = metadados financeiros + PAYLOAD OPERACIONAL canônico
                        corr_fp = stable_fingerprint("reservation_payload_v2", provider, service, operation,
                                                     repr(estimated_cost), currency, project, campaign, job_id,
                                                     request_fingerprint)
                    else:
                        corr_fp = stable_fingerprint("reservation_payload", provider, service, operation,
                                                     repr(estimated_cost), currency, project, campaign, job_id)
                    _prev = conn.execute("SELECT reservation_id, status, correlation_fingerprint FROM ai_api_reservations "
                                         "WHERE correlation_id = ?", (correlation_id,)).fetchone()
                    if _prev is not None:
                        conn.execute("COMMIT")
                        if _prev["correlation_fingerprint"] != corr_fp:
                            _stored = _prev["correlation_fingerprint"] or ""
                            _legacy = not _stored or (request_fingerprint is not None
                                                      and not _stored.startswith('["reservation_payload_v2"'))
                            raise ValueError(f"CORRELATION_ID_CONFLICT: '{correlation_id}' já pertence a uma reserva com "
                                             f"payload {'não comprovável (legado sem fingerprint do payload operacional)' if _legacy else 'DIFERENTE'}"
                                             f" — nenhuma reserva antiga é reaproveitada; revisão explícita")
                        if _prev["status"] == "RESERVED":            # lifecycle: aberta
                            return {"authorized": True, "code": "ALREADY_RESERVED", "reason": "reserva já existente "
                                    "(retry idêntico)", "reservation_id": _prev["reservation_id"], "already_reserved": True}
                        # lifecycle: terminal/pendente de conciliação — NUNCA uma nova autorização
                        return {"authorized": False, "code": "ALREADY_COMPLETED",
                                "reason": f"operação já encerrada ({_prev['status']}) — não é nova autorização",
                                "reservation_id": _prev["reservation_id"], "status": _prev["status"]}
                ub = conn.execute("SELECT amount FROM ai_api_limits WHERE provider = ? AND kind = 'unknown_cost_upper_bound' "
                                  "AND scope_id = '*'", (provider,)).fetchone()
                ub_valid = _valid_upper_bound(ub[0]) if ub is not None else None   # v1.0.6: validado também no USO
                if ub_valid is not None and valid_money(estimated_cost) is not None and estimated_cost > 0:
                    estimated_cost = max(float(estimated_cost), ub_valid)   # reserva >= teto confiável
                decision = self._decide(conn, provider=provider, estimated_cost=estimated_cost, currency=currency,
                                        project=project, campaign=campaign, now=now)
                if decision["authorized"]:
                    rid = f"rsv_{uuid.uuid4().hex[:16]}"
                    conn.execute(
                        "INSERT INTO ai_api_reservations (reservation_id, provider, service, operation, amount, currency, "
                        "project, campaign, job_id, status, created_at, correlation_id, correlation_fingerprint) "
                        "VALUES (?,?,?,?,?,?,?,?,?,'RESERVED',?,?,?)",
                        (rid, provider, service, operation, float(estimated_cost), currency, project, campaign, job_id, now,
                         correlation_id, corr_fp))
                    decision = dict(decision, reservation_id=rid)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:          # v1.0.5: nunca mascara a exceção original
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return decision

    def settle(self, reservation_id: str, *, outcome: str, success: bool, actual_cost: float | None = None,
               actual_currency: str | None = None, input_tokens: int | None = None, output_tokens: int | None = None,
               units: float | None = None, detail: str = "", cost_confirmed: bool = False,
               usage_event_id: str | None = None, usage_event_fingerprint: str | None = None) -> dict:
        """Liquida a reserva conforme o DESFECHO REAL (idempotente):
        EXECUTED -> custo real (ou COST_UNKNOWN) e cobrança = custo real, ou a
        reserva quando o custo não foi informado; NOT_EXECUTED -> reserva
        liberada (cobrança 0); UNKNOWN (ambíguo) -> cobrança MANTIDA."""
        if outcome not in ("EXECUTED", "NOT_EXECUTED", "UNKNOWN"):
            raise ValueError(f"desfecho inválido: {outcome}")
        now = time.time()
        conn = db_module.get_connection(self.db_path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                r = conn.execute("SELECT * FROM ai_api_reservations WHERE reservation_id = ?", (reservation_id,)).fetchone()
                if r is None:
                    raise KeyError(reservation_id)
                from core.governance.idempotency import stable_fingerprint

                fingerprint = stable_fingerprint("settle", reservation_id, outcome, bool(success), repr(actual_cost),
                                                 actual_currency, bool(cost_confirmed))
                if r["status"] != "RESERVED":
                    stored = r["settlement_fingerprint"] if "settlement_fingerprint" in r.keys() else None
                    if stored is not None and stored == fingerprint:
                        conn.execute("COMMIT")
                        return {"reservation_id": reservation_id, "status": r["status"], "already_settled": True}
                    # v1.0.5 (bloqueador 4): replay DIVERGENTE (ou original não comprovável) => conflito + incidente
                    import uuid as _uuid

                    conn.execute("INSERT INTO ai_api_cost_incidents (incident_id, provider, reservation_id, kind, "
                                 "reserved_amount, reported_cost, currency, status, created_at) VALUES (?,?,?,?,?,?,?,'OPEN',?)",
                                 (f"inc_{_uuid.uuid4().hex[:12]}", r["provider"], reservation_id, "SETTLEMENT_REPLAY_CONFLICT",
                                  r["amount"], f"{outcome} {actual_cost!r} {actual_currency!r}", r["currency"], now))
                    conn.execute("COMMIT")
                    raise SettlementConflict(
                        f"reserva {reservation_id} já liquidada com conteúdo {'DIFERENTE' if stored else 'não comprovável'}"
                        f" — replay rejeitado; incidente aberto")
                incident = None
                invalid_reported = actual_cost is not None and valid_money(actual_cost) is None
                known = actual_cost is not None and not invalid_reported and (actual_currency in (None, r["currency"]))
                real_cost = valid_money(actual_cost) if (actual_cost is not None and actual_currency == r["currency"]) else None
                if outcome == "NOT_EXECUTED" and ((real_cost is not None and real_cost > 0) or success):
                    # v1.0.6 (B4): "não executado" com custo real > 0 (ou sucesso) é CONTRADITÓRIO:
                    # nunca libera orçamento nem descarta o custo informado.
                    cost = real_cost
                    charge = max(float(r["amount"]), real_cost or 0.0)
                    cstatus, rstatus = "SETTLEMENT_STATE_CONFLICT", "HELD_CONFLICT"
                    incident = ("SETTLEMENT_STATE_CONFLICT", f"NOT_EXECUTED success={success} cost={actual_cost!r}")
                elif outcome == "UNKNOWN" and real_cost is not None:
                    # v1.0.6 (B4): desfecho ambíguo com custo real conhecido — o custo é PRESERVADO
                    # e contabilizado (nunca abaixo da reserva) e o conflito vai para revisão.
                    cost = real_cost
                    charge = max(float(r["amount"]), real_cost)
                    cstatus, rstatus = "SETTLEMENT_STATE_CONFLICT", "HELD_CONFLICT"
                    incident = ("SETTLEMENT_STATE_CONFLICT", f"UNKNOWN com custo {actual_cost!r}")
                elif outcome == "NOT_EXECUTED":
                    cost, charge, cstatus, rstatus = None, 0.0, "NOT_EXECUTED", "RELEASED"
                elif invalid_reported:
                    # v1.0.3: custo negativo/não finito reportado NUNCA reduz o gasto:
                    # cobra a reserva e abre incidente (provedor bloqueado até revisão).
                    cost, charge, cstatus = None, float(r["amount"]), "COST_INVALID_REPORTED"
                    rstatus = "COMMITTED" if outcome == "EXECUTED" else "HELD_UNKNOWN"
                    incident = ("INVALID_REPORTED_COST", repr(actual_cost))
                elif outcome in ("EXECUTED", "UNKNOWN") and actual_cost is not None and \
                        actual_currency != r["currency"]:
                    # v1.0.4 (bloqueador 3): moeda do custo real divergente (ou ausente)
                    # da moeda reservada — NUNCA conversão implícita: cobra a reserva,
                    # abre incidente e bloqueia o provedor até revisão do Owner.
                    cost, charge, cstatus = None, float(r["amount"]), "CURRENCY_MISMATCH"
                    rstatus = "COMMITTED" if outcome == "EXECUTED" else "HELD_UNKNOWN"
                    incident = ("CURRENCY_MISMATCH_INCIDENT", f"{actual_cost!r} {actual_currency!r}")
                elif outcome == "EXECUTED" and known:
                    cost, charge, rstatus = float(actual_cost), float(actual_cost), "COMMITTED"
                    cstatus = "CONFIRMED" if cost_confirmed else "ESTIMATED"   # nunca exagera a confirmação
                    if cost > float(r["amount"]) + OVERRUN_TOLERANCE:
                        incident = ("COST_OVERRUN", repr(cost))   # gasto real acima do reservado
                else:
                    cost, charge, cstatus = None, float(r["amount"]), "COST_UNKNOWN"
                    rstatus = "COMMITTED" if outcome == "EXECUTED" else "HELD_UNKNOWN"
                    _ubrow = conn.execute(
                        "SELECT amount FROM ai_api_limits WHERE provider = ? AND kind = 'unknown_cost_upper_bound' AND scope_id = '*'",
                        (r["provider"],)).fetchone()
                    has_bound = _ubrow is not None and _valid_upper_bound(_ubrow[0]) is not None   # zero/legado => sem teto
                    if outcome == "EXECUTED" and not has_bound:
                        # v1.0.5 (bloqueador 2): executado, custo real desconhecido e SEM teto
                        # confiável do Owner — cobrança pela estimativa do chamador não é confiável.
                        incident = ("COST_UNKNOWN_NO_UPPER_BOUND", "None")
                if incident is not None:
                    import uuid as _uuid

                    _iid = f"inc_{_uuid.uuid4().hex[:12]}"
                    conn.execute("INSERT INTO ai_api_cost_incidents (incident_id, provider, reservation_id, kind, "
                                 "reserved_amount, reported_cost, currency, status, created_at) "
                                 "VALUES (?,?,?,?,?,?,?,'OPEN',?)",
                                 (_iid, r["provider"], reservation_id, incident[0], r["amount"],
                                  incident[1], actual_currency or r["currency"], now))
                    if incident[0] == "CURRENCY_MISMATCH_INCIDENT":
                        # v1.0.7 (B4): custo original ESTRUTURADO (base da validação matemática FX)
                        conn.execute("UPDATE ai_api_cost_incidents SET original_amount = ?, original_currency = ? "
                                     "WHERE incident_id = ?", (valid_money(actual_cost), actual_currency, _iid))
                conn.execute(
                    "INSERT INTO ai_api_usage (provider, service, operation, created_at, input_tokens, output_tokens, units, "
                    "cost, cost_status, currency, job_id, project, campaign, success, budget_charge, reservation_id, "
                    "usage_event_id, usage_event_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["provider"], r["service"], r["operation"], now, input_tokens, output_tokens, units, cost, cstatus,
                     r["currency"], r["job_id"], r["project"], r["campaign"], int(bool(success)), charge, reservation_id,
                     usage_event_id, usage_event_fingerprint))
                conn.execute("UPDATE ai_api_reservations SET status = ?, settled_at = ?, outcome = ?, detail = ?, "
                             "settlement_fingerprint = ? WHERE reservation_id = ?",
                             (rstatus, now, outcome, detail[:500], fingerprint, reservation_id))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:          # v1.0.5: nunca mascara a exceção original
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return {"reservation_id": reservation_id, "status": rstatus, "cost_status": cstatus, "budget_charge": charge}

    def open_incident(self, *, provider: str, kind: str, reservation_id: str | None = None,
                      reserved_amount: float | None = None, detail: str = "") -> str:
        import uuid

        iid = f"inc_{uuid.uuid4().hex[:12]}"
        with db_module.connect(self.db_path) as conn:
            conn.execute("INSERT INTO ai_api_cost_incidents (incident_id, provider, reservation_id, kind, reserved_amount, "
                         "reported_cost, status, created_at) VALUES (?,?,?,?,?,?,'OPEN',?)",
                         (iid, provider, reservation_id, kind, reserved_amount, detail[:500], time.time()))
        return iid

    def record_free_call(self, *, provider: str, operation: str, success: bool) -> None:
        """Provedor EXPLICITAMENTE gratuito: auditoria da chamada, sem consumir orçamento."""
        with db_module.connect(self.db_path) as conn:
            conn.execute("INSERT INTO ai_api_usage (provider, service, operation, created_at, cost, cost_status, success, "
                         "budget_charge) VALUES (?, ?, ?, ?, 0, 'FREE', ?, 0)",
                         (provider, operation, operation, time.time(), int(bool(success))))

    def open_incidents(self, provider: str | None = None) -> list:
        with db_module.connect(self.db_path) as conn:
            q = "SELECT * FROM ai_api_cost_incidents WHERE status = 'OPEN'" + (" AND provider = ?" if provider else "")
            return [dict(r) for r in conn.execute(q + " ORDER BY created_at", (provider,) if provider else ())]

    def reconcile_incident(self, incident_id: str, *, principal, evidence: str, source: str,
                           final_amount: float | None = None, final_currency: str | None = None, write_off: bool = False,
                           fx_rate: float | None = None, fx_source: str | None = None, fx_timestamp: str | None = None,
                           fees: float = 0.0, adjustments: float = 0.0) -> dict:
        """v1.0.5 (B3 da auditoria 1.0.4) + v1.0.6 (B2 da auditoria 1.0.5): RECONCILIAÇÃO
        financeira ATÔMICA e IDEMPOTENTE. Numa única transação `BEGIN IMMEDIATE`:
        reivindica o incidente (status OPEN), corrige o ledger e grava a resolução com
        fingerprint. Mesmo conteúdo = retry idempotente; conteúdo divergente (ou original
        sem fingerprint) = ReconciliationConflict — nunca sobrescreve a 1ª reconciliação."""
        from core.governance.idempotency import stable_fingerprint
        from core.security.rbac import Role

        if getattr(principal, "role", None) != Role.OWNER:
            raise PermissionError("somente o OWNER reconcilia incidentes de custo")
        if not (evidence or "").strip() or not (source or "").strip():
            raise ValueError("reconciliação exige evidência E fonte")
        import math as _math

        if valid_money(fees) is None:
            raise ValueError(f"fees inválidas ({fees!r}): exige número real finito >= 0")
        if isinstance(adjustments, bool) or not isinstance(adjustments, (int, float)) or not _math.isfinite(adjustments) \
                or abs(adjustments) > MAX_MONEY:
            raise ValueError(f"adjustments inválidos ({adjustments!r}): exige número real finito com |valor| <= MAX_MONEY")
        fp = stable_fingerprint("reconcile_incident", incident_id, bool(write_off), repr(final_amount), final_currency,
                                repr(fx_rate), fx_source, fx_timestamp, repr(float(fees)), repr(float(adjustments)))
        now = time.time()
        conn = db_module.get_connection(self.db_path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                inc = conn.execute("SELECT * FROM ai_api_cost_incidents WHERE incident_id = ?", (incident_id,)).fetchone()
                if inc is None:
                    raise KeyError(f"incidente {incident_id} inexistente")
                if inc["status"] != "OPEN":
                    stored = inc["reconciliation_fingerprint"] if "reconciliation_fingerprint" in inc.keys() else None
                    conn.execute("COMMIT")
                    if stored is not None and stored == fp:
                        return {"incident_id": incident_id, "status": inc["status"], "already_reconciled": True,
                                "final_amount": inc["final_amount"]}
                    raise ReconciliationConflict(
                        f"RECONCILIATION_CONFLICT: incidente {incident_id} já encerrado com conteúdo "
                        f"{'DIFERENTE' if stored else 'não comprovável'} — a 1ª reconciliação é preservada")
                rsv = conn.execute("SELECT * FROM ai_api_reservations WHERE reservation_id = ?",
                                   (inc["reservation_id"],)).fetchone() if inc["reservation_id"] else None
                if rsv is not None:
                    budget_currency = rsv["currency"]
                elif inc["usage_id"] is not None:
                    _ops = {r[0] for r in conn.execute("SELECT DISTINCT currency FROM ai_api_limits WHERE provider = ?",
                                                        (inc["provider"],))}
                    if len(_ops) != 1:
                        raise ValueError(f"moeda operacional de '{inc['provider']}' indefinida ({sorted(_ops)}) — "
                                         f"configure os limites antes de reconciliar o uso")
                    budget_currency = next(iter(_ops))
                else:
                    budget_currency = inc["currency"]
                if not valid_currency(budget_currency):
                    # v1.0.10 (B5 da auditoria 1.0.9): nenhum estado financeiro NOVO em moeda fora da
                    # política — moeda legada só pelo fluxo reconcile_unsupported_currency_history.
                    raise ValueError(f"UNSUPPORTED_CURRENCY: orçamento em {budget_currency!r} fora da política — use "
                                     f"reconcile_unsupported_currency_history antes")
                if write_off:
                    final_amount, kind = None, "WRITE_OFF"
                else:
                    if not valid_currency(final_currency):
                        raise ValueError(f"UNSUPPORTED_CURRENCY: final_currency {final_currency!r} fora da política")
                    if valid_money(final_amount) is None or final_currency != budget_currency:
                        raise ValueError(f"valor final comprovado exigido na moeda do orçamento ({budget_currency}); "
                                         f"recebido {final_amount!r} {final_currency!r} — sem conversão implícita")
                    expected = None
                    if inc["kind"] == "CURRENCY_MISMATCH_INCIDENT":
                        if not (valid_money(fx_rate) not in (None, 0.0) and (fx_source or "").strip()
                                and (fx_timestamp or "").strip()):
                            raise ValueError("custo original em outra moeda: conversão exige fx_rate > 0, fx_source e "
                                             "fx_timestamp")
                        orig_amount, orig_cur = inc["original_amount"], inc["original_currency"]
                        if valid_money(orig_amount) is None or not valid_currency(orig_cur):
                            raise ValueError("FX_RECONCILIATION_MISMATCH: custo original não está estruturado (registro "
                                             "legado ou moeda ausente) — a conta não pode ser verificada; use write-off")
                        # v1.0.7 (B4): a taxa NÃO é metadado — o valor final precisa fechar a conta
                        expected = float(orig_amount) * float(fx_rate) + float(fees) + float(adjustments)
                        if abs(expected - float(final_amount)) > FX_RECONCILIATION_TOLERANCE:
                            raise ValueError(f"FX_RECONCILIATION_MISMATCH: {orig_amount} {orig_cur} × {fx_rate} + fees "
                                             f"{fees} + ajustes {adjustments} = {expected:.4f} {budget_currency}, mas "
                                             f"final_amount = {final_amount} (tolerância {FX_RECONCILIATION_TOLERANCE})")
                    kind = "FINAL_AMOUNT"
                if rsv is not None and write_off and rsv["status"] in ("RESERVED", "HELD_UNKNOWN", "HELD_CONFLICT"):
                    # v1.0.8 (B3 da auditoria 1.0.7): write-off encerra também a RESERVA, no mesmo
                    # commit, em estado terminal WRITTEN_OFF; a cobrança conservadora é MANTIDA.
                    if rsv["status"] == "RESERVED":
                        conn.execute("INSERT INTO ai_api_usage (provider, service, operation, created_at, cost, cost_status, "
                                     "currency, job_id, project, campaign, success, budget_charge, reservation_id) "
                                     "VALUES (?,?,?,?,NULL,'WRITTEN_OFF',?,?,?,?,0,?,?)",
                                     (rsv["provider"], rsv["service"], rsv["operation"], now, rsv["currency"], rsv["job_id"],
                                      rsv["project"], rsv["campaign"], rsv["amount"], rsv["reservation_id"]))
                    else:
                        conn.execute("UPDATE ai_api_usage SET cost_status = 'WRITTEN_OFF' WHERE reservation_id = ?",
                                     (rsv["reservation_id"],))
                    _w = conn.execute("UPDATE ai_api_reservations SET status = 'WRITTEN_OFF', settled_at = ?, detail = ? "
                                      "WHERE reservation_id = ? AND status = ?",
                                      (now, f"write-off {incident_id}", rsv["reservation_id"], rsv["status"]))
                    if _w.rowcount != 1:
                        raise ReconciliationConflict(f"RECONCILIATION_CONFLICT: reserva {rsv['reservation_id']} mudou de estado")
                if rsv is None and inc["usage_id"] is not None:
                    # v1.0.8 (B4): incidente de USO sem custo — corrige o próprio lançamento
                    _ev_rate = 1.0
                    if inc["snapshot_currency"] is not None and inc["snapshot_currency"] != budget_currency:
                        # v1.0.10: evento numa moeda depois MIGRADA/RECUPERADA — vale a taxa REGISTRADA
                        # na migração (única) de snapshot_currency para a moeda atual; nunca 1:1.
                        _mg = conn.execute("SELECT fx_rate FROM ai_api_currency_migrations WHERE provider = ? AND "
                                           "from_currency = ? AND to_currency = ?",
                                           (inc["provider"], inc["snapshot_currency"], budget_currency)).fetchall()
                        if len(_mg) != 1:
                            raise ValueError(f"moeda do evento ({inc['snapshot_currency']}) difere da operacional atual "
                                             f"({budget_currency}) sem migração única registrada — reconciliação de moeda "
                                             f"exigida antes")
                        _ev_rate = float(_mg[0]["fx_rate"])
                    if write_off:
                        # v1.0.9 (B3): usa o teto CONGELADO no evento, nunca o limite corrente
                        if inc["policy_snapshot"] is None:
                            raise ValueError("LEGACY_SNAPSHOT_MISSING: evento sem snapshot de política (legado) — em "
                                             "revisão: informe valor final comprovado; o teto atual não é usado")
                        _charge = _valid_upper_bound(inc["snapshot_upper_bound"])
                        _charge = _charge * _ev_rate if _charge is not None else None
                        if _charge is None:
                            raise ValueError("write-off de uso sem custo exige o teto confiável VIGENTE NO EVENTO (não havia) "
                                             "ou valor final comprovado — nunca custo presumido zero")
                        conn.execute("UPDATE ai_api_usage SET budget_charge = ?, currency = ?, cost_status = 'WRITTEN_OFF' "
                                     "WHERE usage_id = ?", (_charge, budget_currency, inc["usage_id"]))
                    else:
                        conn.execute("UPDATE ai_api_usage SET cost = ?, currency = ?, budget_charge = ?, "
                                     "cost_status = 'RECONCILED' WHERE usage_id = ?",
                                     (final_amount, final_currency, final_amount, inc["usage_id"]))
                if rsv is not None and not write_off:
                    if rsv["status"] == "RESERVED":
                        conn.execute("UPDATE ai_api_reservations SET status = 'COMMITTED', settled_at = ?, outcome = 'EXECUTED', "
                                     "detail = ?, settlement_fingerprint = 'RECONCILED' WHERE reservation_id = ? AND status = 'RESERVED'",
                                     (now, f"reconciliação {incident_id}", rsv["reservation_id"]))
                        conn.execute("INSERT INTO ai_api_usage (provider, service, operation, created_at, cost, cost_status, "
                                     "currency, job_id, project, campaign, success, budget_charge, reservation_id) "
                                     "VALUES (?,?,?,?,?,'RECONCILED',?,?,?,?,1,?,?)",
                                     (rsv["provider"], rsv["service"], rsv["operation"], now, final_amount, rsv["currency"],
                                      rsv["job_id"], rsv["project"], rsv["campaign"], final_amount, rsv["reservation_id"]))
                    else:
                        conn.execute("UPDATE ai_api_usage SET budget_charge = ?, cost = ?, cost_status = 'RECONCILED' "
                                     "WHERE reservation_id = ?", (final_amount, final_amount, rsv["reservation_id"]))
                        if rsv["status"] in ("HELD_UNKNOWN", "HELD_CONFLICT"):
                            conn.execute("UPDATE ai_api_reservations SET status = 'COMMITTED' WHERE reservation_id = ?",
                                         (rsv["reservation_id"],))
                cur = conn.execute(
                    "UPDATE ai_api_cost_incidents SET status = 'RECONCILED', resolved_at = ?, resolved_by = ?, evidence = ?, "
                    "source = ?, reconciliation = ?, final_amount = ?, final_currency = ?, fx_rate = ?, fx_source = ?, "
                    "fx_timestamp = ?, reconciliation_fingerprint = ?, fees = ?, adjustments = ?, expected_amount = ? "
                    "WHERE incident_id = ? AND status = 'OPEN'",
                    (now, principal.subject, evidence[:1000], source[:500], kind, final_amount, final_currency,
                     fx_rate, fx_source, fx_timestamp, fp, float(fees), float(adjustments),
                     None if write_off else expected, incident_id))
                if cur.rowcount != 1:
                    raise ReconciliationConflict(f"RECONCILIATION_CONFLICT: incidente {incident_id} não estava mais OPEN")
                conn.execute("COMMIT")            # ledger + reconciliação + status: UM commit
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return {"incident_id": incident_id, "status": "RECONCILED", "reconciliation": kind, "final_amount": final_amount}

    def migrate_budget_currency(self, *, principal, provider: str, from_currency: str, to_currency: str, fx_rate: float,
                                fx_source: str, fx_timestamp: str, evidence: str) -> dict:
        """v1.0.7 (B2/B3 da auditoria 1.0.6) — MIGRAÇÃO MATERIALIZADA, com composição determinística.
        Numa transação `BEGIN IMMEDIATE`: só migra A PARTIR da moeda operacional atual do provedor;
        cada lançamento recebe seu valor EFETIVO na nova moeda + id da migração + cadeia aplicada
        (ex.: `#1 USD->EUR@0.9 > #2 EUR->GBP@0.8` = ×0.72). Migração posterior age só sobre os
        valores efetivos atuais — nunca reinterpreta o passado. Retry idêntico = idempotente;
        repetir o par com conteúdo diferente = FX_HISTORY_ALREADY_MIGRATED / FX_MIGRATION_CONFLICT."""
        from core.governance.idempotency import stable_fingerprint
        from core.security.rbac import Role

        if getattr(principal, "role", None) != Role.OWNER:
            raise PermissionError("somente o OWNER migra a moeda do orçamento")
        if not (valid_currency(from_currency) and valid_currency(to_currency)) or from_currency == to_currency:
            raise ValueError(f"UNSUPPORTED_CURRENCY: origem/destino inválidos, não suportados ou iguais "
                             f"({from_currency!r}->{to_currency!r}); política {sorted(supported_currencies())}")
        if valid_money(fx_rate) in (None, 0.0) or not (fx_source or "").strip() or not (fx_timestamp or "").strip() \
                or not (evidence or "").strip():
            raise ValueError("migração exige fx_rate > 0 finito, fx_source, fx_timestamp e evidência")
        fp = stable_fingerprint("fx_migration", provider, from_currency, to_currency, repr(float(fx_rate)), fx_source,
                                fx_timestamp)
        conn = db_module.get_connection(self.db_path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                ops = {r[0] for r in conn.execute("SELECT DISTINCT currency FROM ai_api_limits WHERE provider = ?", (provider,))}
                last = conn.execute("SELECT * FROM ai_api_currency_migrations WHERE provider = ? ORDER BY migration_id DESC "
                                    "LIMIT 1", (provider,)).fetchone()
                if ops == {to_currency} and last is not None and last["migration_fingerprint"] == fp:
                    conn.execute("COMMIT")
                    return {"provider": provider, "from": from_currency, "to": to_currency, "fx_rate": fx_rate,
                            "already_migrated": True, "migration_id": last["migration_id"]}
                if ops != {from_currency}:
                    done = conn.execute("SELECT COUNT(*) FROM ai_api_currency_migrations WHERE provider = ? AND "
                                        "from_currency = ? AND to_currency = ?", (provider, from_currency, to_currency)).fetchone()[0]
                    code = "FX_HISTORY_ALREADY_MIGRATED" if done else "FX_MIGRATION_CONFLICT"
                    raise ValueError(f"{code}: moeda operacional atual de '{provider}' é {sorted(ops) or 'indefinida'}, não "
                                     f"{from_currency} — migração só parte da moeda atual; nenhuma reprecificação retroativa")
                open_rsv = conn.execute("SELECT COUNT(*) FROM ai_api_reservations WHERE provider = ? AND status IN "
                                        "('RESERVED','HELD_UNKNOWN','HELD_CONFLICT')", (provider,)).fetchone()[0]
                open_inc = conn.execute("SELECT COUNT(*) FROM ai_api_cost_incidents WHERE provider = ? AND status = 'OPEN'",
                                        (provider,)).fetchone()[0]
                if open_rsv or open_inc:
                    raise ValueError(f"BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION: {open_rsv} reserva(s) e {open_inc} "
                                     f"incidente(s) abertos — reconcilie antes de migrar")
                if self._foreign_unmigrated(conn, provider, from_currency):
                    raise ValueError("FX_MIGRATION_CONFLICT: histórico não consolidado na moeda atual — migração recusada "
                                     "ANTES de criar estado não operacional")
                # v1.0.8 (B2 da auditoria 1.0.7): valida TODOS os valores resultantes ANTES de
                # qualquer escrita — finitos, em [0, MAX_MONEY] e sem underflow de valor positivo.
                import math as _m

                _vals = [r[0] for r in conn.execute(
                    "SELECT COALESCE(converted_amount, budget_charge) FROM ai_api_usage WHERE provider = ? "
                    "AND budget_charge IS NOT NULL AND budget_charge != 0 AND COALESCE(converted_currency, currency) = ?",
                    (provider, from_currency))]
                _vals += [r[0] for r in conn.execute("SELECT amount FROM ai_api_limits WHERE provider = ? AND currency = ?",
                                                     (provider, from_currency))]
                for _v in _vals:
                    _r = float(_v) * float(fx_rate)
                    if not _m.isfinite(_r) or _r < 0 or _r > MAX_MONEY or (float(_v) > 0 and _r == 0.0):
                        raise ValueError(f"FX_CONVERSION_OUT_OF_RANGE: {_v} × {fx_rate} = {_r!r} fora de [0, {MAX_MONEY:g}] "
                                         f"(ou underflow) — migração abortada sem alterar nada")
                cur = conn.execute("INSERT INTO ai_api_currency_migrations (provider, from_currency, to_currency, fx_rate, "
                                   "fx_source, fx_timestamp, evidence, actor, created_at, migration_fingerprint) "
                                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (provider, from_currency, to_currency, float(fx_rate), fx_source, fx_timestamp,
                                    evidence[:1000], principal.subject, time.time(), fp))
                mid = cur.lastrowid
                step = f"#{mid} {from_currency}->{to_currency}@{float(fx_rate)!r}"
                conn.execute("UPDATE ai_api_usage SET converted_amount = COALESCE(converted_amount, budget_charge) * ?, "
                             "converted_currency = ?, fx_migration_id = ?, "
                             "fx_chain = CASE WHEN fx_chain IS NULL THEN ? ELSE fx_chain || ' > ' || ? END "
                             "WHERE provider = ? AND budget_charge IS NOT NULL AND budget_charge != 0 "
                             "AND COALESCE(converted_currency, currency) = ?",
                             (float(fx_rate), to_currency, mid, step, step, provider, from_currency))
                conn.execute("UPDATE ai_api_limits SET amount = amount * ?, currency = ?, updated_at = ?, actor = ? "
                             "WHERE provider = ? AND currency = ?",
                             (float(fx_rate), to_currency, time.time(), principal.subject, provider, from_currency))
                conn.execute("COMMIT")            # histórico materializado + limites + registro: UM commit
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return {"provider": provider, "from": from_currency, "to": to_currency, "fx_rate": fx_rate, "migration_id": mid}

    def materialize_legacy_migration(self, *, principal, provider: str, migration_id: int, evidence: str) -> dict:
        """Upgrade de banco da 1.0.6: migração registrada SEM materialização deixa o histórico
        em moeda estrangeira (bloqueado, fail-closed). O Owner escolhe EXPLICITAMENTE qual
        migração registrada vale; exige que seja a ÚNICA migração legada daquele par."""
        from core.security.rbac import Role

        if getattr(principal, "role", None) != Role.OWNER:
            raise PermissionError("somente o OWNER materializa migração legada")
        if not (evidence or "").strip():
            raise ValueError("materialização exige evidência")
        conn = db_module.get_connection(self.db_path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                m = conn.execute("SELECT * FROM ai_api_currency_migrations WHERE migration_id = ? AND provider = ?",
                                 (migration_id, provider)).fetchone()
                if m is None or m["migration_fingerprint"] is not None:
                    raise ValueError("migração inexistente ou já materializada")
                same = conn.execute("SELECT COUNT(*) FROM ai_api_currency_migrations WHERE provider = ? AND from_currency = ? "
                                    "AND to_currency = ? AND migration_fingerprint IS NULL",
                                    (provider, m["from_currency"], m["to_currency"])).fetchone()[0]
                if same != 1:
                    raise ValueError(f"FX_MIGRATION_CONFLICT: {same} migrações legadas do mesmo par — taxa ambígua; "
                                     f"reconciliação manual exigida")
                step = f"#{migration_id} {m['from_currency']}->{m['to_currency']}@{m['fx_rate']!r} (legado)"
                conn.execute("UPDATE ai_api_usage SET converted_amount = COALESCE(converted_amount, budget_charge) * ?, "
                             "converted_currency = ?, fx_migration_id = ?, fx_chain = ? WHERE provider = ? "
                             "AND budget_charge IS NOT NULL AND budget_charge != 0 AND COALESCE(converted_currency, currency) = ?",
                             (m["fx_rate"], m["to_currency"], migration_id, step, provider, m["from_currency"]))
                conn.execute("UPDATE ai_api_currency_migrations SET migration_fingerprint = ? WHERE migration_id = ?",
                             (f"LEGACY-MATERIALIZED:{principal.subject}:{evidence[:100]}", migration_id))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return {"migration_id": migration_id, "materialized": True}

    def resolve_incident(self, incident_id: str, *, principal, evidence: str) -> dict:
        """Owner revisa o incidente (overrun/custo inválido) com evidência;
        só então o provedor volta a poder reservar."""
        from core.security.rbac import Role

        if getattr(principal, "role", None) != Role.OWNER:
            raise PermissionError("somente o OWNER resolve incidentes de custo")
        if not evidence or not evidence.strip():
            raise ValueError("resolução de incidente exige evidência")
        with db_module.connect(self.db_path) as conn:
            _row = conn.execute("SELECT kind FROM ai_api_cost_incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        if _row is not None and _row["kind"] in FINANCIAL_INCIDENT_KINDS:
            raise ValueError(f"incidente financeiro {_row['kind']} exige RECONCILIAÇÃO (reconcile_incident) com valor "
                             f"final comprovado ou write-off — nunca só troca de status (v1.0.5)")
        with db_module.connect(self.db_path) as conn:
            cur = conn.execute("UPDATE ai_api_cost_incidents SET status = 'RESOLVED', resolved_at = ?, resolved_by = ?, "
                               "evidence = ? WHERE incident_id = ? AND status = 'OPEN'",
                               (time.time(), principal.subject, evidence[:1000], incident_id))
            if cur.rowcount != 1:
                raise KeyError(f"incidente {incident_id} inexistente ou já resolvido")
        return {"incident_id": incident_id, "status": "RESOLVED"}

    def _usage_event_result(self, usage_event_id, ev_fp, reservation_id, correlation_id):
        """Resultado ANTERIOR de um evento já processado (idempotente) ou None se novo.
        Payload ou reserva divergente = USAGE_EVENT_CONFLICT (nada é alterado)."""
        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT u.usage_id, u.cost_status, u.reservation_id, u.usage_event_fingerprint, r.status "
                               "AS rstatus, r.correlation_id FROM ai_api_usage u LEFT JOIN ai_api_reservations r "
                               "ON r.reservation_id = u.reservation_id WHERE u.usage_event_id = ?", (usage_event_id,)).fetchone()
            if row is None:
                return None
            inc = conn.execute("SELECT incident_id FROM ai_api_cost_incidents WHERE usage_id = ? ORDER BY created_at LIMIT 1",
                               (row["usage_id"],)).fetchone()
        if row["usage_event_fingerprint"] != ev_fp:
            raise ValueError(f"USAGE_EVENT_CONFLICT: usage_event_id '{usage_event_id}' já processado com payload "
                             f"diferente — nada foi alterado")
        if (reservation_id is not None and reservation_id != row["reservation_id"]) or \
                (correlation_id is not None and correlation_id != row["correlation_id"]):
            raise ValueError(f"USAGE_EVENT_CONFLICT: usage_event_id '{usage_event_id}' já liquidou a reserva "
                             f"{row['reservation_id']!r} — não liquida outra")
        return {"usage_id": row["usage_id"], "cost_status": row["cost_status"], "reservation_id": row["reservation_id"],
                "settled": row["rstatus"], "incident_id": inc["incident_id"] if inc else None, "already_recorded": True}

    def open_reservations(self) -> list:
        """Reservas REALMENTE pendentes (v1.0.8: inclui HELD_CONFLICT; WRITTEN_OFF é terminal)."""
        with db_module.connect(self.db_path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM ai_api_reservations WHERE status IN ('RESERVED', 'HELD_UNKNOWN', 'HELD_CONFLICT') "
                "ORDER BY created_at")]

    def reconcile_usage(self, usage_id: int, *, principal, evidence: str, source: str, final_amount: float | None = None,
                        final_currency: str | None = None, write_off: bool = False) -> dict:
        """v1.0.8 (B4): reconciliação do USO sem custo — pelo incidente vinculado (atômico/idempotente)."""
        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT incident_id FROM ai_api_cost_incidents WHERE usage_id = ? AND kind = "
                               "'USAGE_COST_UNKNOWN' ORDER BY created_at LIMIT 1", (usage_id,)).fetchone()
        if row is None:
            raise KeyError(f"nenhum incidente de uso vinculado ao usage_id {usage_id}")
        return self.reconcile_incident(row["incident_id"], principal=principal, evidence=evidence, source=source,
                                       final_amount=final_amount, final_currency=final_currency, write_off=write_off)

    def reconcile_unsupported_currency_history(self, *, principal, provider: str, legacy_currency: str,
                                               target_currency: str, fx_rate: float, fx_source: str, fx_timestamp: str,
                                               evidence: str, declared_converted_total: float, fees: float = 0.0,
                                               adjustments: float = 0.0) -> dict:
        """v1.0.9 (B1 da auditoria 1.0.8): RECUPERAÇÃO de histórico em moeda RETIRADA/NÃO
        SUPORTADA. Owner declara moeda destino suportada, FX real (taxa, fonte, timestamp),
        evidência e o TOTAL convertido; a conta precisa fechar:
        |total_efetivo_legado × fx + fees + ajustes − declarado| ≤ tolerância. Numa transação
        `BEGIN IMMEDIATE`: materializa cada lançamento (moeda EFETIVA), converte os limites
        legados, registra a migração e a trilha de auditoria. Idempotente por fingerprint;
        divergência = FX_HISTORY_ALREADY_MIGRATED. Nenhuma conversão é inventada."""
        import json as _json
        import math as _m
        import re as _re
        import uuid as _uuid

        from core.governance.idempotency import stable_fingerprint
        from core.security.rbac import Role

        if getattr(principal, "role", None) != Role.OWNER:
            raise PermissionError("somente o OWNER reconcilia histórico em moeda retirada")
        if not (isinstance(legacy_currency, str) and _re.fullmatch(r"[A-Z]{3}", legacy_currency)):
            raise ValueError("moeda legada malformada")
        if valid_currency(legacy_currency):
            raise ValueError("moeda ainda SUPORTADA — use migrate_budget_currency")
        if not valid_currency(target_currency):
            raise ValueError(f"UNSUPPORTED_CURRENCY: destino {target_currency!r} fora da política")
        if valid_money(fx_rate) in (None, 0.0) or not (fx_source or "").strip() or not (fx_timestamp or "").strip() \
                or not (evidence or "").strip():
            raise ValueError("exige fx_rate > 0 finito, fx_source, fx_timestamp e evidência")
        if valid_money(declared_converted_total) is None or valid_money(fees) is None or isinstance(adjustments, bool) \
                or not isinstance(adjustments, (int, float)) or not _m.isfinite(adjustments) or abs(adjustments) > MAX_MONEY:
            raise ValueError("total declarado/fees/ajustes inválidos")
        if float(fees) + float(adjustments) < -FX_RECONCILIATION_TOLERANCE:
            raise ValueError("fees + ajustes líquidos negativos reduziriam o gasto histórico — recusado (conservador)")
        fp = stable_fingerprint("legacy_currency_reconcile", provider, legacy_currency, target_currency, repr(float(fx_rate)),
                                fx_source, fx_timestamp, repr(float(declared_converted_total)), repr(float(fees)),
                                repr(float(adjustments)))
        now = time.time()
        conn = db_module.get_connection(self.db_path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                prev = conn.execute("SELECT migration_id, migration_fingerprint FROM ai_api_currency_migrations WHERE "
                                    "provider = ? AND from_currency = ? AND to_currency = ?",
                                    (provider, legacy_currency, target_currency)).fetchall()
                if any(p["migration_fingerprint"] == fp for p in prev):
                    conn.execute("COMMIT")
                    return {"provider": provider, "legacy": legacy_currency, "target": target_currency,
                            "already_reconciled": True}
                if prev:
                    raise ValueError(f"FX_HISTORY_ALREADY_MIGRATED: histórico {legacy_currency}->{target_currency} de "
                                     f"'{provider}' já reconciliado com conteúdo diferente — conflito")
                pend = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM ai_api_reservations WHERE provider = ? AND currency = ? AND status IN "
                    "('RESERVED','HELD_UNKNOWN','HELD_CONFLICT')) + (SELECT COUNT(*) FROM ai_api_cost_incidents WHERE "
                    "provider = ? AND status = 'OPEN' AND kind != 'USAGE_COST_UNKNOWN')",
                    (provider, legacy_currency, provider)).fetchone()[0]
                if pend:
                    raise ValueError(f"CURRENCY_RETIREMENT_REQUIRES_RECONCILIATION: {pend} reserva(s)/incidente(s) "
                                     f"pendentes — reconcilie antes")
                others = {r[0] for r in conn.execute("SELECT DISTINCT currency FROM ai_api_limits WHERE provider = ?",
                                                     (provider,))} - {legacy_currency, target_currency}
                if others:
                    raise ValueError(f"BUDGET_CURRENCY_CONFLICT: limites em {sorted(others)} — ambíguo, recusado")
                rows = conn.execute(f"SELECT usage_id, {EFFECTIVE_AMOUNT} AS v FROM ai_api_usage WHERE provider = ? AND "
                                    f"{EFFECTIVE_CURRENCY} = ? AND budget_charge IS NOT NULL AND budget_charge != 0",
                                    (provider, legacy_currency)).fetchall()
                lims = conn.execute("SELECT kind, scope_id, amount FROM ai_api_limits WHERE provider = ? AND currency = ?",
                                    (provider, legacy_currency)).fetchall()
                if not rows and not lims:
                    raise ValueError(f"nada em {legacy_currency} para reconciliar em '{provider}'")
                total = sum(float(r["v"]) for r in rows)
                expected = total * float(fx_rate) + float(fees) + float(adjustments)
                if abs(expected - float(declared_converted_total)) > FX_RECONCILIATION_TOLERANCE:
                    raise ValueError(f"FX_RECONCILIATION_MISMATCH: {total} {legacy_currency} × {fx_rate} + fees {fees} + "
                                     f"ajustes {adjustments} = {expected:.4f} {target_currency}, declarado "
                                     f"{declared_converted_total}")
                for v in [float(r["v"]) for r in rows] + [float(l["amount"]) for l in lims]:
                    r_ = v * float(fx_rate)
                    if not _m.isfinite(r_) or r_ < 0 or r_ > MAX_MONEY or (v > 0 and r_ == 0.0):
                        raise ValueError(f"FX_CONVERSION_OUT_OF_RANGE: {v} × {fx_rate} = {r_!r}")
                cur = conn.execute("INSERT INTO ai_api_currency_migrations (provider, from_currency, to_currency, fx_rate, "
                                   "fx_source, fx_timestamp, evidence, actor, created_at, migration_fingerprint) "
                                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (provider, legacy_currency, target_currency, float(fx_rate), fx_source, fx_timestamp,
                                    evidence[:1000], principal.subject, now, fp))
                mid = cur.lastrowid
                step = f"#{mid} {legacy_currency}->{target_currency}@{float(fx_rate)!r} (reconciliação de moeda retirada)"
                conn.execute(f"UPDATE ai_api_usage SET converted_amount = {EFFECTIVE_AMOUNT} * ?, converted_currency = ?, "
                             f"fx_migration_id = ?, fx_chain = CASE WHEN fx_chain IS NULL THEN ? ELSE fx_chain || ' > ' || ? END "
                             f"WHERE provider = ? AND {EFFECTIVE_CURRENCY} = ? AND budget_charge IS NOT NULL AND budget_charge != 0",
                             (float(fx_rate), target_currency, mid, step, step, provider, legacy_currency))
                extra = float(fees) + float(adjustments)
                if extra > FX_RECONCILIATION_TOLERANCE:
                    conn.execute("INSERT INTO ai_api_usage (provider, service, operation, created_at, cost, cost_status, "
                                 "currency, success, budget_charge) VALUES (?, 'legacy_fx', 'fees_adjustments', ?, ?, "
                                 "'LEGACY_FX_ADJUSTMENT', ?, 1, ?)", (provider, now, extra, target_currency, extra))
                for l in lims:
                    clash = conn.execute("SELECT 1 FROM ai_api_limits WHERE provider = ? AND kind = ? AND scope_id = ? AND "
                                         "currency = ?", (provider, l["kind"], l["scope_id"], target_currency)).fetchone()
                    if clash:   # o limite já existe na moeda destino: o legado sai, o vigente fica
                        conn.execute("DELETE FROM ai_api_limits WHERE provider = ? AND kind = ? AND scope_id = ? AND "
                                     "currency = ?", (provider, l["kind"], l["scope_id"], legacy_currency))
                conn.execute("UPDATE ai_api_limits SET amount = amount * ?, currency = ?, updated_at = ?, actor = ? "
                             "WHERE provider = ? AND currency = ?",
                             (float(fx_rate), target_currency, now, principal.subject, provider, legacy_currency))
                conn.execute("INSERT INTO ai_api_cost_incidents (incident_id, provider, kind, reported_cost, currency, status, "
                             "created_at, resolved_at, resolved_by, evidence, source, fx_rate, fx_source, fx_timestamp, "
                             "original_currency, original_amount, final_amount, final_currency, fees, adjustments, "
                             "expected_amount, reconciliation, reconciliation_fingerprint) "
                             "VALUES (?,?,'LEGACY_CURRENCY_RECONCILED',?,?,'RECONCILED',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                             "'FINAL_AMOUNT',?)",
                             (f"inc_{_uuid.uuid4().hex[:12]}", provider, _json.dumps({"migration_id": mid, "rows": len(rows)}),
                              target_currency, now, now, principal.subject, evidence[:1000], "reconcile_unsupported_currency_history",
                              float(fx_rate), fx_source, fx_timestamp, legacy_currency, total, float(declared_converted_total),
                              target_currency, float(fees), float(adjustments), expected, fp))
                conn.execute("COMMIT")         # histórico + limites + migração + auditoria: UM commit
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return {"provider": provider, "legacy": legacy_currency, "target": target_currency, "migration_id": mid,
                "converted_total": expected}

    def retire_unsupported_currency_limits(self, *, principal, provider: str, evidence: str) -> dict:
        """v1.0.8 (B1): revisão de moeda LEGADA fora da política. Só remove limites em moeda
        não suportada quando NÃO há lançamento, reserva ou incidente nessa moeda (senão exige
        reconciliação/write-off). Registro auditável como incidente encerrado."""
        from core.security.rbac import Role

        if getattr(principal, "role", None) != Role.OWNER:
            raise PermissionError("somente o OWNER revisa moeda legada")
        if not (evidence or "").strip():
            raise ValueError("revisão exige evidência")
        import uuid

        conn = db_module.get_connection(self.db_path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                bad = sorted({r[0] for r in conn.execute("SELECT DISTINCT currency FROM ai_api_limits WHERE provider = ?",
                                                          (provider,)) if not valid_currency(r[0])})
                for code in bad:
                    # v1.0.9 (B2 da auditoria 1.0.8): histórico pela moeda EFETIVA (inclui o
                    # materializado por migração), reservas e incidentes — nunca só a coluna nativa.
                    used = conn.execute(
                        f"SELECT (SELECT COUNT(*) FROM ai_api_usage WHERE provider = ? AND {EFFECTIVE_CURRENCY} = ? AND "
                        f"COALESCE(budget_charge,0) != 0) + (SELECT COUNT(*) FROM ai_api_reservations WHERE provider = ? "
                        f"AND currency = ?) + (SELECT COUNT(*) FROM ai_api_cost_incidents WHERE provider = ? AND "
                        f"status = 'OPEN' AND ? IN (COALESCE(currency,''), COALESCE(snapshot_currency,''), "
                        f"COALESCE(original_currency,''), COALESCE(final_currency,'')))",
                        (provider, code, provider, code, provider, code)).fetchone()[0]
                    if used:
                        raise ValueError(f"CURRENCY_RETIREMENT_REQUIRES_RECONCILIATION: '{code}' tem {used} registro(s) "
                                         f"financeiro(s) efetivo(s) — use reconcile_unsupported_currency_history")
                    conn.execute("DELETE FROM ai_api_limits WHERE provider = ? AND currency = ?", (provider, code))
                    conn.execute("INSERT INTO ai_api_cost_incidents (incident_id, provider, kind, reported_cost, currency, "
                                 "status, created_at, resolved_at, resolved_by, evidence) "
                                 "VALUES (?,?,'LEGACY_CURRENCY_RETIRED',?,?,'RECONCILED',?,?,?,?)",
                                 (f"inc_{uuid.uuid4().hex[:12]}", provider, code, code, time.time(), time.time(),
                                  principal.subject, evidence[:1000]))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return {"provider": provider, "retired": bad}

    def reconcile(self, reservation_id: str, *, principal, executed: bool, actual_cost: float | None, evidence: str) -> dict:
        """Owner reconcilia reserva órfã (crash) ou ambígua, com evidência.
        v1.0.6 (variantes de B2/B4): ATÔMICA (uma transação `BEGIN IMMEDIATE`, reivindicação
        condicionada ao estado) e IDEMPOTENTE por fingerprint — retry idêntico devolve o mesmo
        resultado; retry divergente (ou original não comprovável) é RECONCILIATION_CONFLICT."""
        from core.governance.idempotency import stable_fingerprint
        from core.security.rbac import Role

        if getattr(principal, "role", None) != Role.OWNER:
            raise PermissionError("somente o OWNER reconcilia reservas de custo")
        if not evidence:
            raise ValueError("reconciliação exige evidência")
        if actual_cost is not None and valid_money(actual_cost) is None:
            raise ValueError(f"custo de reconciliação inválido ({actual_cost!r}): exige número real finito >= 0")
        if not executed and actual_cost:
            raise ValueError("SETTLEMENT_STATE_CONFLICT: executed=False com custo real > 0 — contraditório")
        rfp = stable_fingerprint("reconcile_reservation", reservation_id, bool(executed), repr(actual_cost))
        now = time.time()
        conn = db_module.get_connection(self.db_path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                r = conn.execute("SELECT * FROM ai_api_reservations WHERE reservation_id = ?", (reservation_id,)).fetchone()
                if r is None:
                    raise KeyError(reservation_id)
                if not valid_currency(r["currency"]):
                    raise ValueError(f"UNSUPPORTED_CURRENCY: reserva em {r['currency']!r} fora da política — use o fluxo "
                                     f"de moeda legada")
                if r["status"] not in ("RESERVED", "HELD_UNKNOWN"):
                    conn.execute("COMMIT")
                    if r["settlement_fingerprint"] == rfp:
                        return {"reservation_id": reservation_id, "status": r["status"], "already_reconciled": True}
                    raise ReconciliationConflict(f"RECONCILIATION_CONFLICT: reserva {reservation_id} já encerrada "
                                                 f"({r['status']}) com conteúdo diferente ou não comprovável")
                if executed and actual_cost is None:
                    conn.execute("COMMIT")
                    return {"reservation_id": reservation_id, "status": r["status"],
                            "reason": "executado sem custo informado — a cobrança conservadora é mantida"}
                new_status = "COMMITTED" if executed else "RELEASED"
                cur = conn.execute("UPDATE ai_api_reservations SET status = ?, settled_at = ?, outcome = ?, detail = ?, "
                                   "settlement_fingerprint = ? WHERE reservation_id = ? AND status = ?",
                                   (new_status, now, "EXECUTED" if executed else "NOT_EXECUTED",
                                    f"reconciliado pelo Owner: {evidence}"[:500], rfp, reservation_id, r["status"]))
                if cur.rowcount != 1:
                    raise ReconciliationConflict(f"RECONCILIATION_CONFLICT: reserva {reservation_id} mudou de estado")
                charge = float(actual_cost) if executed else 0.0
                if r["status"] == "RESERVED":
                    conn.execute("INSERT INTO ai_api_usage (provider, service, operation, created_at, cost, cost_status, "
                                 "currency, job_id, project, campaign, success, budget_charge, reservation_id) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (r["provider"], r["service"], r["operation"], now, float(actual_cost) if executed else None,
                                  "CONFIRMED" if executed else "NOT_EXECUTED", r["currency"], r["job_id"], r["project"],
                                  r["campaign"], int(bool(executed)), charge, reservation_id))
                else:
                    conn.execute("UPDATE ai_api_usage SET budget_charge = ?, cost = ?, cost_status = ? WHERE reservation_id = ?",
                                 (charge, float(actual_cost) if executed else None,
                                  "CONFIRMED" if executed else "NOT_EXECUTED", reservation_id))
                if executed and float(actual_cost) > float(r["amount"]) + OVERRUN_TOLERANCE:
                    import uuid as _uuid

                    conn.execute("INSERT INTO ai_api_cost_incidents (incident_id, provider, reservation_id, kind, "
                                 "reserved_amount, reported_cost, currency, status, created_at) VALUES (?,?,?,?,?,?,?,'OPEN',?)",
                                 (f"inc_{_uuid.uuid4().hex[:12]}", r["provider"], reservation_id, "COST_OVERRUN",
                                  r["amount"], repr(actual_cost), r["currency"], now))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return {"reservation_id": reservation_id, "status": new_status}

    def record_usage(self, *, provider: str, service: str, operation: str, success: bool, currency: str | None = None,
                     cost: float | None = None, cost_confirmed: bool = False, input_tokens: int | None = None,
                     output_tokens: int | None = None, units: float | None = None, job_id: str | None = None,
                     project: str | None = None, campaign: str | None = None, reservation_id: str | None = None,
                     correlation_id: str | None = None, usage_event_id: str | None = None) -> dict:
        if cost is not None and valid_money(cost) is None:
            raise ValueError(f"custo inválido ({cost!r}): exige número real finito >= 0")
        if cost is not None and not valid_currency(currency):
            raise ValueError(f"custo informado exige moeda válida (ISO 4217), recebido {currency!r}")
        if cost is not None:
            with db_module.connect(self.db_path) as conn:
                _ops = {r[0] for r in conn.execute("SELECT DISTINCT currency FROM ai_api_limits WHERE provider = ?", (provider,))}
            if _ops and _ops != {currency}:
                # v1.0.7 (variante do B1): uso direto em moeda diferente da operacional reabriria a mistura
                raise ValueError(f"BUDGET_CURRENCY_CONFLICT: custo em {currency}, mas '{provider}' opera em {sorted(_ops)}")
        for _n, _v in (("input_tokens", input_tokens), ("output_tokens", output_tokens), ("units", units)):
            if _v is not None and valid_money(_v) is None:
                raise ValueError(f"{_n} inválido ({_v!r})")
        # v1.0.2: registrar uso de uma chamada AUTORIZADA liquida a reserva
        # correspondente (sem cobrança dupla, sem reserva órfã).
        # v1.0.11 (P1 da auditoria cirúrgica 1.0.10): `usage_event_id` é identidade FORTE e é
        # resolvida ANTES de qualquer matching — um evento já processado devolve o resultado
        # anterior e nunca toca outra reserva (nem uma criada depois).
        ev_fp = None
        if usage_event_id is not None:
            if not (isinstance(usage_event_id, str) and usage_event_id.strip()):
                raise ValueError("usage_event_id inválido")
            from core.governance.idempotency import stable_fingerprint as _sfp0

            ev_fp = _sfp0("usage_event", provider, service, operation, bool(success), repr(cost), currency,
                          bool(cost_confirmed), repr(input_tokens), repr(output_tokens), repr(units), job_id, project,
                          campaign)
            prev = self._usage_event_result(usage_event_id, ev_fp, reservation_id, correlation_id)
            if prev is not None:
                return prev
        via = "RESERVATION"
        if reservation_id is None and correlation_id is not None:
            with db_module.connect(self.db_path) as conn:
                _r = conn.execute("SELECT reservation_id FROM ai_api_reservations WHERE correlation_id = ?",
                                  (correlation_id,)).fetchone()
            if _r is None:
                raise ValueError(f"CORRELATION_IDENTITY_MISMATCH: correlation_id '{correlation_id}' desconhecido")
            reservation_id, via = _r["reservation_id"], "CORRELATION"
        if reservation_id is not None:
            # v1.0.10 (B2 da auditoria 1.0.9): a identidade PERSISTIDA da reserva é a fonte da
            # verdade — divergência = conflito, ANTES de qualquer alteração.
            with db_module.connect(self.db_path) as conn:
                _rv = conn.execute("SELECT provider, service, operation, project, campaign, job_id FROM ai_api_reservations "
                                   "WHERE reservation_id = ?", (reservation_id,)).fetchone()
            if _rv is None:
                raise ValueError(f"{via}_IDENTITY_MISMATCH: reserva '{reservation_id}' inexistente")
            diffs = [f for f, given in (("provider", provider), ("service", service), ("operation", operation))
                     if _rv[f] != given]
            diffs += [f for f, given in (("project", project), ("campaign", campaign), ("job_id", job_id))
                      if given is not None and _rv[f] != given]
            if diffs:
                raise ValueError(f"{via}_IDENTITY_MISMATCH: campos {diffs} divergem da reserva '{reservation_id}' — "
                                 f"nenhuma reserva, uso, cobrança ou incidente foi alterado")
        if reservation_id is None:
            # v1.0.9 (B4 da auditoria 1.0.8): NUNCA adivinhar a reserva. Sem identidade:
            # 0 candidatos = uso sem reserva; 1 = inequívoco; 2+ = AMBIGUOUS_RESERVATION_MATCH.
            with db_module.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT reservation_id FROM ai_api_reservations WHERE status = 'RESERVED' AND provider = ? AND service = ? "
                    "AND operation = ? AND COALESCE(project,'') = COALESCE(?,'') AND COALESCE(campaign,'') = COALESCE(?,'')",
                    (provider, service, operation, project, campaign)).fetchall()
            if len(rows) > 1:
                raise ValueError(f"AMBIGUOUS_RESERVATION_MATCH: {len(rows)} reservas abertas equivalentes para "
                                 f"{provider}/{service}/{operation} — informe reservation_id ou correlation_id; "
                                 f"nenhuma reserva foi alterada")
            reservation_id = rows[0]["reservation_id"] if rows else None
        if reservation_id is not None:
            outcome = "EXECUTED" if (success or cost is not None) else "NOT_EXECUTED"
            try:
                out = self.settle(reservation_id, outcome=outcome, success=success, actual_cost=cost,
                                  actual_currency=currency, input_tokens=input_tokens, output_tokens=output_tokens,
                                  units=units, cost_confirmed=cost_confirmed, usage_event_id=usage_event_id,
                                  usage_event_fingerprint=ev_fp)
            except sqlite3.IntegrityError:
                # corrida: o MESMO evento foi gravado por outra entrega concorrente (índice único)
                prev = self._usage_event_result(usage_event_id, ev_fp, reservation_id, None) if usage_event_id else None
                if prev is None:
                    raise
                return prev
            _uid = None
            if usage_event_id is not None:          # v1.0.11: a 1ª entrega devolve o MESMO uso que o retry
                with db_module.connect(self.db_path) as conn:
                    _u = conn.execute("SELECT usage_id FROM ai_api_usage WHERE usage_event_id = ?", (usage_event_id,)).fetchone()
                _uid = _u["usage_id"] if _u else None
            return {"usage_id": _uid, "cost_status": out.get("cost_status"), "reservation_id": reservation_id,
                    "settled": out.get("status")}
        # v1.0.10 (B3 da auditoria 1.0.9): uso SEM reserva exige identidade idempotente do evento
        if not (isinstance(usage_event_id, str) and usage_event_id.strip()):
            raise ValueError("USAGE_EVENT_ID_REQUIRED: uso sem reserva exige usage_event_id (identidade do evento desde a "
                             "origem) — sem ela um retry duplicaria custo/incidente")
        # ev_fp já calculado no início (mesma fórmula, v1.0.11)
        status = "COST_UNKNOWN" if cost is None else ("CONFIRMED" if cost_confirmed else "ESTIMATED")
        now = time.time()
        with _immediate(self.db_path) as conn:
            _ex = conn.execute("SELECT usage_id, cost_status, usage_event_fingerprint FROM ai_api_usage WHERE usage_event_id = ?",
                               (usage_event_id,)).fetchone()
            if _ex is not None:
                if _ex["usage_event_fingerprint"] != ev_fp:
                    raise ValueError(f"USAGE_EVENT_CONFLICT: usage_event_id '{usage_event_id}' já registrado com payload "
                                     f"diferente — nada foi alterado")
                _inc = conn.execute("SELECT incident_id FROM ai_api_cost_incidents WHERE usage_id = ? ORDER BY created_at "
                                    "LIMIT 1", (_ex["usage_id"],)).fetchone()
                return {"usage_id": _ex["usage_id"], "cost_status": _ex["cost_status"],
                        "incident_id": _inc["incident_id"] if _inc else None, "already_recorded": True}
            # uso registrado fora de reserva: custo conhecido entra no orçamento;
            # desconhecido fica sem cobrança (NULL) e EXIGE reconciliação.
            cur = conn.execute(
                "INSERT INTO ai_api_usage (provider, service, operation, created_at, input_tokens, output_tokens, units, "
                "cost, cost_status, currency, job_id, project, campaign, success, budget_charge, usage_event_id, "
                "usage_event_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (provider, service, operation, now, input_tokens, output_tokens, units, cost, status, currency, job_id,
                 project, campaign, int(bool(success)), cost, usage_event_id, ev_fp))
            usage_id, incident_id = cur.lastrowid, None
            if cost is None:
                # v1.0.8 (B4 da auditoria 1.0.7): uso sem custo e sem reserva ganha, NA MESMA
                # transação, um objeto reconciliável com identidade persistente.
                import uuid as _uuid

                incident_id = f"inc_{_uuid.uuid4().hex[:12]}"
                conn.execute("INSERT INTO ai_api_cost_incidents (incident_id, provider, kind, reported_cost, currency, "
                             "status, created_at, usage_id) VALUES (?,?,'USAGE_COST_UNKNOWN',?,?,'OPEN',?,?)",
                             (incident_id, provider, f"{service}/{operation} success={success}", currency, now, usage_id))
                # v1.0.9 (B3 da auditoria 1.0.8): SNAPSHOT IMUTÁVEL da política vigente no EVENTO
                import json as _json

                _lims = [dict(r) for r in conn.execute("SELECT kind, scope_id, amount, currency, updated_at FROM "
                                                        "ai_api_limits WHERE provider = ? ORDER BY kind, scope_id", (provider,))]
                _ops = sorted({l["currency"] for l in _lims})
                _ub = next((l["amount"] for l in _lims if l["kind"] == "unknown_cost_upper_bound" and l["scope_id"] == "*"),
                           None)
                _snap = {"provider": provider, "service": service, "operation": operation, "currency": _ops,
                         "unknown_cost_upper_bound": _valid_upper_bound(_ub) if _ub is not None else None,
                         "limits": _lims, "at": now, "policy_version": stable_policy_version(_lims)}
                conn.execute("UPDATE ai_api_cost_incidents SET policy_snapshot = ?, snapshot_upper_bound = ?, "
                             "snapshot_currency = ? WHERE incident_id = ?",
                             (_json.dumps(_snap, sort_keys=True), _snap["unknown_cost_upper_bound"],
                              _ops[0] if len(_ops) == 1 else None, incident_id))
        return {"usage_id": usage_id, "cost_status": status, "incident_id": incident_id}

    def summary(self, effective: bool = False) -> dict:
        """Por moeda NATIVA (padrão, compatível) ou EFETIVA (`effective=True`, v1.0.9:
        valores materializados por migração, igual aos cálculos de orçamento)."""
        cur_expr = EFFECTIVE_CURRENCY if effective else "currency"
        amt_expr = EFFECTIVE_AMOUNT if effective else "budget_charge"
        with db_module.connect(self.db_path) as conn:
            rows = conn.execute(f"SELECT provider, {cur_expr} AS currency, COUNT(*) n, SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 "
                                f"END) unknown, COALESCE(SUM(cost),0) known, COALESCE(SUM({amt_expr}),0) charged "
                                f"FROM ai_api_usage GROUP BY provider, {cur_expr}").fetchall()
        return {f"{r['provider']}/{r['currency']}": {"calls": r["n"], "known_cost": round(r["known"], 6),
                                                    "calls_cost_unknown": r["unknown"],
                                                    "budget_charged": round(r["charged"], 6)} for r in rows}



def run_metered_call(*, provider: str, classification: str | None, operation: str, params: dict, perform, blocked):
    """v1.0.4 — caminho ÚNICO de chamada de provedor cobrável:
    classificação → (PAID) reserva atômica → I/O → liquidação pelo desfecho real.
    Runtime operacional/produção ou CostGuard configurado => governança OBRIGATÓRIA.
    Falha da guarda NUNCA libera: sem reserva não há I/O; falha ao liquidar mantém
    a reserva contando e abre incidente (provedor bloqueado até revisão)."""
    import logging

    from core.operations import kill_switch as _ks

    log = logging.getLogger("growth_studio.cost_guard")
    db = configured_db()
    if db is None and not _ks.fail_closed_enforced():
        return perform()[0]                      # modo biblioteca/desenvolvimento explícito
    if classification not in ("PAID", "FREE"):
        return blocked(f"PAYMENT_CLASSIFICATION_REQUIRED: provedor '{provider}' sem classificação de custo explícita "
                       f"(paid=True/False) — desconhecido nunca é presumido gratuito")
    if db is None:
        return blocked("COST_GUARD_NOT_CONFIGURED: runtime operacional/produção sem CostGuard — bloqueio preventivo")
    guard = CostGuard(db)
    if classification == "FREE":
        response = perform()[0]
        try:
            guard.record_free_call(provider=provider, operation=operation, success=getattr(response, "available", False))
        except Exception:
            log.exception("auditoria de chamada gratuita falhou (%s)", provider)
        return response
    # v1.0.11 (P1 da auditoria cirúrgica 1.0.10): a identidade da chamada lógica atravessa
    # origem -> reserva -> I/O -> liquidação. Validada ANTES de qualquer I/O externo.
    correlation_id = params.get("correlation_id")
    req_fp = None
    if correlation_id is not None:
        # v1.0.12: fingerprint do PAYLOAD OPERACIONAL completo, calculado ANTES de qualquer I/O
        try:
            req_fp = request_fingerprint(provider=provider, service=operation, operation=operation, params=params)
        except ValueError as exc:
            return blocked(f"{exc} — chamada não executada (sem identidade verificável)")
    try:
        res = guard.reserve(provider=provider, service=operation, operation=operation,
                            estimated_cost=params.get("estimated_cost"), currency=params.get("currency", "USD"),
                            project=params.get("project"), campaign=params.get("campaign"), job_id=params.get("job_id"),
                            correlation_id=correlation_id, request_fingerprint=req_fp)
    except ValueError as exc:
        if "CORRELATION_ID_CONFLICT" in str(exc):
            return blocked(str(exc))                      # payload divergente: conflito, SEM I/O
        return blocked(f"COST_GUARD_UNAVAILABLE: reserva impossível ({type(exc).__name__}) — nenhuma chamada paga sem reserva")
    except Exception as exc:
        return blocked(f"COST_GUARD_UNAVAILABLE: reserva impossível ({type(exc).__name__}) — nenhuma chamada paga sem reserva")
    if res.get("code") in ("ALREADY_RESERVED", "ALREADY_COMPLETED"):
        # Execução NO MÁXIMO UMA VEZ por identidade: reserva aberta pode ter uma execução em
        # curso (ou órfã, reconciliável pelo Owner); concluída nunca é reexecutada.
        return blocked(f"{res['code']}: chamada lógica '{correlation_id}' já registrada (reserva {res['reservation_id']}) "
                       f"— perform() NÃO reexecutado")
    if not res["authorized"]:
        return blocked(f"{res['code']}: {res['reason']}")
    response, outcome, cost, currency = perform()
    try:
        guard.settle(res["reservation_id"], outcome=outcome, success=getattr(response, "available", False),
                     actual_cost=cost, actual_currency=currency, detail=getattr(response, "reason", ""),
                     cost_confirmed=cost is not None)
    except Exception as exc:
        log.exception("liquidação falhou (%s) — reserva segue contando", provider)
        try:
            guard.open_incident(provider=provider, kind="SETTLEMENT_FAILED", reservation_id=res["reservation_id"],
                                detail=f"{type(exc).__name__}: {exc}")
        except Exception:
            log.exception("incidente de liquidação não pôde ser gravado (%s)", provider)
    return response
