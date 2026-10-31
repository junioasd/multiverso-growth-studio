"""
atlas.radar.models
=====================
ATLAS+RADAR — item 1: modelos de dados do Radar Permanente de
Oportunidades Comerciais.

Cada atributo avaliado de uma oportunidade carrega seu PRÓPRIO nível de
evidência (`OpportunitySignal`) — nunca um número solto sem proveniência.
Isso torna estruturalmente impossível "misturar" um dado real com um
inventado: cada valor sabe de onde veio e quão confiável é.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EvidenceLevel(str, Enum):
    """REAL/ESTIMADO/INFERIDO/DESCONHECIDO — a classificação de
    evidência exigida para toda avaliação do radar (nunca REAL por
    padrão; REAL só quando a fonte está genuinamente configurada e
    respondendo em modo real — ver `atlas.radar.sources`)."""
    REAL = "REAL"
    ESTIMATED = "ESTIMATED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class ChannelType(str, Enum):
    """Canais de distribuição/venda reconhecidos pelo radar. `OTHER` é
    o escape-hatch deliberado para "novas plataformas que surgirem no
    futuro" — o radar nunca trava por não conhecer um canal, apenas o
    classifica como OTHER até alguém registrar um adapter dedicado."""
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"
    OTHER_SOCIAL_NETWORK = "other_social_network"
    MARKETPLACE = "marketplace"
    ECOMMERCE = "ecommerce"
    OWN_STORE = "own_store"
    SITE = "site"
    BLOG = "blog"
    PORTAL = "portal"
    LANDING_PAGE = "landing_page"
    SALES_PAGE = "sales_page"
    COMPARATOR = "comparator"
    SEARCH_ENGINE = "search_engine"
    COMMUNITY = "community"
    FORUM = "forum"
    CREATOR_PLATFORM = "creator_platform"
    AFFILIATE_PLATFORM = "affiliate_platform"
    SERVICE_PLATFORM = "service_platform"
    B2B_PLATFORM = "b2b_platform"
    EMAIL = "email"
    NEWSLETTER = "newsletter"
    CONTENT_CHANNEL = "content_channel"
    COMMERCIAL_PARTNER = "commercial_partner"
    DISTRIBUTOR = "distributor"
    OTHER = "other"


class OfferType(str, Enum):
    OWN_PRODUCT = "own_product"
    PHYSICAL_PRODUCT = "physical_product"
    INFOPRODUCT = "infoproduct"
    AFFILIATE_PRODUCT = "affiliate_product"
    SERVICE = "service"
    SUBSCRIPTION = "subscription"
    DIGITAL_OFFER = "digital_offer"
    OTHER = "other"


# Sinais avaliados no mínimo por oportunidade (Seção 1 do escopo desta
# correção). Nomes usados como chaves em `CommercialOpportunity.signals`.
OPPORTUNITY_SIGNAL_KEYS = (
    "channel_type", "product_type", "audience", "potential_reach", "purchase_intent",
    "competition", "cost", "fees", "commission", "margin_potential", "cac", "roi",
    "technical_requirements", "account_requirements", "limitations", "policies",
    "compliance", "risks", "country_region", "availability", "opportunity_maturity",
    "evidence_quality",
)


@dataclass
class OpportunitySignal:
    """Um único atributo avaliado, com evidência e proveniência
    explícitas — nunca um valor solto."""
    value: Any
    evidence: EvidenceLevel = EvidenceLevel.UNKNOWN
    source: str | None = None


@dataclass
class CommercialOpportunity:
    opportunity_id: str
    channel_type: ChannelType
    channel_name: str
    offer_type: OfferType
    title: str
    signals: dict[str, OpportunitySignal] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    discovered_at: float = field(default_factory=time.time)

    def signal(self, key: str) -> OpportunitySignal:
        return self.signals.get(key, OpportunitySignal(value="UNKNOWN", evidence=EvidenceLevel.UNKNOWN))

    @staticmethod
    def new_id() -> str:
        return f"opp_{uuid.uuid4().hex[:12]}"


@dataclass
class RankedOpportunity:
    opportunity: CommercialOpportunity
    score: float | str  # "UNKNOWN" quando a evidência é insuficiente para pontuar com confiança
    confidence: str      # LOW | MEDIUM | HIGH — nunca "CERTAIN" (mesma disciplina do Learning Loop)
    justification: list[str] = field(default_factory=list)
