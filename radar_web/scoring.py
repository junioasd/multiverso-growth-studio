"""
radar_web.scoring
=====================
RADAR WEB COMERCIAL — Funnel Intelligence Score (item 10).

Score TRANSPARENTE — cada dimensão é um número explícito fornecido
pelo chamador (nunca inventado aqui) entre 0.0 e 1.0. `confidence`
fica sempre SEPARADO do `overall` — o score nunca mascara incerteza
(item 10: "Score não deve mascarar incerteza").
"""

from __future__ import annotations

from dataclasses import dataclass, fields

_POSITIVE_DIMENSIONS = (
    "evidence_strength", "market_presence", "growth_signal", "longevity", "repeatability",
    "conversion_signal", "scalability", "channel_fit", "product_fit",
)
_RISK_DIMENSIONS = ("saturation_risk", "implementation_cost", "operational_complexity")


@dataclass
class FunnelIntelligenceScore:
    evidence_strength: float
    market_presence: float
    growth_signal: float
    longevity: float
    repeatability: float
    conversion_signal: float
    scalability: float
    channel_fit: float
    product_fit: float
    saturation_risk: float
    implementation_cost: float
    operational_complexity: float
    confidence: float  # SEMPRE separado do overall — nunca embutido no cálculo

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"dimensão '{f.name}' deve estar entre 0.0 e 1.0 — recebido {value}")

    @property
    def overall(self) -> float:
        """Média simples e transparente das dimensões positivas menos a
        média das dimensões de risco/custo — fórmula auditável, nunca
        uma caixa-preta. Sempre reportado JUNTO com `confidence`,
        nunca sozinho."""
        positive_avg = sum(getattr(self, d) for d in _POSITIVE_DIMENSIONS) / len(_POSITIVE_DIMENSIONS)
        risk_avg = sum(getattr(self, d) for d in _RISK_DIMENSIONS) / len(_RISK_DIMENSIONS)
        raw = positive_avg - (risk_avg * 0.5)
        return max(0.0, min(1.0, raw))


class FunnelIntelligenceScorer:
    def score(self, *, dimensions: dict, confidence: float) -> FunnelIntelligenceScore:
        """`dimensions` precisa trazer TODAS as chaves esperadas —
        nunca preenche uma dimensão ausente com um valor inventado."""
        expected = {f.name for f in fields(FunnelIntelligenceScore) if f.name != "confidence"}
        missing = expected - set(dimensions.keys())
        if missing:
            raise ValueError(f"dimensões ausentes, nunca preenchidas com valor inventado: {sorted(missing)}")
        return FunnelIntelligenceScore(confidence=confidence, **dimensions)
