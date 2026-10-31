"""
atlas.radar.sources
======================
ATLAS+RADAR — item 1: fontes de descoberta do radar.

`OpportunitySource` é a interface que QUALQUER fonte de descoberta deve
implementar — canais já cobertos, e canais que "surgirem no futuro"
(basta registrar um novo `OpportunitySource` em `build_default_sources`,
sem alterar `atlas.radar.engine`). Nenhuma fonte desta entrega faz
chamada de API externa real (instrução explícita deste lote) — todas
operam em modo MOCK, e TODO sinal produzido em modo MOCK é classificado
`EvidenceLevel.UNKNOWN`, nunca REAL/ESTIMATED/INFERRED — mesmo que o
valor "pareça" plausível. A classificação de evidência é decidida pelo
MODO do adapter (`AdapterMode.REAL` vs `MOCK`), nunca por confiar no
conteúdo retornado.

A fonte de afiliados REUTILIZA `core.adapters.affiliate.adapters`
(já existente, já testado) — o radar nunca duplica essa lógica; apenas
envolve o catálogo retornado como `CommercialOpportunity`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from atlas.radar.models import (
    ChannelType,
    CommercialOpportunity,
    EvidenceLevel,
    OfferType,
    OpportunitySignal,
)
from core.adapters.affiliate.adapters import REGISTRY as AFFILIATE_REGISTRY
from core.adapters.base import AdapterMode


def _evidence_for_mode(mode: AdapterMode) -> EvidenceLevel:
    """Única regra de classificação: REAL só quando o adapter subjacente
    está genuinamente em modo REAL (credencial configurada). MOCK vira
    UNKNOWN sempre — nunca um número inventado é apresentado como
    evidência real/estimada/inferida."""
    return EvidenceLevel.REAL if mode == AdapterMode.REAL else EvidenceLevel.UNKNOWN


class OpportunitySource(ABC):
    """Interface de uma fonte de descoberta. Implementações reais
    (quando existirem, fora deste lote) preenchem `is_configured()` e
    fazem a chamada de API real dentro de `discover()` — mas mesmo
    assim, `discover()` NUNCA deve classificar um sinal como REAL/
    ESTIMATED/INFERRED sem uma fonte real por trás."""

    channel_type: ChannelType
    source_name: str
    default_offer_type: OfferType = OfferType.OTHER

    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    def discover(self, *, niche: str | None = None) -> list[CommercialOpportunity]:
        ...


@dataclass
class GenericMockOpportunitySource(OpportunitySource):
    """Fonte MOCK genérica, parametrizável por canal — usada para todo
    canal ainda sem integração real dedicada. NUNCA fabrica número de
    audiência/receita/conversão: todo sinal vem `UNKNOWN`, com uma nota
    explícita de que é uma fonte simulada."""

    channel_type: ChannelType
    source_name: str
    default_offer_type: OfferType = OfferType.OTHER

    def is_configured(self) -> bool:
        return False  # nenhuma integração externa real neste lote

    def discover(self, *, niche: str | None = None) -> list[CommercialOpportunity]:
        niche_label = niche or "geral"
        evidence = _evidence_for_mode(AdapterMode.MOCK)
        signal = OpportunitySignal(value="UNKNOWN", evidence=evidence, source=self.source_name)
        opportunity = CommercialOpportunity(
            opportunity_id=CommercialOpportunity.new_id(),
            channel_type=self.channel_type,
            channel_name=self.source_name,
            offer_type=self.default_offer_type,
            title=f"Oportunidade em {self.source_name} ({niche_label})",
            signals={key: signal for key in (
                "audience", "potential_reach", "purchase_intent", "competition", "cost", "fees",
                "commission", "margin_potential", "cac", "roi",
            )},
            notes=[
                f"Fonte MOCK ('{self.source_name}') — nenhuma chamada real de API foi feita; "
                "nenhuma métrica foi inventada. Configure uma integração real para elevar a evidência.",
            ],
        )
        return [opportunity]


class AffiliateMarketplaceOpportunitySource(OpportunitySource):
    """Envolve `core.adapters.affiliate.adapters` (Hotmart, Monetizze,
    Eduzz, Kiwify, Braip, ...) — reaproveita a lógica de catálogo já
    existente e testada, sem duplicá-la. O radar NÃO se limita a esta
    fonte (Seção 1: "não limitar a descoberta a Hotmart, Monetizze ou
    marketplaces de afiliados") — ela é UMA entre várias fontes
    registradas."""

    channel_type = ChannelType.AFFILIATE_PLATFORM
    default_offer_type = OfferType.AFFILIATE_PRODUCT

    def __init__(self, platform_key: str) -> None:
        self.platform_key = platform_key
        self.source_name = platform_key
        self._adapter = AFFILIATE_REGISTRY[platform_key]()

    def is_configured(self) -> bool:
        return self._adapter.is_configured()

    def discover(self, *, niche: str | None = None) -> list[CommercialOpportunity]:
        result = self._adapter.fetch_catalog(niche)
        evidence = _evidence_for_mode(result.mode)
        opportunities = []
        for product in result.data.get("products", []):
            def make_signal(value):
                return OpportunitySignal(
                    value=value if evidence != EvidenceLevel.UNKNOWN else "UNKNOWN",
                    evidence=evidence, source=self.source_name,
                )
            opportunities.append(CommercialOpportunity(
                opportunity_id=CommercialOpportunity.new_id(),
                channel_type=self.channel_type,
                channel_name=self.source_name,
                offer_type=self.default_offer_type,
                title=product.get("name", "produto de afiliado"),
                signals={
                    "commission": make_signal(product.get("commission_pct")),
                    "cost": make_signal(product.get("price")),
                    "competition": OpportunitySignal(value="UNKNOWN", evidence=EvidenceLevel.UNKNOWN, source=self.source_name),
                },
                notes=[result.data.get("note", "")] if evidence == EvidenceLevel.UNKNOWN else [],
            ))
        return opportunities


def build_default_sources() -> list[OpportunitySource]:
    """Registro padrão de fontes — cobre a lista de canais pedida no
    escopo desta correção. Adicionar um canal novo no futuro é só
    ACRESCENTAR um item aqui (ou registrar um `OpportunitySource`
    dedicado quando uma integração real existir); `atlas.radar.engine`
    nunca precisa mudar."""
    generic_channels = [
        (ChannelType.YOUTUBE, "youtube", OfferType.INFOPRODUCT),
        (ChannelType.INSTAGRAM, "instagram", OfferType.DIGITAL_OFFER),
        (ChannelType.FACEBOOK, "facebook", OfferType.DIGITAL_OFFER),
        (ChannelType.TIKTOK, "tiktok", OfferType.DIGITAL_OFFER),
        (ChannelType.OTHER_SOCIAL_NETWORK, "other_social_network", OfferType.DIGITAL_OFFER),
        (ChannelType.MARKETPLACE, "marketplace_generico", OfferType.PHYSICAL_PRODUCT),
        (ChannelType.ECOMMERCE, "ecommerce_proprio", OfferType.PHYSICAL_PRODUCT),
        (ChannelType.OWN_STORE, "loja_propria", OfferType.OWN_PRODUCT),
        (ChannelType.SITE, "site_proprio", OfferType.OWN_PRODUCT),
        (ChannelType.BLOG, "blog", OfferType.INFOPRODUCT),
        (ChannelType.PORTAL, "portal", OfferType.DIGITAL_OFFER),
        (ChannelType.LANDING_PAGE, "landing_page", OfferType.DIGITAL_OFFER),
        (ChannelType.SALES_PAGE, "pagina_de_venda", OfferType.DIGITAL_OFFER),
        (ChannelType.COMPARATOR, "comparador", OfferType.PHYSICAL_PRODUCT),
        (ChannelType.SEARCH_ENGINE, "busca_organica", OfferType.DIGITAL_OFFER),
        (ChannelType.COMMUNITY, "comunidade", OfferType.SERVICE),
        (ChannelType.FORUM, "forum", OfferType.SERVICE),
        (ChannelType.CREATOR_PLATFORM, "plataforma_de_creators", OfferType.SUBSCRIPTION),
        (ChannelType.SERVICE_PLATFORM, "plataforma_de_servicos", OfferType.SERVICE),
        (ChannelType.B2B_PLATFORM, "plataforma_b2b", OfferType.SERVICE),
        (ChannelType.EMAIL, "email", OfferType.DIGITAL_OFFER),
        (ChannelType.NEWSLETTER, "newsletter", OfferType.SUBSCRIPTION),
        (ChannelType.CONTENT_CHANNEL, "canal_de_conteudo", OfferType.INFOPRODUCT),
        (ChannelType.COMMERCIAL_PARTNER, "parceiro_comercial", OfferType.OTHER),
        (ChannelType.DISTRIBUTOR, "distribuidor_digital", OfferType.PHYSICAL_PRODUCT),
    ]
    sources: list[OpportunitySource] = [
        GenericMockOpportunitySource(channel_type=ct, source_name=name, default_offer_type=ot)
        for ct, name, ot in generic_channels
    ]
    sources.extend(
        AffiliateMarketplaceOpportunitySource(platform_key)
        for platform_key in sorted(AFFILIATE_REGISTRY.keys())
    )
    return sources
