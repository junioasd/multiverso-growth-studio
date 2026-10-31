"""
atlas.opportunity
====================
ATLAS — itens 6-9: coordenar o MultiGerenciador Comercial, avaliar
oportunidades de venda (produtos, infoprodutos, afiliados, serviços,
produtos próprios) e trabalhar distribuição/funis em visão ampla de
canais.

Este módulo NÃO reimplementa nenhuma lógica de scoring/compliance —
delega inteiramente para `commercial.multigerenciador.MultiGerenciadorComercial`
(já existente e já testado), apenas organizando o resultado sob a
ótica de "categoria de oferta" pedida pelo Owner. Nenhuma pontuação,
categoria ou fato é inventado: quando a informação de categoria não
está disponível nos metadados do catálogo, fica UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from commercial.affiliate_marketplace_intelligence import RankedProduct
from commercial.funnel_engine import FunnelRecommendation, FunnelSelectionInput
from commercial.multigerenciador import MultiGerenciadorComercial

# Canais de distribuição reconhecidos pelo item 8 do escopo do ATLAS.
# Meramente descritivo (rótulos) — nenhuma integração externa real
# nesta fase (Seção 62 / instrução explícita deste lote).
RECOGNIZED_DISTRIBUTION_CHANNELS = (
    "youtube", "instagram", "facebook", "tiktok", "site", "blog",
    "pagina_de_venda", "landing_page", "loja", "marketplace", "email",
)

# Categorias de oferta que o Owner pediu para o ATLAS diferenciar. Um
# produto só recebe uma categoria diferente de UNKNOWN quando o próprio
# catálogo/adapter fornecer essa informação em metadata — nunca inferida.
_KNOWN_OFFER_CATEGORIES = ("product", "infoproduct", "affiliate", "service", "own_product")


@dataclass
class ClassifiedOpportunity:
    ranked_product: RankedProduct
    offer_category: str  # um de _KNOWN_OFFER_CATEGORIES, ou "UNKNOWN"


@dataclass
class OpportunityAssessment:
    channel_id: str
    platform: str
    opportunities: list[ClassifiedOpportunity]
    funnel_recommendation: FunnelRecommendation
    top_pick: ClassifiedOpportunity | None


def _classify_offer_category(ranked: RankedProduct) -> str:
    metadata = ranked.catalog_product.metadata or {}
    category = metadata.get("offer_category")
    if category in _KNOWN_OFFER_CATEGORIES:
        return category
    # Produtos vindos de um marketplace de afiliados são, por
    # construção, ofertas de afiliado — isso não é uma inferência sobre
    # o CONTEÚDO do produto, é o fato estrutural de por onde ele veio.
    return "affiliate"


class OpportunityEvaluator:
    """Envolve `MultiGerenciadorComercial` para a visão do ATLAS sobre
    oportunidades comerciais — nunca substitui nem duplica a lógica de
    scoring/compliance já existente."""

    def __init__(self, mgc: MultiGerenciadorComercial) -> None:
        self.mgc = mgc

    def assess(
        self,
        *,
        channel_id: str,
        platform: str,
        affiliate_platform: str,
        niche: str,
        funnel_request: FunnelSelectionInput,
        audience_fit_by_product: dict[str, float] | None = None,
    ) -> OpportunityAssessment:
        base = self.mgc.assess_opportunity(
            channel_id=channel_id,
            platform=platform,
            affiliate_platform=affiliate_platform,
            niche=niche,
            funnel_request=funnel_request,
            audience_fit_by_product=audience_fit_by_product,
        )

        classified = [
            ClassifiedOpportunity(ranked_product=rp, offer_category=_classify_offer_category(rp))
            for rp in base.ranked_products
        ]
        top_pick = next((c for c in classified if c.ranked_product.recommended), None)

        return OpportunityAssessment(
            channel_id=channel_id,
            platform=platform,
            opportunities=classified,
            funnel_recommendation=base.funnel_recommendation,
            top_pick=top_pick,
        )
