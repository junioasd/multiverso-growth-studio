"""
radar_web.models
====================
RADAR WEB COMERCIAL — modelos (itens 7, 9, 19).

Classifica funis/estratégias observadas no mercado. Arquitetura
extensível — `FunnelMechanism.OTHER` é o escape-hatch explícito para
formatos emergentes não listados ("não limitar a arquitetura a essa
lista", item 7).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from core.evidence.claims import ClaimType
from core.governance.idempotency import stable_fingerprint


class FunnelMechanism(str, Enum):
    VSL = "VSL"
    WEBINAR = "WEBINAR"
    QUIZ = "QUIZ"
    LEAD_MAGNET = "LEAD_MAGNET"
    TRIPWIRE = "TRIPWIRE"
    LOW_TICKET = "LOW_TICKET"
    PERPETUAL = "PERPETUAL"
    LAUNCH = "LAUNCH"
    DIRECT_RESPONSE = "DIRECT_RESPONSE"
    SOCIAL_SELLING = "SOCIAL_SELLING"
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    REMARKETING = "REMARKETING"
    UPSELL = "UPSELL"
    DOWNSELL = "DOWNSELL"
    CROSS_SELL = "CROSS_SELL"
    ORDER_BUMP = "ORDER_BUMP"
    SUBSCRIPTION = "SUBSCRIPTION"
    CHECKOUT_OPTIMIZATION = "CHECKOUT_OPTIMIZATION"
    COMMUNITY = "COMMUNITY"
    CREATOR_COMMERCE = "CREATOR_COMMERCE"
    SOCIAL_COMMERCE = "SOCIAL_COMMERCE"
    OTHER = "OTHER"  # escape-hatch — arquitetura nunca limitada à lista


class FunnelMaturity(str, Enum):
    EMERGING = "EMERGING"
    GROWING = "GROWING"
    HIGH_SCALE = "HIGH_SCALE"
    MATURE = "MATURE"
    SATURATED = "SATURATED"
    DECLINING = "DECLINING"
    OBSOLETE = "OBSOLETE"
    UNKNOWN = "UNKNOWN"


# Maturidades que EXIGEM evidência forte — nunca atribuídas só por
# opinião (item 9: "nunca classificar como HIGH_SCALE apenas por
# opinião").
_HIGH_CONFIDENCE_REQUIRED = frozenset({FunnelMaturity.HIGH_SCALE, FunnelMaturity.MATURE})
_MIN_CONFIDENCE_FOR_STRONG_CLAIM = 0.7


def compute_strategy_fingerprint(
    *, mechanism: FunnelMechanism, niche: str, channel: str, offer_type: str | None = None,
) -> str:
    """CORREÇÃO (item 4 — identidade estável de estratégia). Hash
    determinístico baseado SOMENTE em atributos ESTRUTURAIS estáveis
    (mecanismo, nicho, canal, tipo de oferta) — NUNCA inclui
    timestamp, UUID aleatório ou qualquer atributo volátil. A MESMA
    combinação estrutural, observada em anos diferentes, SEMPRE produz
    o MESMO `strategy_id` — pré-requisito para versionamento
    longitudinal real (`radar_web.evolution`)."""
    normalized_niche = (niche or "").strip().lower()
    normalized_channel = (channel or "").strip().lower()
    normalized_offer = (offer_type or "").strip().lower() or None
    raw = stable_fingerprint(mechanism.value, normalized_niche, normalized_channel, normalized_offer)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"strat_{digest}"


def compute_strategy_variant_id(*, strategy_id: str, variant_label: str) -> str:
    """Para VARIAÇÕES de uma mesma estratégia estrutural (ex: mesmo
    mecanismo/nicho/canal, mas uma variação de copy/criativo) —
    `strategy_variant_id` deriva do `strategy_id` base + um rótulo de
    variação, nunca aleatório."""
    digest = hashlib.sha256(f"{strategy_id}:{variant_label.strip().lower()}".encode("utf-8")).hexdigest()[:16]
    return f"{strategy_id}_variant_{digest}"


@dataclass
class FunnelStrategy:
    strategy_id: str
    name: str
    niche: str
    offer: str
    channel: str
    market: str
    funnel_stage: str
    mechanism: FunnelMechanism
    maturity: FunnelMaturity
    claim_type: ClaimType
    confidence: float
    source: str
    observed_at: float
    scale_signals: list = field(default_factory=list)
    conversion_signals: list = field(default_factory=list)
    repetition_signals: list = field(default_factory=list)
    evidence_ids: list = field(default_factory=list)
    year: int = field(default_factory=lambda: datetime.now(timezone.utc).year)
    version: int = 1
    # EXPANSÃO — item 13 (Knowledge Freshness): todo conhecimento
    # estratégico carrega quando foi observado, validado e revisado
    # pela última vez, e até quando continua válido.
    created_at: float = field(default_factory=time.time)
    validated_at: float | None = None
    last_reviewed_at: float | None = None
    valid_until: float | None = None
    # CORREÇÃO (item 8 — FunnelStrategy deve ter claims explícitas):
    # `primary_claim_id` é a claim principal que sustenta esta
    # estratégia (obrigatória para `claim_type=FACT` — ver Quality
    # Gate). `claim_ids` permite vincular claims adicionais (ex: uma
    # por métrica) quando aplicável.
    primary_claim_id: str | None = None
    claim_ids: list = field(default_factory=list)

    @staticmethod
    def new_id() -> str:
        return f"funnel_{uuid.uuid4().hex[:12]}"

    @property
    def freshness_score(self) -> float:
        """1.0 = observado agora; decai linearmente até 0.0 em 365 dias
        sem revisão — nunca apresenta conhecimento obsoleto como atual
        sem um alerta explícito (`is_stale`)."""
        reference = self.last_reviewed_at or self.observed_at
        age_days = max(0.0, (time.time() - reference) / 86400)
        return max(0.0, 1.0 - min(age_days / 365.0, 1.0))

    @property
    def is_stale(self) -> bool:
        if self.valid_until is not None and time.time() > self.valid_until:
            return True
        return self.freshness_score < 0.3

    def with_maturity_enforced(self) -> "FunnelStrategy":
        """CORREÇÃO estrutural do item 9: nunca permite uma maturidade
        de alta confiança (HIGH_SCALE/MATURE) sem evidência FACT e
        confidence suficiente — rebaixa para UNKNOWN silenciosamente
        nunca; aqui SEMPRE que a classificação não se sustenta, o
        rebaixamento é explícito e documentado no próprio objeto."""
        if self.maturity in _HIGH_CONFIDENCE_REQUIRED:
            if self.claim_type != ClaimType.FACT or self.confidence < _MIN_CONFIDENCE_FOR_STRONG_CLAIM:
                self.maturity = FunnelMaturity.UNKNOWN
        return self
