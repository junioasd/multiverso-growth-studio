"""
core.evidence.claims
========================
EXPANSÃO — item 21 (evidência vs inferência).

Distinção obrigatória entre 5 tipos de afirmação — reutilizada por
`core.multimodal` (evidência multimodal) e `radar_web` (RADAR WEB
COMERCIAL). Nunca transforma "parece escalar" em "converte em X%" sem
dado verificável — a diferença entre `FACT` e as demais categorias É
precisamente essa barreira.
"""

from __future__ import annotations

from enum import Enum


class ClaimType(str, Enum):
    FACT = "FACT"
    ESTIMATE = "ESTIMATE"
    INFERENCE = "INFERENCE"
    HYPOTHESIS = "HYPOTHESIS"
    UNKNOWN = "UNKNOWN"
    # COMANDO 2/6: PREVISÃO/SIMULAÇÃO — valor projetado para o futuro
    # (ou cenário simulado). Nunca FACT, mesmo quando calculado a partir
    # de fatos observados; o Gêmeo Digital do ATLAS o mantém separado.
    FORECAST = "FORECAST"


# Métricas que NUNCA podem ser inventadas — só entram como FACT quando
# vierem de uma fonte verificável (ex: dados operacionais próprios via
# `commercial/sales_tracking.py`, nunca de observação de mercado).
# CORREÇÃO (item 9): lista expandida com as métricas comerciais
# protegidas exigidas — cada uma exige source, provenance,
# observed_at, context, measurement_type, confidence e evidence;
# nunca vira FACT sem isso.
NEVER_FABRICATED_METRICS = frozenset({
    "roi", "faturamento", "revenue", "conversion_rate", "cac", "ltv", "sales_volume", "sales",
    "ctr", "cpc", "cpa", "roas", "aov", "refund_rate",
    # CORREÇÃO (item 12 desta rodada): CPM, MARGIN, PROFIT adicionados.
    "cpm", "margin", "profit",
})


def requires_verifiable_source(metric_name: str) -> bool:
    return metric_name.lower() in NEVER_FABRICATED_METRICS
