"""
core.evidence.registry
==========================
CORREÇÃO (item 1 — EvidenceRecord VERIFIED não pode ser
autodeclarado): antes, campos NÃO VAZIOS (mesmo com valores
arbitrários como `source="totally-fake-source"`,
`verifier="self"`) bastavam para conceder `VERIFIED`. Isso está
PROIBIDO. Agora, `VERIFIED` exige uma POLÍTICA REAL:

    - campos mínimos genuinamente preenchidos (não só não-vazios);
    - `verifier` REGISTRADO e AUTORIZADO em `VerifierRegistry` (nunca
      aceito só por não ser vazio — um verifier desconhecido como
      `'self'` é sempre recusado, `VerifierRegistry` é vazio por
      padrão);
    - `source_type` fora de `SourceType.NEVER_SUSTAINS_FACT`
      (`MOCK`/`UNKNOWN` nunca sustentam FACT real).

`EvidenceRegistry` é a fonte de verdade — todo `evidence_id` citado
por uma `FunnelStrategy`/`MetricEvidence` precisa RESOLVER para um
`EvidenceRecord` real aqui. Sem registro correspondente:
`VerificationStatus.MISSING` — nunca tratado como presente.

CORREÇÃO (item 9 — binding de claim): `related_claim_id` adicionado,
junto de `related_strategy_id`/`related_metric_name` já existentes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.multimodal.evidence import Modality


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    INVALID = "INVALID"
    EXPIRED = "EXPIRED"
    MISSING = "MISSING"  # nunca setado num EvidenceRecord real — devolvido pelo resolver quando o ID não existe


class EvidenceScope(str, Enum):
    STRATEGY_SPECIFIC = "STRATEGY_SPECIFIC"  # padrão — nunca reutilizável entre estratégias
    GLOBAL = "GLOBAL"
    CROSS_STRATEGY = "CROSS_STRATEGY"
    BENCHMARK = "BENCHMARK"


# Aliases semanticamente equivalentes — normalizam para o MESMO nome
# canônico. Métricas genuinamente DIFERENTES (ROI != CTR != CAC !=
# ROAS) NUNCA aparecem no mesmo grupo. CORREÇÃO (item 12): CPM,
# MARGIN, PROFIT adicionados.
_METRIC_ALIASES = {
    "roi": "roi", "return_on_investment": "roi",
    "roas": "roas", "return_on_ad_spend": "roas",
    "ctr": "ctr", "click_through_rate": "ctr",
    "conversion_rate": "conversion_rate", "cvr": "conversion_rate",
    "cac": "cac", "customer_acquisition_cost": "cac",
    "cpa": "cpa", "cost_per_acquisition": "cpa",
    "cpc": "cpc", "cost_per_click": "cpc",
    "cpm": "cpm", "cost_per_mille": "cpm", "cost_per_thousand": "cpm",
    "ltv": "ltv", "lifetime_value": "ltv", "clv": "ltv",
    "revenue": "revenue", "faturamento": "revenue",
    "sales": "sales", "sales_volume": "sales_volume",
    "aov": "aov", "average_order_value": "aov",
    "refund_rate": "refund_rate", "chargeback_rate": "refund_rate",
    "margin": "margin", "profit_margin": "margin",
    "profit": "profit", "lucro": "profit",
}


class CanonicalMetricRegistry:
    """CORREÇÃO (item 8 — Canonical Metric Registry): registro
    canônico formal (não apenas uma função solta) — métricas
    semanticamente equivalentes (roi/ROI/return_on_investment)
    resolvem para o MESMO nome; métricas genuinamente diferentes
    (ROI/CTR/CAC/ROAS) NUNCA se confundem."""

    _ALIASES = _METRIC_ALIASES

    @classmethod
    def canonicalize(cls, name: str) -> str:
        key = (name or "").strip().lower()
        return cls._ALIASES.get(key, key)

    @classmethod
    def matches(cls, a: str, b: str) -> bool:
        return cls.canonicalize(a) == cls.canonicalize(b)


def canonical_metric_name(name: str) -> str:
    """Compatibilidade — delega para `CanonicalMetricRegistry`."""
    return CanonicalMetricRegistry.canonicalize(name)


@dataclass
class EvidenceRecord:
    evidence_id: str
    type: str
    source: str
    provenance: str
    observed_at: float
    modality: Modality
    confidence: float = 0.0
    related_strategy_id: str | None = None
    related_claim: str | None = None
    related_claim_id: str | None = None
    related_metric_name: str | None = None
    integrity_reference: str | None = None
    scope: EvidenceScope = EvidenceScope.STRATEGY_SPECIFIC
    verifier: str | None = None
    verification_method: str | None = None
    # CORREÇÃO (item 3): natureza da fonte — MOCK/UNKNOWN nunca
    # sustentam FACT, não importa o texto de `source`. Aceita o enum
    # `SourceType` (core.evidence.source_registry) ou sua string
    # equivalente — sem import direto aqui para evitar ciclo.
    source_type: object = None
    # CORREÇÃO (item 1): o que foi PEDIDO nunca é o que é CONCEDIDO
    # automaticamente — `verification_status` (efetivo) só é setado
    # via `EvidenceRegistry`, nunca lido diretamente do construtor.
    requested_verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verification_status: VerificationStatus = field(init=False, default=VerificationStatus.UNVERIFIED)
    created_at: float = field(default_factory=time.time)
    valid_until: float | None = None

    @staticmethod
    def new_id() -> str:
        return f"ev_{uuid.uuid4().hex[:12]}"

    @property
    def is_expired(self) -> bool:
        return self.valid_until is not None and time.time() > self.valid_until

    def has_acceptable_source_type(self) -> bool:
        """Mantido para compatibilidade — checagem RASA do rótulo
        alegado pelo chamador. CORREÇÃO (item 1 desta rodada): esta
        checagem NUNCA decide sozinha se a evidência é `VERIFIED` —
        `EvidenceRegistry._grant_effective_status()` sempre resolve o
        `source_type` EFETIVO contra a `SourceRegistration` real,
        ignorando o que o chamador alegou aqui."""
        value = self.source_type.value if hasattr(self.source_type, "value") else self.source_type
        return value not in (None, "MOCK", "UNKNOWN")

    def has_minimum_structure(self) -> bool:
        """Checagem estrutural MÍNIMA — campos genuinamente
        preenchidos. Nunca substitui a política completa (source
        resolvida + verifier autorizado + claim binding), que só
        `EvidenceRegistry` decide."""
        return bool(
            self.evidence_id and self.source and self.source != "UNKNOWN" and self.provenance
            and self.observed_at and self.observed_at > 0 and self.created_at and self.created_at > 0
            and 0.0 < self.confidence <= 1.0 and self.modality is not None
            and self.verifier and self.verification_method
        )

    def is_structurally_sufficient_for_verified(self, *, verifier_registry=None) -> bool:
        """Compatibilidade — checagem estrutural + verifier, SEM
        resolução de source (comportamento da rodada anterior).
        `EvidenceRegistry.register()` usa a política COMPLETA (com
        `SourceRegistry`), não este método, diretamente."""
        if not self.has_minimum_structure():
            return False
        if not self.has_acceptable_source_type():
            return False
        if verifier_registry is None:
            return False
        return verifier_registry.is_authorized(self.verifier, verification_method=self.verification_method)


class EvidenceRegistry:
    """Fonte de verdade para resolução de evidência. `resolve()` NUNCA
    inventa um registro — devolve `None` quando o `evidence_id` não
    existe (o chamador trata isso como `MISSING`, nunca como
    presente).

    CORREÇÃO (item 2 rodada anterior): mantém um `verifier_registry`
    PRÓPRIO — vazio por padrão (nenhum verifier confiável até registro
    explícito).

    CORREÇÃO (item 1 desta rodada — SourceRegistry controla evidência
    real): mantém também um `source_registry` PRÓPRIO — vazio por
    padrão. `record.source` é usado como `source_id` — o `source_type`
    EFETIVO SEMPRE vem da `SourceRegistration` resolvida, NUNCA da
    alegação do chamador em `record.source_type`. Sem registro
    correspondente: `SOURCE_MISSING` (tratado como `UNVERIFIED`).
    Registrada mas não validada: `SOURCE_UNVERIFIED`. Health/trust
    insuficiente: `SOURCE_REJECTED` (ambos também `UNVERIFIED` — o
    motivo específico fica disponível via `explain_status()`).

    CORREÇÃO (item 9 — binding de claim): quando `claim_registry` é
    fornecido e `record.related_claim_id` está preenchido, o claim
    também precisa resolver e bater com `related_strategy_id`/
    `related_metric_name` da própria evidência."""

    def __init__(self, *, verifier_registry=None, source_registry=None, claim_registry=None) -> None:
        from core.evidence.claim_registry import ClaimRegistry
        from core.evidence.source_registry import SourceRegistry
        from core.evidence.verifier_registry import VerifierRegistry

        self._records: dict = {}
        self.verifier_registry = verifier_registry if verifier_registry is not None else VerifierRegistry()
        self.source_registry = source_registry if source_registry is not None else SourceRegistry()
        self.claim_registry = claim_registry if claim_registry is not None else ClaimRegistry()

    def explain_status(self, evidence_id: str) -> str:
        """Diagnóstico legível — por que uma evidência não está
        `VERIFIED` (ou confirma que está). Nunca usado para decidir
        nada, só para relatórios/depuração."""
        record = self.resolve(evidence_id)
        if record is None:
            return "MISSING: evidence_id não registrado"
        if record.requested_verification_status != VerificationStatus.VERIFIED:
            return f"status solicitado não é VERIFIED ({record.requested_verification_status.value})"
        if not record.has_minimum_structure():
            return "estrutura mínima incompleta"
        source_registration = self.source_registry.resolve(record.source)
        if source_registration is None:
            return f"SOURCE_MISSING: source '{record.source}' não registrada no SourceRegistry"
        if not source_registration.can_sustain_fact():
            if source_registration.verification_status != VerificationStatus.VERIFIED:
                return f"SOURCE_UNVERIFIED: source '{record.source}' não está VERIFIED"
            return f"SOURCE_REJECTED: source '{record.source}' — trust_class/health insuficiente"
        if not self.verifier_registry.is_authorized(record.verifier, verification_method=record.verification_method):
            return f"verifier '{record.verifier}' não autorizado"
        if record.related_claim_id and not self.claim_registry.matches(
            record.related_claim_id, strategy_id=record.related_strategy_id, metric_name=record.related_metric_name,
        ):
            return f"CLAIM_BINDING_ERROR: claim '{record.related_claim_id}' não resolve/bate com strategy/metric"
        return "VERIFIED"

    def _grant_effective_status(self, record: EvidenceRecord) -> VerificationStatus:
        """CORREÇÃO (item 1): ÚNICO caminho que decide o status
        EFETIVO. Se o chamador não pediu `VERIFIED`, o pedido é
        respeitado como está (UNVERIFIED/INVALID/EXPIRED — nunca
        promovido sozinho). Se pediu `VERIFIED`, só é concedido após a
        POLÍTICA REAL COMPLETA passar: estrutura + source RESOLVIDA no
        `SourceRegistry` (nunca a alegação do chamador) + verifier
        autorizado + (quando aplicável) claim binding — caso
        contrário, `UNVERIFIED` (nunca finge)."""
        if record.requested_verification_status != VerificationStatus.VERIFIED:
            return record.requested_verification_status
        if not record.has_minimum_structure():
            return VerificationStatus.UNVERIFIED

        # CORREÇÃO (item 1/6): resolve a source contra o SourceRegistry
        # — o source_type EFETIVO vem da SourceRegistration, nunca da
        # alegação do chamador em record.source_type.
        source_registration = self.source_registry.resolve(record.source)
        if source_registration is None or not source_registration.can_sustain_fact():
            return VerificationStatus.UNVERIFIED

        if not self.verifier_registry.is_authorized(record.verifier, verification_method=record.verification_method):
            return VerificationStatus.UNVERIFIED

        # CORREÇÃO (item 9): binding de claim, quando aplicável.
        if record.related_claim_id and not self.claim_registry.matches(
            record.related_claim_id, strategy_id=record.related_strategy_id, metric_name=record.related_metric_name,
        ):
            return VerificationStatus.INVALID

        return VerificationStatus.VERIFIED

    def register(self, record: EvidenceRecord) -> EvidenceRecord:
        record.verification_status = self._grant_effective_status(record)
        self._records[record.evidence_id] = record
        return record

    def resolve(self, evidence_id: str) -> EvidenceRecord | None:
        return self._records.get(evidence_id)

    def resolve_status(self, evidence_id: str) -> VerificationStatus:
        """Sempre devolve um `VerificationStatus` — `MISSING` quando o
        ID não existe, `EXPIRED` quando `valid_until` já passou (mesmo
        que o registro tenha `VERIFIED` efetivo — expiração sempre
        prevalece), e o `verification_status` EFETIVO do registro caso
        contrário."""
        record = self.resolve(evidence_id)
        if record is None:
            return VerificationStatus.MISSING
        if record.is_expired:
            return VerificationStatus.EXPIRED
        return record.verification_status

    def resolve_status_for(
        self, evidence_id: str, *, strategy_id: str | None = None, metric_name: str | None = None,
    ) -> tuple:
        """CORREÇÃO (itens 7/9/10 — binding forte): resolução COM
        checagem de vínculo. Devolve `(status, binding_error|None)`.
        Evidência de outra estratégia (quando `scope=STRATEGY_SPECIFIC`,
        o padrão) nunca é aceita silenciosamente; evidência vinculada a
        uma métrica diferente da solicitada nunca é aceita
        silenciosamente."""
        base_status = self.resolve_status(evidence_id)
        if base_status in (VerificationStatus.MISSING,):
            return base_status, None
        record = self.resolve(evidence_id)
        if record is None:
            return base_status, None

        if strategy_id is not None and record.related_strategy_id is not None:
            if record.related_strategy_id != strategy_id and record.scope == EvidenceScope.STRATEGY_SPECIFIC:
                return (
                    VerificationStatus.INVALID,
                    f"STRATEGY_BINDING_ERROR: evidência '{evidence_id}' pertence à estratégia "
                    f"'{record.related_strategy_id}', não a '{strategy_id}' (scope={record.scope.value})",
                )

        if metric_name is not None and record.related_metric_name is not None:
            requested_canonical = CanonicalMetricRegistry.canonicalize(metric_name)
            bound_canonical = CanonicalMetricRegistry.canonicalize(record.related_metric_name)
            if requested_canonical != bound_canonical:
                return (
                    VerificationStatus.INVALID,
                    f"METRIC_BINDING_ERROR: métrica solicitada '{requested_canonical}' mas evidência "
                    f"'{evidence_id}' vinculada a '{bound_canonical}'",
                )

        return base_status, None

    def set_verification_status(self, evidence_id: str, status: VerificationStatus) -> EvidenceRecord:
        """CORREÇÃO (item 1): mesmo aqui, pedir `VERIFIED` passa pela
        MESMA política real — nunca um atalho para contornar
        `register()`."""
        record = self.resolve(evidence_id)
        if record is None:
            raise KeyError(f"evidence_id '{evidence_id}' não registrado — nada para atualizar")
        record.requested_verification_status = status
        record.verification_status = self._grant_effective_status(record)
        return record

    def all(self) -> list:
        return list(self._records.values())
