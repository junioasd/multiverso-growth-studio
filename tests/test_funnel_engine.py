import unittest

from commercial.funnel_engine import FunnelEngine, FunnelSelectionInput
from core.models.entities import FunnelType


class FunnelEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = FunnelEngine()

    def test_cold_audience_gets_lead_magnet(self):
        rec = self.engine.recommend(
            FunnelSelectionInput(platform="youtube", audience_trust_level="LOW", audience_maturity="COLD")
        )
        self.assertEqual(rec.funnel_type, FunnelType.LEAD_MAGNET)

    def test_high_ticket_gets_webinar(self):
        rec = self.engine.recommend(
            FunnelSelectionInput(platform="youtube", ticket_price=1500, audience_trust_level="MEDIUM")
        )
        self.assertEqual(rec.funnel_type, FunnelType.WEBINAR)

    def test_hot_trusted_audience_gets_direct_offer(self):
        rec = self.engine.recommend(
            FunnelSelectionInput(platform="instagram", audience_trust_level="HIGH", audience_maturity="HOT")
        )
        self.assertEqual(rec.funnel_type, FunnelType.DIRECT_TO_OFFER)

    def test_unknown_data_returns_low_confidence(self):
        rec = self.engine.recommend(FunnelSelectionInput(platform="tiktok"))
        self.assertEqual(rec.confidence, "LOW")

    def test_platform_template_included(self):
        rec = self.engine.recommend(FunnelSelectionInput(platform="tiktok"))
        self.assertIn("descoberta", rec.template_sequence)


if __name__ == "__main__":
    unittest.main()
