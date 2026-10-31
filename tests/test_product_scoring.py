import unittest

from commercial.product_scoring import ProductScoringEngine, ProductScoringInput


class ProductScoringTests(unittest.TestCase):
    def setUp(self):
        self.engine = ProductScoringEngine()

    def test_high_fit_product_scores_well(self):
        data = ProductScoringInput(
            audience_fit=0.9, product_quality=0.85, offer_quality=0.8, market_demand=0.7,
            conversion_potential=0.7, commission_pct=0.4, refund_rate=0.05, reputation_score=0.85,
            compliance_ok=True,
        )
        result = self.engine.score(data)
        self.assertFalse(result.rejected)
        self.assertGreater(result.overall_score, 0.6)

    def test_low_fit_product_scores_poorly(self):
        data = ProductScoringInput(
            audience_fit=0.1, product_quality=0.3, offer_quality=0.2, market_demand=0.2,
            conversion_potential=0.2, commission_pct=0.3, refund_rate=0.3, reputation_score=0.2,
        )
        result = self.engine.score(data)
        self.assertLess(result.overall_score, 0.4)

    def test_high_commission_low_quality_rejected(self):
        data = ProductScoringInput(
            audience_fit=0.8, product_quality=0.2, commission_pct=0.7, refund_rate=0.05, reputation_score=0.8,
        )
        result = self.engine.score(data)
        self.assertTrue(result.rejected)
        self.assertTrue(any("qualidade" in r for r in result.rejection_reasons))

    def test_low_commission_excellent_fit_not_penalized(self):
        data = ProductScoringInput(
            audience_fit=0.9, product_quality=0.9, commission_pct=0.15, refund_rate=0.02, reputation_score=0.9,
        )
        result = self.engine.score(data)
        self.assertFalse(result.rejected)

    def test_high_refund_rejected(self):
        data = ProductScoringInput(
            audience_fit=0.8, product_quality=0.7, commission_pct=0.65, refund_rate=0.35, reputation_score=0.7,
        )
        result = self.engine.score(data)
        self.assertTrue(result.rejected)

    def test_incompatible_policy_rejected(self):
        data = ProductScoringInput(audience_fit=0.8, product_quality=0.8, compliance_ok=False)
        result = self.engine.score(data)
        self.assertTrue(result.rejected)

    def test_insufficient_data_returns_unknown(self):
        data = ProductScoringInput()
        result = self.engine.score(data)
        self.assertEqual(result.overall_score, "UNKNOWN")

    def test_never_invents_score_for_missing_fields(self):
        data = ProductScoringInput(audience_fit=0.9)  # só um campo informado
        result = self.engine.score(data)
        for key, value in result.components.items():
            if key != "audience_fit":
                self.assertEqual(value, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
