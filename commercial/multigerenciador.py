"""
commercial.multigerenciador
=============================
MULTIGERENCIADOR COMERCIAL / Organic Commerce Orchestrator (Seção 29).

Fluxo:
MERCADO -> AUDIÊNCIA -> OPORTUNIDADE -> PRODUTO -> OFERTA -> CANAL ->
FUNIL -> CONTEÚDO -> DISTRIBUIÇÃO -> LEAD -> CONVERSÃO -> RECEITA ->
MARGEM -> APRENDIZADO -> OTIMIZAÇÃO.

Este orquestrador não inventa dados: cada etapa depende de inputs
concretos (catálogo real/mock, métricas reais/mock) e delega para os
módulos especializados já implementados.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from commercial.affiliate_marketplace_intelligence import (
    AffiliateMarketplaceIntelligence,
    RankedProduct,
)
from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.funnel_engine import FunnelEngine, FunnelSelectionInput, FunnelRecommendation
from commercial.learning_loop import LearningLoop
from commercial.sales_tracking import SalesTracker
from core.quality.commercial_quality import CommercialQualityGate
from core.quality.gates import Verdict


@dataclass
class OpportunityAssessment:
    channel_id: str
    platform: str
    ranked_products: list[RankedProduct]
    funnel_recommendation: FunnelRecommendation
    top_pick: RankedProduct | None


class MultiGerenciadorComercial:
    """Orquestrador comercial central. Mantém instâncias dos submódulos
    para uso consistente dentro de uma sessão/piloto."""

    def __init__(self, *, db_path: str | None = None) -> None:
        # MICROCORREÇÃO 2 (PERSISTÊNCIA): quando `db_path` é informado,
        # CRM/SalesTracker/Finance passam a persistir em SQLite
        # (write-through) e recarregam o estado existente no boot —
        # sobrevivem a reinício do processo. `learning_loop` e
        # `affiliate_intelligence`/`funnel_engine` continuam em memória
        # nesta fase (fora do escopo desta microcorreção).
        self.affiliate_intelligence = AffiliateMarketplaceIntelligence()
        self.funnel_engine = FunnelEngine()
        self.crm = CRM(db_path=db_path)
        self.sales_tracker = SalesTracker(db_path=db_path)
        self.finance = FinanceLedger(db_path=db_path)
        self.learning_loop = LearningLoop()
        self.commercial_quality_gate = CommercialQualityGate()

    def assess_opportunity(
        self,
        *,
        channel_id: str,
        platform: str,
        affiliate_platform: str,
        niche: str,
        funnel_request: FunnelSelectionInput,
        audience_fit_by_product: dict[str, float] | None = None,
    ) -> OpportunityAssessment:
        """Etapas MERCADO -> AUDIÊNCIA -> OPORTUNIDADE -> PRODUTO -> FUNIL."""
        catalog = self.affiliate_intelligence.discover_catalog(affiliate_platform, niche)
        ranked = self.affiliate_intelligence.analyze_and_rank(
            catalog, audience_fit_by_product=audience_fit_by_product
        )
        funnel_recommendation = self.funnel_engine.recommend(funnel_request)

        top_pick = next((rp for rp in ranked if rp.recommended), None)

        return OpportunityAssessment(
            channel_id=channel_id,
            platform=platform,
            ranked_products=ranked,
            funnel_recommendation=funnel_recommendation,
            top_pick=top_pick,
        )

    def evaluate_scaling_readiness(self, channel_id: str) -> dict:
        """Antes de escalar produto/oferta (Seção 50): combina sales
        tracking + finance + commercial quality gate."""
        sales_summary = self.sales_tracker.summarize(channel_id)
        finance_snapshot = self.finance.snapshot(channel_id)

        conversion_rate = (
            sales_summary.sales / sales_summary.clicks if sales_summary.clicks else None
        )
        refund_rate = (
            sales_summary.refunds / sales_summary.sales if sales_summary.sales else None
        )

        gate_subject = {
            "clicks": sales_summary.clicks,
            "leads": sales_summary.leads,
            "sales": sales_summary.sales,
            "conversion_rate": conversion_rate,
            "refund_rate": refund_rate,
            "revenue": sales_summary.real_revenue,
            "margin": finance_snapshot.margin,
            "complaints": 0,
            "compliance_status": "UNKNOWN",
        }
        gate_result = self.commercial_quality_gate.evaluate(gate_subject)

        return {
            "sales_summary": sales_summary,
            "finance_snapshot": finance_snapshot,
            "gate_verdict": gate_result.verdict.value,
            "gate_reasons": gate_result.reasons,
            "ready_to_scale": gate_result.verdict == Verdict.APPROVED,
        }
