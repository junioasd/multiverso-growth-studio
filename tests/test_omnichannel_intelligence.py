import unittest

from commercial.omnichannel_intelligence import (
    ChannelType, CreativePrinciple, OmnichannelIntelligence, PrincipleCategory,
)
from commercial.sales_tracking import SalesTracker
from core.learning.loop import LearningLoop, Prediction

"""
Testes BLOCO FINAL 1 (item 5) — Omnichannel Campaign Intelligence:
classificação real de tráfego, comparação orgânico vs. ads sobre
dados reais, princípios (nunca cópia de criativo), propagação
bidirecional só com aprendizado validado estatisticamente.
"""


class TrafficClassificationTests(unittest.TestCase):
    def test_paid_markers_classified_as_ads(self):
        intel = OmnichannelIntelligence()
        for source in ("google_ads", "facebook_ads", "tiktok_ads", "cpc_campaign"):
            self.assertEqual(intel.classify_traffic_source(source), ChannelType.PAID_ADS)

    def test_organic_source_classified_correctly(self):
        intel = OmnichannelIntelligence()
        self.assertEqual(intel.classify_traffic_source("youtube_organic"), ChannelType.ORGANIC)

    def test_empty_source_never_defaults_to_organic(self):
        intel = OmnichannelIntelligence()
        self.assertEqual(intel.classify_traffic_source(None), ChannelType.UNKNOWN)
        self.assertEqual(intel.classify_traffic_source(""), ChannelType.UNKNOWN)


class PerformanceMeasurementTests(unittest.TestCase):
    def test_unavailable_without_sales_tracker(self):
        intel = OmnichannelIntelligence()
        self.assertIsNone(intel.measure_performance(channel_id="c1"))

    def test_compare_organic_vs_ads_uses_real_data(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type="REAL", amount=100.0, traffic_source="google_ads", idempotency_key="s1")
        tracker.record_event(channel_id="c1", event_type="sale", value_type="REAL", amount=50.0, traffic_source="youtube_organic", idempotency_key="s2")
        intel = OmnichannelIntelligence(sales_tracker=tracker)
        result = intel.compare_organic_vs_ads(channel_id="c1")
        self.assertEqual(result["ads"]["real_revenue"], 100.0)
        self.assertEqual(result["organic"]["real_revenue"], 50.0)

    def test_compare_unavailable_without_tracker(self):
        intel = OmnichannelIntelligence()
        result = intel.compare_organic_vs_ads(channel_id="c1")
        self.assertEqual(result["status"], "UNAVAILABLE")


class CreativePrincipleTests(unittest.TestCase):
    def test_long_description_rejected_prevents_pasting_full_creative(self):
        with self.assertRaises(ValueError):
            CreativePrinciple.new(
                category=PrincipleCategory.HOOK, description="x" * 501, origin_channel_type=ChannelType.ORGANIC,
            )

    def test_empty_description_rejected(self):
        with self.assertRaises(ValueError):
            CreativePrinciple.new(category=PrincipleCategory.HOOK, description="", origin_channel_type=ChannelType.ORGANIC)

    def test_valid_principle_recorded(self):
        intel = OmnichannelIntelligence()
        p = intel.record_principle(CreativePrinciple.new(
            category=PrincipleCategory.CTA, description="CTA direto no meio do vídeo", origin_channel_type=ChannelType.ORGANIC,
        ))
        self.assertEqual(len(intel.principles_by_category(PrincipleCategory.CTA)), 1)
        self.assertEqual(intel.principles_by_category(PrincipleCategory.HOOK), [])


class BidirectionalPropagationTests(unittest.TestCase):
    def test_no_suggestion_without_validated_learning(self):
        intel = OmnichannelIntelligence(learning_loop=LearningLoop())
        principle = intel.record_principle(CreativePrinciple.new(
            category=PrincipleCategory.HOOK, description="x", origin_channel_type=ChannelType.PAID_ADS, origin_channel_id="c1",
        ))
        suggestion = intel.suggest_bidirectional_propagation(principle_id=principle.principle_id, metric_name="ctr", scope_channel_id="c1")
        self.assertIsNone(suggestion)

    def test_suggestion_only_after_statistical_validation(self):
        loop = LearningLoop()
        intel = OmnichannelIntelligence(learning_loop=loop)
        principle = intel.record_principle(CreativePrinciple.new(
            category=PrincipleCategory.HOOK, description="pergunta retórica", origin_channel_type=ChannelType.PAID_ADS,
            origin_channel_id="c1",
        ))
        for _ in range(6):
            p = loop.record_prediction(Prediction.new(agent_id="PULSE", metric_name="ctr", predicted_value=0.5, context="x", scope_channel_id="c1"))
            loop.record_outcome(prediction_id=p.prediction_id, actual_value=0.6)
        loop.promote_validated_learning(metric_name="ctr", statement="x", validated_by="ECHO", scope_channel_id="c1")

        suggestion = intel.suggest_bidirectional_propagation(principle_id=principle.principle_id, metric_name="ctr", scope_channel_id="c1")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion.from_channel_type, ChannelType.PAID_ADS)
        self.assertEqual(suggestion.to_channel_type, ChannelType.ORGANIC)

    def test_unknown_principle_returns_none(self):
        intel = OmnichannelIntelligence()
        result = intel.suggest_bidirectional_propagation(principle_id="nao_existe", metric_name="ctr")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
