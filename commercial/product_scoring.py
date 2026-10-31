"""
commercial.product_scoring
============================
Product Scoring Engine (Seções 35-37). NÃO escolhe produto apenas
pela maior comissão. Gera score explicável; UNKNOWN quando faltar
dado; nunca inventa pontuação. Alta comissão não significa
automaticamente bom produto — pode ser rejeitado por qualidade ruim,
refund elevado, página fraca, reputação baixa, fit ruim, ou risco de
marca.
"""

from __future__ import annotations

from dataclasses import dataclass, field

UNKNOWN = "UNKNOWN"

# pesos declarados e auditáveis — não escondidos dentro de uma fórmula opaca
WEIGHTS = {
    "audience_fit": 0.20,
    "product_quality": 0.20,
    "offer_quality": 0.10,
    "market_demand": 0.10,
    "conversion_potential": 0.10,
    "commission_quality": 0.10,
    "refund_risk": 0.10,   # invertido: refund baixo = pontuação alta
    "reputation": 0.10,
}


@dataclass
class ProductScoringInput:
    audience_fit: float | None = None          # 0-1
    product_quality: float | None = None        # 0-1
    offer_quality: float | None = None           # 0-1
    market_demand: float | None = None            # 0-1
    conversion_potential: float | None = None       # 0-1
    commission_pct: float | None = None               # 0-1 (ex: 0.5 = 50%)
    refund_rate: float | None = None                    # 0-1
    reputation_score: float | None = None                 # 0-1
    compliance_ok: bool | None = None
    high_commission_but_low_quality_override: bool = False


@dataclass
class ProductScoreResult:
    overall_score: float | str  # float 0-1, ou "UNKNOWN" se dados insuficientes
    components: dict[str, float | str] = field(default_factory=dict)
    rejected: bool = False
    rejection_reasons: list[str] = field(default_factory=list)


class ProductScoringEngine:
    def score(self, data: ProductScoringInput) -> ProductScoreResult:
        components: dict[str, float | str] = {}
        rejection_reasons: list[str] = []

        # --- Regra de rejeição direta (Seção 37): comissão alta não salva
        # produto ruim.
        if data.commission_pct is not None and data.commission_pct >= 0.6:
            if data.product_quality is not None and data.product_quality < 0.4:
                rejection_reasons.append("comissão alta, mas qualidade do produto baixa")
            if data.refund_rate is not None and data.refund_rate > 0.20:
                rejection_reasons.append("comissão alta, mas refund rate elevado")
            if data.reputation_score is not None and data.reputation_score < 0.4:
                rejection_reasons.append("comissão alta, mas reputação baixa")
            if data.audience_fit is not None and data.audience_fit < 0.3:
                rejection_reasons.append("comissão alta, mas fit com audiência ruim")

        if data.compliance_ok is False:
            rejection_reasons.append("política da plataforma impede a promoção")

        weighted_sum = 0.0
        weight_used = 0.0
        raw_map = {
            "audience_fit": data.audience_fit,
            "product_quality": data.product_quality,
            "offer_quality": data.offer_quality,
            "market_demand": data.market_demand,
            "conversion_potential": data.conversion_potential,
            "commission_quality": data.commission_pct,
            "refund_risk": (1 - data.refund_rate) if data.refund_rate is not None else None,
            "reputation": data.reputation_score,
        }

        for key, value in raw_map.items():
            if value is None:
                components[key] = UNKNOWN
                continue
            components[key] = round(value, 3)
            weighted_sum += value * WEIGHTS[key]
            weight_used += WEIGHTS[key]

        if weight_used == 0:
            overall: float | str = UNKNOWN
        else:
            # normaliza pelo peso efetivamente disponível — nunca inventa
            # dado ausente, apenas reponde à proporção do que existe
            overall = round(weighted_sum / weight_used, 3)

        rejected = len(rejection_reasons) > 0
        return ProductScoreResult(
            overall_score=overall,
            components=components,
            rejected=rejected,
            rejection_reasons=rejection_reasons,
        )
