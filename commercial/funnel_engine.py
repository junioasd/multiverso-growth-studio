"""
commercial.funnel_engine
==========================
Funnel Engine (Seções 42-44). Suporta múltiplos tipos de funil,
escolhidos conforme plataforma/produto/ticket/público/maturidade/
confiança/nicho. Nada é hardcoded como regra fixa — a escolha é uma
recomendação testável (Seção 44: testar e aprender).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.models.entities import FunnelType

# Mapeamento de referência plataforma -> sequência típica (Seção 43).
# Serve como ponto de partida, não como regra fixa e imutável.
PLATFORM_FUNNEL_TEMPLATES: dict[str, list[str]] = {
    "tiktok": ["descoberta", "short", "cta", "pagina_oferta"],
    "youtube": ["short", "video_longo", "confianca_autoridade", "cta", "oferta"],
    "instagram": ["reel", "stories", "dm_ou_pagina"],
    "facebook": ["conteudo_comunidade", "relacionamento", "oferta"],
}


@dataclass
class FunnelSelectionInput:
    platform: str
    ticket_price: float | None = None
    audience_trust_level: str = "UNKNOWN"  # LOW|MEDIUM|HIGH|UNKNOWN
    audience_maturity: str = "UNKNOWN"     # COLD|WARM|HOT|UNKNOWN
    niche: str = "UNKNOWN"


@dataclass
class FunnelRecommendation:
    funnel_type: FunnelType
    template_sequence: list[str]
    reasoning: list[str] = field(default_factory=list)
    confidence: str = "LOW"  # heurística inicial, sobe com aprendizado real (Seção 52)


class FunnelEngine:
    def recommend(self, request: FunnelSelectionInput) -> FunnelRecommendation:
        reasoning: list[str] = []
        template = PLATFORM_FUNNEL_TEMPLATES.get(request.platform.lower(), [])

        funnel_type = FunnelType.CONTENT_FUNNEL  # padrão conservador

        if request.audience_trust_level == "LOW" or request.audience_maturity == "COLD":
            funnel_type = FunnelType.LEAD_MAGNET
            reasoning.append("audiência fria/baixa confiança: priorizar lead magnet antes da oferta direta")
        elif request.ticket_price is not None and request.ticket_price >= 500:
            funnel_type = FunnelType.WEBINAR
            reasoning.append("ticket alto: webinar/VSL tende a converter melhor que direct-to-offer")
        elif request.audience_trust_level == "HIGH" and request.audience_maturity == "HOT":
            funnel_type = FunnelType.DIRECT_TO_OFFER
            reasoning.append("audiência quente e confiante: direct-to-offer é viável")
        else:
            reasoning.append("dados insuficientes para recomendação de alta confiança; usando content-funnel conservador")

        confidence = "LOW"
        if request.audience_trust_level != "UNKNOWN" and request.audience_maturity != "UNKNOWN":
            confidence = "MEDIUM"

        return FunnelRecommendation(
            funnel_type=funnel_type,
            template_sequence=template,
            reasoning=reasoning,
            confidence=confidence,
        )
