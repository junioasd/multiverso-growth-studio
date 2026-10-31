import unittest

from commercial.funnel_engine import FunnelSelectionInput
from commercial.multigerenciador import MultiGerenciadorComercial
from core.models.entities import ValueType


class MultiGerenciadorTests(unittest.TestCase):
    def setUp(self):
        self.mgc = MultiGerenciadorComercial()

    def test_assess_opportunity_end_to_end(self):
        assessment = self.mgc.assess_opportunity(
            channel_id="chan_1",
            platform="youtube",
            affiliate_platform="hotmart",
            niche="saude",
            funnel_request=FunnelSelectionInput(platform="youtube", audience_trust_level="MEDIUM"),
        )
        self.assertEqual(assessment.channel_id, "chan_1")
        self.assertGreater(len(assessment.ranked_products), 0)
        self.assertIsNotNone(assessment.funnel_recommendation)

    def test_scaling_readiness_without_data_is_unknown_not_ready(self):
        readiness = self.mgc.evaluate_scaling_readiness("chan_new")
        self.assertFalse(readiness["ready_to_scale"])

    def test_scaling_readiness_with_good_data(self):
        for _ in range(40):
            self.mgc.sales_tracker.record_event(channel_id="chan_1", event_type="click")
        for _ in range(15):
            self.mgc.sales_tracker.record_event(channel_id="chan_1", event_type="lead")
        for _ in range(5):
            self.mgc.sales_tracker.record_event(
                channel_id="chan_1", event_type="sale", value_type=ValueType.REAL, amount=100
            )
        self.mgc.finance.record(entry_type="revenue", amount=500, channel_id="chan_1")
        self.mgc.finance.record(entry_type="realized_cost", amount=50, channel_id="chan_1")

        readiness = self.mgc.evaluate_scaling_readiness("chan_1")
        # compliance ainda UNKNOWN por padrão -> não aprova sozinho, mas não deve quebrar
        self.assertIn("gate_verdict", readiness)


if __name__ == "__main__":
    unittest.main()
