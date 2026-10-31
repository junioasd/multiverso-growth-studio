"""
commercial.experiment_engine
==============================
Experiment Engine (Seção 51). Prepara experimentos A/B para hook,
title, thumbnail, CTA, offer, landing page, funnel. Não declara
vencedor com amostra insuficiente.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_SAMPLE_PER_VARIANT = 100  # amostra mínima conservadora antes de declarar vencedor
MIN_Z_SCORE_SIGNIFICANT = 1.96  # ~95% de confiança


@dataclass
class VariantResult:
    name: str
    impressions: int
    conversions: int

    @property
    def conversion_rate(self) -> float:
        return self.conversions / self.impressions if self.impressions else 0.0


@dataclass
class ExperimentVerdict:
    winner: str | None
    significant: bool
    reason: str


class ExperimentEngine:
    def evaluate_ab(self, variant_a: VariantResult, variant_b: VariantResult) -> ExperimentVerdict:
        if variant_a.impressions < MIN_SAMPLE_PER_VARIANT or variant_b.impressions < MIN_SAMPLE_PER_VARIANT:
            return ExperimentVerdict(
                winner=None,
                significant=False,
                reason=(
                    f"amostra insuficiente (mínimo {MIN_SAMPLE_PER_VARIANT} por variante); "
                    "não é possível declarar vencedor"
                ),
            )

        p1, n1 = variant_a.conversion_rate, variant_a.impressions
        p2, n2 = variant_b.conversion_rate, variant_b.impressions
        p_pool = (variant_a.conversions + variant_b.conversions) / (n1 + n2)

        if p_pool in (0, 1):
            return ExperimentVerdict(winner=None, significant=False, reason="sem variância suficiente nos dados")

        se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
        if se == 0:
            return ExperimentVerdict(winner=None, significant=False, reason="erro padrão zero; dados insuficientes")

        z = (p1 - p2) / se

        if abs(z) < MIN_Z_SCORE_SIGNIFICANT:
            return ExperimentVerdict(
                winner=None, significant=False, reason=f"diferença não estatisticamente significativa (z={z:.2f})"
            )

        winner = variant_a.name if p1 > p2 else variant_b.name
        return ExperimentVerdict(winner=winner, significant=True, reason=f"significativo (z={z:.2f})")
