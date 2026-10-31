"""
atlas.digital_twin
====================
COMANDO 2/6 — GÊMEO DIGITAL ADAPTATIVO DO ATLAS.

Camada viva de modelagem do estado do negócio que o ATLAS consulta antes
de interpretar, decidir ou aprender. Não é uma ferramenta isolada: é
alimentada pelas MESMAS tabelas reais que a `GovernedFacade` persiste
(vendas, finanças, catálogo, leads, observabilidade de integrações,
descoberta, missões) e é consumida pelo `AtlasDirector` e pelo ciclo
adaptativo (`atlas.learning.AdaptiveStrategyCycle`).

Por que um módulo novo (e só um): nenhuma estrutura existente modelava
estado factual VERSIONADO com proveniência, validade e conflito entre
fontes — `core.memory.WorkingMemory` é volátil e genérica (e seu
`record_decision` não tem consumidor em produção); `atlas.performance`
apenas agrega. O resto do comando foi incorporado a estruturas
existentes (`core.learning.loop`, `core.evidence.claims`,
`atlas.learning`, `atlas.director`).

Regras estruturais (não textuais):

1. QUATRO classes epistêmicas, rigidamente separadas
   (`core.evidence.claims.ClaimType`): FACT (fato observado),
   INFERENCE, HYPOTHESIS, FORECAST (previsão/simulação). `ESTIMATE` e
   `UNKNOWN` NÃO são armazenáveis: estimativa entra como INFERENCE (com
   a base declarada) e "desconhecido" é AUSÊNCIA de fato — nunca um
   valor gravado.
2. Um valor só é devolvido como fato (`TwinReading.fact_value`) se veio
   de uma afirmação FACT vigente. Inferência/hipótese/previsão ficam em
   `TwinReading.non_factual`, sempre rotuladas — nunca promovidas.
3. Toda afirmação carrega: origem (`source_id` + `source_ref`),
   observação (`observed_at`), validade (`valid_until`), qualidade,
   confiança, confirmação, versão e histórico (append-only; nunca
   UPDATE/DELETE).
4. Status explícitos de leitura: CURRENT, UNKNOWN, EXPIRED, CONFLICT,
   LOW_CONFIDENCE, UNCONFIRMED, NOT_OBSERVED.
5. `EvidenceGate` bloqueia decisões críticas sem evidência factual
   mínima (qualquer requisito com status != CURRENT bloqueia).
6. `DecisionLedger` persiste decisões, alternativas, evidência,
   incerteza — inclusive decisões de NÃO agir (BLOCKED/ABSTAINED).
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.evidence.claims import ClaimType
from core.models import db as db_module

# ----------------------------------------------------------------------
# Vocabulário
# ----------------------------------------------------------------------
TWIN_CLAIM_TYPES = frozenset({ClaimType.FACT, ClaimType.INFERENCE, ClaimType.HYPOTHESIS, ClaimType.FORECAST})

# Entidades modeladas (as grandezas — receita, custo, margem, conversão,
# tráfego — são ATRIBUTOS dessas entidades).
ENTITY_TYPES = frozenset({
    "channel", "campaign", "funnel", "product", "offer", "platform", "traffic_source",
    "audience", "competition", "integration", "policy", "operations", "strategy",
})

# Janelas de validade padrão por natureza do dado (segundos). Um fato
# sincronizado deixa de ser CURRENT depois disso até ser re-observado.
TTL_CATALOG = 7 * 24 * 3600.0          # cadastro (canal/produto/oferta/funil)
TTL_PERFORMANCE = 24 * 3600.0          # agregados de desempenho/finanças
TTL_INTEGRATION = 3600.0               # estado de integração
TTL_POLICY = 7 * 24 * 3600.0           # restrições de autoridade (código)
TTL_COMPETITION_DEFAULT = 3 * 24 * 3600.0  # sinal de mercado sem validade própria

# Barra mínima para uma leitura sustentar decisão.
MIN_DECISION_CONFIDENCE = 0.6
_FUTURE_TOLERANCE_SECONDS = 5.0
_NUMERIC_REL_TOLERANCE = 1e-9

# Confiança/qualidade atribuídas às fontes operacionais PRÓPRIAS
# (registradas via GovernedFacade, com validação de borda). Nunca 1.0:
# registro próprio ainda pode conter erro humano de lançamento.
_OWN_RECORD_CONFIDENCE = 0.95
_OWN_RECORD_QUALITY = 0.9


class ReadingStatus(str, Enum):
    CURRENT = "CURRENT"                # fato vigente, confirmado, confiável, sem conflito
    UNKNOWN = "UNKNOWN"                # nenhuma afirmação — desconhecido
    NOT_OBSERVED = "NOT_OBSERVED"      # só inferência/hipótese/previsão — nenhum fato
    EXPIRED = "EXPIRED"                # último fato venceu
    CONFLICT = "CONFLICT"              # fontes vigentes discordam
    LOW_CONFIDENCE = "LOW_CONFIDENCE"  # fato vigente com confiança abaixo da barra
    UNCONFIRMED = "UNCONFIRMED"        # fato vigente sem confirmação da fonte


class TwinValidationError(ValueError):
    pass


@dataclass(frozen=True)
class TwinFact:
    fact_id: str
    entity_type: str
    entity_id: str
    attribute: str
    value: object
    claim_type: ClaimType
    source_id: str
    source_ref: str
    basis: str
    observed_at: float
    valid_until: float
    confidence: float
    quality: float
    confirmed: bool
    derived_from: tuple
    version: int
    supersedes: str | None
    recorded_at: float
    recorded_by: str

    @property
    def key(self) -> tuple:
        return (self.entity_type, self.entity_id, self.attribute)

    def is_expired(self, now: float) -> bool:
        return now >= self.valid_until


@dataclass
class TwinReading:
    entity_type: str
    entity_id: str
    attribute: str
    status: ReadingStatus
    fact_value: object = None                 # SÓ preenchido a partir de FACT
    facts: list = field(default_factory=list)          # versões FACT vigentes (1 por fonte)
    stale_facts: list = field(default_factory=list)    # versões FACT vencidas mais recentes
    non_factual: list = field(default_factory=list)    # INFERENCE/HYPOTHESIS/FORECAST vigentes
    notes: list = field(default_factory=list)

    @property
    def usable_for_decision(self) -> bool:
        return self.status == ReadingStatus.CURRENT

    def describe(self) -> dict:
        return {
            "key": f"{self.entity_type}:{self.entity_id}.{self.attribute}",
            "status": self.status.value,
            "fact_value": self.fact_value,
            "facts": [_fact_brief(f) for f in self.facts],
            "stale_facts": [_fact_brief(f) for f in self.stale_facts],
            "non_factual": [_fact_brief(f) for f in self.non_factual],
            "notes": list(self.notes),
        }


def _fact_brief(f: TwinFact) -> dict:
    return {
        "class": f.claim_type.value, "value": f.value, "source": f.source_id, "ref": f.source_ref,
        "observed_at": f.observed_at, "valid_until": f.valid_until, "confidence": f.confidence,
        "quality": f.quality, "confirmed": f.confirmed, "version": f.version, "fact_id": f.fact_id,
        "basis": f.basis,
    }


def _values_equal(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) \
            and not isinstance(b, bool):
        return math.isclose(float(a), float(b), rel_tol=_NUMERIC_REL_TOLERANCE, abs_tol=1e-12)
    return a == b


# ----------------------------------------------------------------------
# Repositório de afirmações
# ----------------------------------------------------------------------
class DigitalTwin:
    """Registro append-only de afirmações + leituras com status. Com
    `db_path`, TUDO é persistido (sobrevive a restart); sem ele, vive em
    memória (mesmo padrão dos demais componentes do ATLAS)."""

    def __init__(self, *, db_path: str | None = None) -> None:
        self.db_path = db_path
        self._facts: dict = {}   # fact_id -> TwinFact
        if db_path:
            self._load()

    # ------------------------------------------------------------ escrita
    def record(
        self, *, entity_type: str, entity_id: str, attribute: str, value, claim_type: ClaimType,
        source_id: str, source_ref: str = "", basis: str = "", observed_at: float,
        valid_until: float, confidence: float, quality: float, confirmed: bool = False,
        derived_from: list | tuple = (), recorded_by: str, now: float | None = None,
    ) -> TwinFact:
        now = time.time() if now is None else now
        claim_type = ClaimType(claim_type)
        self._validate(
            entity_type=entity_type, entity_id=entity_id, attribute=attribute, value=value,
            claim_type=claim_type, source_id=source_id, source_ref=source_ref, basis=basis,
            observed_at=observed_at, valid_until=valid_until, confidence=confidence, quality=quality,
            derived_from=derived_from, recorded_by=recorded_by, now=now,
        )
        previous = self._latest_version(entity_type, entity_id, attribute, source_id, claim_type)
        if previous is not None and _values_equal(previous.value, value) \
                and previous.source_ref == source_ref and not previous.is_expired(now) \
                and previous.observed_at >= observed_at:
            return previous  # re-sincronização sem mudança: não cria versão ruidosa
        fact = TwinFact(
            fact_id=f"tf_{uuid.uuid4().hex[:16]}", entity_type=entity_type, entity_id=entity_id,
            attribute=attribute, value=value, claim_type=claim_type, source_id=source_id,
            source_ref=source_ref, basis=basis, observed_at=float(observed_at),
            valid_until=float(valid_until), confidence=float(confidence), quality=float(quality),
            confirmed=bool(confirmed), derived_from=tuple(derived_from),
            version=(previous.version + 1) if previous else 1,
            supersedes=previous.fact_id if previous else None, recorded_at=now, recorded_by=recorded_by,
        )
        self._persist(fact)
        self._facts[fact.fact_id] = fact
        return fact

    def _validate(self, *, entity_type, entity_id, attribute, value, claim_type, source_id, source_ref,
                  basis, observed_at, valid_until, confidence, quality, derived_from, recorded_by, now):
        if claim_type not in TWIN_CLAIM_TYPES:
            raise TwinValidationError(
                f"classe {claim_type.value} não é armazenável no Gêmeo: estimativa entra como INFERENCE "
                "com base declarada; desconhecido é ausência de fato"
            )
        if entity_type not in ENTITY_TYPES:
            raise TwinValidationError(f"entity_type desconhecido: {entity_type!r}")
        if not entity_id or not attribute:
            raise TwinValidationError("entity_id e attribute são obrigatórios")
        if not source_id:
            raise TwinValidationError("proveniência obrigatória: source_id vazio")
        if not recorded_by:
            raise TwinValidationError("recorded_by obrigatório (quem registrou)")
        if value is None:
            raise TwinValidationError("valor None não é afirmação — desconhecido é AUSÊNCIA de registro")
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise TwinValidationError(f"valor não serializável: {exc}") from exc
        if not (isinstance(observed_at, (int, float)) and observed_at > 0):
            raise TwinValidationError("observed_at obrigatório")
        if observed_at > now + _FUTURE_TOLERANCE_SECONDS and claim_type == ClaimType.FACT:
            raise TwinValidationError("FATO com observed_at no futuro — observação impossível")
        if not (isinstance(valid_until, (int, float)) and valid_until > observed_at):
            raise TwinValidationError("valid_until obrigatório e posterior a observed_at (todo dado expira)")
        if not (0.0 < float(confidence) <= 1.0):
            raise TwinValidationError("confidence fora de (0, 1]")
        if not (0.0 <= float(quality) <= 1.0):
            raise TwinValidationError("quality fora de [0, 1]")
        if claim_type == ClaimType.FACT and not source_ref:
            raise TwinValidationError("FATO exige source_ref (referência verificável à observação)")
        if claim_type in (ClaimType.INFERENCE, ClaimType.FORECAST) and not basis:
            raise TwinValidationError(f"{claim_type.value} exige basis (método/fundamento declarado)")
        if claim_type == ClaimType.HYPOTHESIS and not basis:
            raise TwinValidationError("HYPOTHESIS exige basis (o que se supõe e por quê)")
        for ref in derived_from:
            if ref not in self._facts:
                raise TwinValidationError(f"derived_from referencia afirmação inexistente: {ref}")

    def _latest_version(self, entity_type, entity_id, attribute, source_id, claim_type) -> TwinFact | None:
        best = None
        for f in self._facts.values():
            if (f.entity_type, f.entity_id, f.attribute, f.source_id, f.claim_type) == \
                    (entity_type, entity_id, attribute, source_id, claim_type):
                if best is None or f.version > best.version:
                    best = f
        return best

    # ------------------------------------------------------------ leitura
    def get(self, fact_id: str) -> TwinFact | None:
        return self._facts.get(fact_id)

    def history(self, entity_type: str, entity_id: str, attribute: str) -> list:
        return sorted(
            (f for f in self._facts.values() if f.key == (entity_type, entity_id, attribute)),
            key=lambda f: (f.recorded_at, f.version),
        )

    def keys(self, *, entity_type: str | None = None, entity_id: str | None = None) -> list:
        return sorted({
            f.key for f in self._facts.values()
            if (entity_type is None or f.entity_type == entity_type)
            and (entity_id is None or f.entity_id == entity_id)
        })

    def reading(self, entity_type: str, entity_id: str, attribute: str, *, now: float | None = None) -> TwinReading:
        now = time.time() if now is None else now
        versions = [f for f in self._facts.values() if f.key == (entity_type, entity_id, attribute)]
        reading = TwinReading(entity_type=entity_type, entity_id=entity_id, attribute=attribute,
                              status=ReadingStatus.UNKNOWN)
        if not versions:
            reading.notes.append("nenhuma afirmação registrada — estado DESCONHECIDO (nada inventado)")
            return reading
        latest: dict = {}
        for f in versions:
            k = (f.source_id, f.claim_type)
            if k not in latest or f.version > latest[k].version:
                latest[k] = f
        facts = [f for (_, ct), f in latest.items() if ct == ClaimType.FACT]
        current = sorted((f for f in facts if not f.is_expired(now)), key=lambda f: f.source_id)
        stale = sorted((f for f in facts if f.is_expired(now)), key=lambda f: f.source_id)
        reading.facts, reading.stale_facts = current, stale
        reading.non_factual = sorted(
            (f for (_, ct), f in latest.items() if ct != ClaimType.FACT and not f.is_expired(now)),
            key=lambda f: (f.claim_type.value, f.source_id),
        )
        if not current:
            if stale:
                reading.status = ReadingStatus.EXPIRED
                reading.notes.append("último fato VENCIDO — valor antigo não é estado atual")
            else:
                reading.status = ReadingStatus.NOT_OBSERVED
                reading.notes.append("só existem inferência/hipótese/previsão — nenhum fato observado")
            return reading
        distinct = []
        for f in current:
            if not any(_values_equal(f.value, d.value) for d in distinct):
                distinct.append(f)
        if len(distinct) > 1:
            reading.status = ReadingStatus.CONFLICT
            reading.notes.append(
                "fontes vigentes DISCORDAM: " + "; ".join(f"{f.source_id}={f.value!r}" for f in current)
            )
            return reading
        reading.fact_value = current[0].value
        if max(f.confidence for f in current) < MIN_DECISION_CONFIDENCE:
            reading.status = ReadingStatus.LOW_CONFIDENCE
            reading.notes.append(f"confiança abaixo de {MIN_DECISION_CONFIDENCE}")
        elif not any(f.confirmed for f in current):
            reading.status = ReadingStatus.UNCONFIRMED
            reading.notes.append("fato vigente sem confirmação da fonte")
        else:
            reading.status = ReadingStatus.CURRENT
        return reading

    def snapshot(self, *, entity_type: str | None = None, entity_id: str | None = None,
                 now: float | None = None) -> list:
        return [self.reading(*k, now=now) for k in self.keys(entity_type=entity_type, entity_id=entity_id)]

    # ------------------------------------------------------- persistência
    def _persist(self, f: TwinFact) -> None:
        if not self.db_path:
            return
        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO atlas_twin_facts (fact_id, entity_type, entity_id, attribute, value_json, "
                "claim_type, source_id, source_ref, basis, observed_at, valid_until, confidence, quality, "
                "confirmed, derived_from_json, version, supersedes, recorded_at, recorded_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f.fact_id, f.entity_type, f.entity_id, f.attribute, json.dumps(f.value), f.claim_type.value,
                 f.source_id, f.source_ref, f.basis, f.observed_at, f.valid_until, f.confidence, f.quality,
                 int(f.confirmed), json.dumps(list(f.derived_from)), f.version, f.supersedes,
                 f.recorded_at, f.recorded_by),
            )

    def _load(self) -> None:
        with db_module.connect(self.db_path) as conn:
            for r in conn.execute("SELECT * FROM atlas_twin_facts").fetchall():
                self._facts[r["fact_id"]] = TwinFact(
                    fact_id=r["fact_id"], entity_type=r["entity_type"], entity_id=r["entity_id"],
                    attribute=r["attribute"], value=json.loads(r["value_json"]),
                    claim_type=ClaimType(r["claim_type"]), source_id=r["source_id"], source_ref=r["source_ref"],
                    basis=r["basis"], observed_at=r["observed_at"], valid_until=r["valid_until"],
                    confidence=r["confidence"], quality=r["quality"], confirmed=bool(r["confirmed"]),
                    derived_from=tuple(json.loads(r["derived_from_json"])), version=r["version"],
                    supersedes=r["supersedes"], recorded_at=r["recorded_at"], recorded_by=r["recorded_by"],
                )

    # ------------------------------------------- sincronização (dados reais)
    def sync_from_operational_store(self, *, recorded_by: str = "ATLAS", now: float | None = None) -> dict:
        """Projeta no Gêmeo o que JÁ está persistido nas tabelas reais.
        Só lê (SELECT). Nada é inventado: sem linhas, sem afirmação — o
        estado correspondente fica DESCONHECIDO."""
        if not self.db_path:
            raise RuntimeError("sincronização exige db_path (fonte de verdade persistida)")
        now = time.time() if now is None else now
        counts: dict = {}
        with db_module.connect(self.db_path) as conn:
            rows = {
                "sales": conn.execute("SELECT * FROM sales_events").fetchall(),
                "finance": conn.execute("SELECT * FROM finance_entries").fetchall(),
                "channels": conn.execute("SELECT * FROM channels").fetchall(),
                "products": conn.execute("SELECT * FROM products").fetchall(),
                "offers": conn.execute("SELECT * FROM offers").fetchall(),
                "funnels": conn.execute("SELECT * FROM funnels").fetchall(),
                "leads": conn.execute("SELECT channel_id, COUNT(*) AS n, MAX(created_at) AS last "
                                      "FROM leads GROUP BY channel_id").fetchall(),
                "observability": conn.execute("SELECT * FROM execution_observability "
                                              "ORDER BY executed_at").fetchall(),
                "discovery": conn.execute("SELECT * FROM discovery_opportunities").fetchall(),
                "missions": conn.execute("SELECT status, COUNT(*) AS n, MAX(updated_at) AS last "
                                         "FROM missions GROUP BY status").fetchall(),
            }
        rec = self._recorder(recorded_by, now, counts)
        self._sync_sales(rows["sales"], rec, now)
        self._sync_finance(rows["finance"], rec, now)
        self._sync_catalog(rows, rec, now)
        self._sync_integrations(rows["observability"], rec, now)
        self._sync_competition(rows["discovery"], rec, now)
        self._sync_operations(rows["missions"], rec, now)
        self._sync_policy(rec, now)
        return counts

    def _recorder(self, recorded_by, now, counts):
        def rec(**kw):
            fact = self.record(recorded_by=recorded_by, now=now, **kw)
            counts[kw["entity_type"]] = counts.get(kw["entity_type"], 0) + 1
            return fact
        return rec

    _SALES_DIMENSIONS = (
        ("channel", "channel_id"), ("campaign", "campaign_id"), ("funnel", "funnel_id"),
        ("product", "product_id"), ("platform", "platform"), ("traffic_source", "traffic_source"),
    )

    def _sync_sales(self, rows, rec, now):
        claim_by_value_type = {"REAL": ClaimType.FACT, "ESTIMATED": ClaimType.INFERENCE,
                               "FORECAST": ClaimType.FORECAST}
        for entity_type, column in self._SALES_DIMENSIONS:
            groups: dict = {}
            for r in rows:
                dim = r[column]
                claim = claim_by_value_type.get(r["value_type"])
                if not dim or claim is None:
                    continue  # UNKNOWN nunca vira afirmação
                groups.setdefault((dim, claim, r["value_type"]), []).append(r)
            for (dim, claim, value_type), items in groups.items():
                counts = {t: 0 for t in ("impression", "view", "click", "lead", "checkout", "sale", "refund")}
                sale_amount = refund_amount = 0.0
                for r in items:
                    counts[r["event_type"]] = counts.get(r["event_type"], 0) + 1
                    if r["event_type"] == "sale" and r["amount"] is not None:
                        sale_amount += r["amount"]
                    if r["event_type"] == "refund" and r["amount"] is not None:
                        refund_amount += r["amount"]
                last = max(r["created_at"] for r in items)
                ref = f"sales_events[{column}={dim};value_type={value_type};n={len(items)};last={last:.3f}]"
                common = dict(
                    entity_type=entity_type, entity_id=dim, claim_type=claim, source_id="sales_tracking",
                    source_ref=ref, observed_at=last, valid_until=now + TTL_PERFORMANCE,
                    confidence=_OWN_RECORD_CONFIDENCE if claim == ClaimType.FACT else 0.5,
                    quality=_OWN_RECORD_QUALITY, confirmed=(claim == ClaimType.FACT),
                    basis="" if claim == ClaimType.FACT else f"eventos {value_type} lançados no rastreamento",
                )
                for event_type, n in counts.items():
                    if n:
                        rec(attribute=f"events.{event_type}", value=n, **common)
                if counts["sale"]:
                    rec(attribute="revenue", value=round(sale_amount, 2), **common)
                if counts["refund"]:
                    rec(attribute="refunds_amount", value=round(refund_amount, 2), **common)
                if claim == ClaimType.FACT and counts["click"] > 0:
                    rec(attribute="conversion_rate", value=round(counts["sale"] / counts["click"], 6),
                        **{**common, "source_ref": ref + ";ratio=sale/click"})

    def _sync_finance(self, rows, rec, now):
        by_channel: dict = {}
        for r in rows:
            by_channel.setdefault(r["channel_id"] or "", []).append(r)
        for channel_id, items in by_channel.items():
            if not channel_id:
                continue
            totals: dict = {}
            for r in items:
                totals[r["entry_type"]] = totals.get(r["entry_type"], 0.0) + r["amount"]
            last = max(r["created_at"] for r in items)
            ref = f"finance_entries[channel_id={channel_id};n={len(items)};last={last:.3f}]"
            common = dict(
                entity_type="channel", entity_id=channel_id, claim_type=ClaimType.FACT,
                source_id="finance_ledger", source_ref=ref, observed_at=last,
                valid_until=now + TTL_PERFORMANCE, confidence=_OWN_RECORD_CONFIDENCE,
                quality=_OWN_RECORD_QUALITY, confirmed=True,
            )
            if "revenue" in totals:
                rec(attribute="revenue", value=round(totals["revenue"], 2), **common)
            realized_costs = sum(totals.get(k, 0.0) for k in ("expense", "realized_cost", "commission"))
            if any(k in totals for k in ("expense", "realized_cost", "commission")):
                rec(attribute="costs_realized", value=round(realized_costs, 2), **common)
            if "committed_cost" in totals:
                rec(attribute="costs_committed", value=round(totals["committed_cost"], 2), **common)
            if "refund" in totals:
                rec(attribute="refunds", value=round(totals["refund"], 2), **common)
            if "revenue" in totals and any(k in totals for k in ("expense", "realized_cost", "commission")):
                # margem = receita − reembolsos − custos REALIZADOS, todos do
                # MESMO livro (custo comprometido não realizado fica fora).
                # COMANDO 5/6: reembolso passou a ser descontado.
                rec(attribute="margin", value=round(totals["revenue"] - totals.get("refund", 0.0) - realized_costs, 2),
                    **{**common, "source_ref": ref + ";margin=revenue-refund-(expense+realized_cost+commission)"})

    def _sync_catalog(self, rows, rec, now):
        def common(entity_type, entity_id, table, created_at):
            return dict(entity_type=entity_type, entity_id=entity_id, claim_type=ClaimType.FACT,
                        source_id="catalog_registry", source_ref=f"{table}[{entity_id}]", observed_at=created_at,
                        valid_until=now + TTL_CATALOG, confidence=_OWN_RECORD_CONFIDENCE,
                        quality=_OWN_RECORD_QUALITY, confirmed=True)
        for r in rows["channels"]:
            c = common("channel", r["channel_id"], "channels", r["created_at"])
            rec(attribute="registered", value=True, **c)
            for col in ("platform", "niche", "language", "region", "strategy"):
                if r[col]:
                    rec(attribute=col, value=r[col], **c)
            if r["audience"]:
                # público DECLARADO no cadastro — não é audiência medida
                rec(attribute="declared_target_audience", value=r["audience"], **c)
        for r in rows["products"]:
            c = common("product", r["product_id"], "products", r["created_at"])
            rec(attribute="registered", value=True, **c)
            for col in ("platform", "name", "niche", "price", "commission_pct", "refund_rate"):
                if r[col] is not None and r[col] != "":
                    rec(attribute=col, value=r[col], **c)
        for r in rows["offers"]:
            c = common("offer", r["offer_id"], "offers", r["created_at"])
            rec(attribute="status", value=r["status"], **c)
            rec(attribute="product_id", value=r["product_id"], **c)
            rec(attribute="channel_id", value=r["channel_id"], **c)
        for r in rows["funnels"]:
            c = common("funnel", r["funnel_id"], "funnels", r["created_at"])
            rec(attribute="funnel_type", value=r["funnel_type"], **c)
            rec(attribute="channel_id", value=r["channel_id"], **c)
        for r in rows["leads"]:
            if r["channel_id"]:
                rec(attribute="leads_total", value=r["n"],
                    **{**common("audience", r["channel_id"], "leads", r["last"]),
                       "source_ref": f"leads[channel_id={r['channel_id']};n={r['n']}]",
                       "valid_until": now + TTL_PERFORMANCE})

    def _sync_integrations(self, rows, rec, now):
        by_platform: dict = {}
        for r in rows:
            by_platform.setdefault(r["platform"], []).append(r)
        for platform, items in by_platform.items():
            last = items[-1]
            recent = items[-20:]
            ref = f"execution_observability[platform={platform};last={last['entry_id']}]"
            c = dict(entity_type="integration", entity_id=platform, claim_type=ClaimType.FACT,
                     source_id="execution_observability", source_ref=ref, observed_at=last["executed_at"],
                     valid_until=last["executed_at"] + TTL_INTEGRATION, confidence=_OWN_RECORD_CONFIDENCE,
                     quality=_OWN_RECORD_QUALITY, confirmed=True)
            rec(attribute="last_execution_success", value=bool(last["success"]), **c)
            rec(attribute="recent_failure_rate",
                value=round(sum(1 for r in recent if not r["success"]) / len(recent), 4),
                **{**c, "source_ref": ref + f";window={len(recent)}"})
            if not last["success"] and last["error_reason"]:
                rec(attribute="last_error", value=last["error_reason"], **c)

    def _sync_competition(self, rows, rec, now):
        claim_map = {"FACT": ClaimType.FACT, "ESTIMATE": ClaimType.INFERENCE, "INFERENCE": ClaimType.INFERENCE,
                     "HYPOTHESIS": ClaimType.HYPOTHESIS, "FORECAST": ClaimType.FORECAST}
        for r in rows:
            signals = json.loads(r["signals_json"])
            for name, sig in signals.items():
                claim = claim_map.get(sig.get("claim_type"))
                if claim is None or sig.get("value") is None or not sig.get("observed_at"):
                    continue  # UNKNOWN / sem observação: fica desconhecido
                observed = float(sig["observed_at"])
                valid_until = sig.get("valid_until") or (observed + TTL_COMPETITION_DEFAULT)
                if valid_until <= observed:
                    continue
                confidence = float(sig.get("confidence") or 0.0)
                if confidence <= 0.0:
                    continue
                lineage = sig.get("lineage") or {}
                rec(entity_type="competition", entity_id=r["opportunity_id"], attribute=f"signal.{name}",
                    value=sig["value"], claim_type=claim, source_id=f"discovery:{sig.get('source') or 'unknown'}",
                    source_ref=f"discovery_opportunities[{r['identity_key']}];acquisition="
                               f"{lineage.get('acquisition_id') or sig.get('claimed_acquisition_id') or ''}",
                    basis="" if claim == ClaimType.FACT else f"sinal de descoberta classificado {sig.get('claim_type')}",
                    observed_at=observed, valid_until=valid_until, confidence=min(confidence, 1.0),
                    quality=0.7, confirmed=(claim == ClaimType.FACT and sig.get("evidence") == "REAL"
                                            and bool(lineage.get("acquisition_id"))))

    def _sync_operations(self, rows, rec, now):
        for r in rows:
            rec(entity_type="operations", entity_id="missions", attribute=f"count.{r['status']}", value=r["n"],
                claim_type=ClaimType.FACT, source_id="mission_tracker",
                source_ref=f"missions[status={r['status']};n={r['n']}]", observed_at=r["last"] or now,
                valid_until=now + TTL_PERFORMANCE, confidence=_OWN_RECORD_CONFIDENCE,
                quality=_OWN_RECORD_QUALITY, confirmed=True)

    def _sync_policy(self, rec, now):
        from core.security.rbac import _ROLE_PERMISSIONS, Role  # leitura apenas

        perms = sorted(p.value for p in _ROLE_PERMISSIONS.get(Role.ATLAS, ()))
        rec(entity_type="policy", entity_id="atlas_authority", attribute="permissions", value=perms,
            claim_type=ClaimType.FACT, source_id="core.security.rbac", source_ref="ROLE_PERMISSIONS[ATLAS]",
            observed_at=now, valid_until=now + TTL_POLICY, confidence=1.0, quality=1.0, confirmed=True)


# ----------------------------------------------------------------------
# Gate de evidência para decisões
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class EvidenceRequirement:
    entity_type: str
    entity_id: str
    attribute: str
    purpose: str = ""


@dataclass
class EvidenceAssessment:
    allowed: bool
    readings: list
    blocking: list          # [(requirement, reading)] que impedem a decisão

    def reasons(self) -> list:
        return [
            f"{req.entity_type}:{req.entity_id}.{req.attribute} = {rd.status.value}"
            + (f" ({'; '.join(rd.notes)})" if rd.notes else "")
            for req, rd in self.blocking
        ]

    def fact_ids(self) -> list:
        return [f.fact_id for rd in self.readings for f in rd.facts]


class EvidenceGate:
    """Decisão crítica só é permitida quando TODOS os requisitos
    factuais têm leitura CURRENT. Qualquer outro status bloqueia — e o
    motivo explícito acompanha o bloqueio."""

    def __init__(self, twin: DigitalTwin) -> None:
        self.twin = twin

    def assess(self, requirements: list, *, now: float | None = None) -> EvidenceAssessment:
        if not requirements:
            raise ValueError("decisão crítica sem requisito factual declarado não é avaliável")
        readings, blocking = [], []
        for req in requirements:
            rd = self.twin.reading(req.entity_type, req.entity_id, req.attribute, now=now)
            readings.append(rd)
            if not rd.usable_for_decision:
                blocking.append((req, rd))
        return EvidenceAssessment(allowed=not blocking, readings=readings, blocking=blocking)


# ----------------------------------------------------------------------
# Memória de decisões
# ----------------------------------------------------------------------
class DecisionStatus(str, Enum):
    DECIDED = "DECIDED"
    BLOCKED = "BLOCKED"          # gate de evidência impediu
    ABSTAINED = "ABSTAINED"      # ATLAS optou por não agir (evidência inadequada)
    DISAGREED = "DISAGREED"      # proposta rejeitada com fundamento
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    topic: str
    scope: str
    status: DecisionStatus
    chosen: str | None
    alternatives: tuple
    evidence: tuple
    justification: str
    uncertainty: str
    supersedes: str | None
    created_at: float
    actor: str


class DecisionLedger:
    def __init__(self, *, db_path: str | None = None) -> None:
        self.db_path = db_path
        self._records: list = []
        if db_path:
            with db_module.connect(db_path) as conn:
                for r in conn.execute("SELECT * FROM atlas_decisions ORDER BY created_at").fetchall():
                    self._records.append(DecisionRecord(
                        decision_id=r["decision_id"], topic=r["topic"], scope=r["scope"],
                        status=DecisionStatus(r["status"]), chosen=r["chosen"],
                        alternatives=tuple(json.loads(r["alternatives_json"])),
                        evidence=tuple(json.loads(r["evidence_json"])), justification=r["justification"],
                        uncertainty=r["uncertainty"], supersedes=r["supersedes"], created_at=r["created_at"],
                        actor=r["actor"],
                    ))

    def record(self, *, topic: str, scope: str, status: DecisionStatus, justification: str, actor: str,
               chosen: str | None = None, alternatives: list | tuple = (), evidence: list | tuple = (),
               uncertainty: str = "", now: float | None = None) -> DecisionRecord:
        if not justification:
            raise ValueError("decisão sem justificativa não é registrável")
        previous = self.latest(topic=topic, scope=scope)
        rec = DecisionRecord(
            decision_id=f"dec_{uuid.uuid4().hex[:16]}", topic=topic, scope=scope, status=DecisionStatus(status),
            chosen=chosen, alternatives=tuple(alternatives), evidence=tuple(evidence),
            justification=justification, uncertainty=uncertainty,
            supersedes=previous.decision_id if previous else None,
            created_at=time.time() if now is None else now, actor=actor,
        )
        if self.db_path:
            with db_module.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO atlas_decisions (decision_id, topic, scope, status, chosen, alternatives_json, "
                    "evidence_json, justification, uncertainty, supersedes, created_at, actor) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rec.decision_id, rec.topic, rec.scope, rec.status.value, rec.chosen,
                     json.dumps(list(rec.alternatives)), json.dumps(list(rec.evidence)), rec.justification,
                     rec.uncertainty, rec.supersedes, rec.created_at, rec.actor),
                )
        self._records.append(rec)
        return rec

    def latest(self, *, topic: str, scope: str) -> DecisionRecord | None:
        matching = [r for r in self._records if r.topic == topic and r.scope == scope]
        return matching[-1] if matching else None

    def history(self, *, topic: str | None = None, scope: str | None = None) -> list:
        return [r for r in self._records
                if (topic is None or r.topic == topic) and (scope is None or r.scope == scope)]

    def get(self, decision_id: str) -> DecisionRecord | None:
        return next((r for r in self._records if r.decision_id == decision_id), None)


# ----------------------------------------------------------------------
# COMANDO 4/6 — VISÃO EVOLUTIVA sobre o histórico factual do Gêmeo.
# Responde, com evidência: o que mudou, quando, quanto, qual fonte
# comprova e se a mudança é PERSISTENTE, PONTUAL ou NÃO CONFIRMADA.
# Mudança é ASSOCIAÇÃO TEMPORAL — nunca afirmação de causa.
# ----------------------------------------------------------------------
class ChangeKind(str, Enum):
    PERSISTENT = "PERSISTENT"      # novo nível mantido em observações posteriores
    TRANSIENT = "TRANSIENT"        # voltou ao nível anterior: pontual
    UNCONFIRMED = "UNCONFIRMED"    # só uma observação no novo nível: tendência NÃO sustentada


@dataclass(frozen=True)
class ChangeEvent:
    entity_type: str
    entity_id: str
    attribute: str
    source_id: str
    from_value: float
    to_value: float
    relative_change: float
    changed_at: float              # observed_at da primeira observação no novo nível
    kind: ChangeKind
    evidence_fact_ids: tuple       # fato antes, fato depois (+ confirmações)
    note: str = "associação temporal observada — causalidade NÃO demonstrada"


def _rel(a: float, b: float) -> float:
    base = abs(a) if a != 0 else 1e-12
    return (b - a) / base


def detect_changes(twin: "DigitalTwin", entity_type: str, entity_id: str, attribute: str, *,
                   min_relative_change: float = 0.25, confirmations_required: int = 1,
                   since: float | None = None) -> list:
    """Mudanças RELEVANTES (|variação| >= `min_relative_change`) entre
    observações FATUAIS consecutivas da MESMA fonte. Só FACT entra —
    inferência/previsão nunca produz "mudança de mercado"."""
    events = []
    by_source: dict = {}
    for f in twin.history(entity_type, entity_id, attribute):
        if f.claim_type == ClaimType.FACT and isinstance(f.value, (int, float)) and not isinstance(f.value, bool):
            by_source.setdefault(f.source_id, []).append(f)
    for source_id, facts in by_source.items():
        facts.sort(key=lambda f: (f.observed_at, f.version))
        for i in range(1, len(facts)):
            before, after = facts[i - 1], facts[i]
            rel = _rel(float(before.value), float(after.value))
            if abs(rel) < min_relative_change or (since is not None and after.observed_at <= since):
                continue
            later = facts[i + 1:]
            reverted = [f for f in later if abs(_rel(float(before.value), float(f.value))) < min_relative_change]
            held = [f for f in later if f not in reverted and (float(f.value) - float(before.value)) * rel > 0]
            if reverted:
                kind = ChangeKind.TRANSIENT
            elif len(held) >= confirmations_required:
                kind = ChangeKind.PERSISTENT
            else:
                kind = ChangeKind.UNCONFIRMED
            events.append(ChangeEvent(
                entity_type=entity_type, entity_id=entity_id, attribute=attribute, source_id=source_id,
                from_value=float(before.value), to_value=float(after.value), relative_change=round(rel, 6),
                changed_at=after.observed_at, kind=kind,
                evidence_fact_ids=(before.fact_id, after.fact_id) + tuple(f.fact_id for f in held[:3]),
            ))
    return sorted(events, key=lambda e: e.changed_at)
