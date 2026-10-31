import unittest

from commercial.affiliate_compliance import AffiliateComplianceGate, ComplianceCheckInput
from commercial.affiliate_marketplace_intelligence import AffiliateMarketplaceIntelligence
from core.adapters.affiliate.adapters import get_adapter
from core.quality.gates import Verdict


class AffiliateComplianceGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = AffiliateComplianceGate()

    def test_all_unknown_blocks(self):
        result = self.gate.evaluate(ComplianceCheckInput().__dict__)
        self.assertEqual(result.verdict, Verdict.BLOCKED)

    def test_all_true_approves(self):
        data = ComplianceCheckInput(
            platform_rules_reviewed=True, producer_terms_reviewed=True, traffic_restrictions_ok=True,
            keyword_restrictions_ok=True, brand_usage_authorized=True, image_usage_authorized=True,
            claims_within_policy=True, social_platform_policy_ok=True, affiliate_disclosure_present=True,
            legal_requirements_met=True,
        )
        result = self.gate.evaluate(data.__dict__)
        self.assertEqual(result.verdict, Verdict.APPROVED)

    def test_disclosure_absent_blocks(self):
        data = ComplianceCheckInput(
            platform_rules_reviewed=True, producer_terms_reviewed=True, claims_within_policy=True,
            affiliate_disclosure_present=False, legal_requirements_met=True,
        )
        result = self.gate.evaluate(data.__dict__)
        self.assertEqual(result.verdict, Verdict.BLOCKED)

    def test_non_critical_unknown_only_review_required(self):
        data = ComplianceCheckInput(
            platform_rules_reviewed=True, producer_terms_reviewed=True, claims_within_policy=True,
            affiliate_disclosure_present=True, legal_requirements_met=True,
            traffic_restrictions_ok=None,  # não crítico
        )
        result = self.gate.evaluate(data.__dict__)
        self.assertEqual(result.verdict, Verdict.REVIEW_REQUIRED)


class AffiliateAdapterCatalogTests(unittest.TestCase):
    def test_unregistered_platform_raises(self):
        with self.assertRaises(ValueError):
            get_adapter("plataforma_inexistente")

    def test_mock_catalog_returned_when_unconfigured(self):
        adapter = get_adapter("hotmart")
        result = adapter.fetch_catalog("emagrecimento")
        self.assertTrue(result.success)
        self.assertGreater(len(result.data["products"]), 0)

    def test_empty_catalog_handled_gracefully(self):
        intel = AffiliateMarketplaceIntelligence()
        ranked = intel.analyze_and_rank([])
        self.assertEqual(ranked, [])


class AffiliateMarketplaceIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.intel = AffiliateMarketplaceIntelligence()

    def test_discover_and_rank_end_to_end(self):
        catalog = self.intel.discover_catalog("hotmart", "saude")
        self.assertGreater(len(catalog), 0)
        ranked = self.intel.analyze_and_rank(
            catalog, audience_fit_by_product={p.external_id: 0.8 for p in catalog}
        )
        self.assertEqual(len(ranked), len(catalog))

    def test_high_refund_product_not_recommended(self):
        catalog = self.intel.discover_catalog("hotmart", "saude")
        # produto B do mock tem refund_rate=0.22, reputação 0.4 -> não deve ser recomendado sem compliance ok
        ranked = self.intel.analyze_and_rank(catalog)
        product_b = next(rp for rp in ranked if "B" in rp.catalog_product.name)
        self.assertFalse(product_b.recommended)


if __name__ == "__main__":
    unittest.main()
