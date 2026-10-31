"""
commercial.affiliate_marketplace_intelligence
================================================
Affiliate Marketplace Intelligence (Seção 30). Pesquisa e analisa
produtos disponíveis em marketplaces/programas de afiliados
legitimamente autorizados.

Fluxo: PLATAFORMA AUTORIZADA -> CATÁLOGO -> PRODUTOS -> ANÁLISE ->
RANKING -> COMPLIANCE -> FIT COM AUDIÊNCIA -> SELEÇÃO/RECOMENDAÇÃO.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from commercial.affiliate_compliance import AffiliateComplianceGate, ComplianceCheckInput
from commercial.product_scoring import ProductScoringEngine, ProductScoringInput
from core.adapters.affiliate.adapters import CatalogProduct, get_adapter
from core.quality.gates import Verdict


@dataclass
class RankedProduct:
    catalog_product: CatalogProduct
    score_result: dict
    compliance_verdict: str
    recommended: bool
    reasons: list[str] = field(default_factory=list)


class AffiliateMarketplaceIntelligence:
    def __init__(self) -> None:
        self.scoring_engine = ProductScoringEngine()
        self.compliance_gate = AffiliateComplianceGate()

    def discover_catalog(self, platform: str, niche: str | None = None) -> list[CatalogProduct]:
        adapter = get_adapter(platform)
        result = adapter.fetch_catalog(niche)
        if not result.success:
            return []
        products_raw = result.data.get("products", [])
        return [CatalogProduct(**p) for p in products_raw]

    def analyze_and_rank(
        self,
        products: list[CatalogProduct],
        *,
        audience_fit_by_product: dict[str, float] | None = None,
        compliance_by_product: dict[str, ComplianceCheckInput] | None = None,
    ) -> list[RankedProduct]:
        audience_fit_by_product = audience_fit_by_product or {}
        compliance_by_product = compliance_by_product or {}

        ranked: list[RankedProduct] = []
        for product in products:
            fit = audience_fit_by_product.get(product.external_id)

            scoring_input = ProductScoringInput(
                audience_fit=fit,
                product_quality=product.reputation_score,  # proxy quando não há dado separado
                offer_quality=None,
                market_demand=None,
                conversion_potential=None,
                commission_pct=product.commission_pct,
                refund_rate=product.refund_rate,
                reputation_score=product.reputation_score,
                compliance_ok=None,
            )
            score_result = self.scoring_engine.score(scoring_input)

            compliance_input = compliance_by_product.get(product.external_id, ComplianceCheckInput())
            compliance_result = self.compliance_gate.evaluate(compliance_input.__dict__)

            recommended = (
                not score_result.rejected
                and compliance_result.verdict == Verdict.APPROVED
                and isinstance(score_result.overall_score, float)
                and score_result.overall_score >= 0.55
            )

            reasons = list(score_result.rejection_reasons) + list(compliance_result.reasons)

            ranked.append(
                RankedProduct(
                    catalog_product=product,
                    score_result={
                        "overall_score": score_result.overall_score,
                        "components": score_result.components,
                    },
                    compliance_verdict=compliance_result.verdict.value,
                    recommended=recommended,
                    reasons=reasons,
                )
            )

        # Ordena por score quando numérico; UNKNOWN vai para o fim.
        def sort_key(rp: RankedProduct):
            score = rp.score_result["overall_score"]
            return (0, -score) if isinstance(score, float) else (1, 0)

        ranked.sort(key=sort_key)
        return ranked
