import unittest

from core.learning.loop import LearningLoop, Prediction
from youtube_strategy.distribution_engine import (
    ContentCharacteristics, ContentFormat, DistributionPlanner, FormatSelector, PackagingEvaluator,
)

"""
Testes BLOCO FINAL 1 (item 4) — YouTube Distribution & Packaging
Engine: avaliação real de título/thumbnail, seleção real de formato,
planejamento de distribuição integrado ao Learning Loop.
"""


class PackagingEvaluatorTests(unittest.TestCase):
    def test_empty_title_blocked(self):
        r = PackagingEvaluator().evaluate_title("")
        self.assertEqual(r.verdict, "BLOCKED")

    def test_misleading_marker_flagged(self):
        r = PackagingEvaluator().evaluate_title("CHOCANTE: isso vai mudar sua vida!!!")
        self.assertEqual(r.verdict, "NEEDS_REVIEW")
        self.assertTrue(any("clickbait" in issue for issue in r.issues))

    def test_normal_title_ok(self):
        r = PackagingEvaluator().evaluate_title("Guia completo de Python para iniciantes")
        self.assertEqual(r.verdict, "OK")

    def test_thumbnail_unconfirmed_is_needs_review_never_approved(self):
        r = PackagingEvaluator().evaluate_thumbnail_matches_content(thumbnail_matches_content=None)
        self.assertEqual(r.verdict, "NEEDS_REVIEW")

    def test_thumbnail_mismatch_blocked(self):
        r = PackagingEvaluator().evaluate_thumbnail_matches_content(thumbnail_matches_content=False)
        self.assertEqual(r.verdict, "BLOCKED")


class FormatSelectorTests(unittest.TestCase):
    def test_short_duration_recommends_short(self):
        r = FormatSelector().decide(ContentCharacteristics(duration_seconds=30))
        self.assertEqual(r.recommended_format, ContentFormat.SHORT)

    def test_long_duration_recommends_longform(self):
        r = FormatSelector().decide(ContentCharacteristics(duration_seconds=900))
        self.assertEqual(r.recommended_format, ContentFormat.LONG_FORM)

    def test_missing_duration_never_fabricates_decision(self):
        r = FormatSelector().decide(ContentCharacteristics())
        self.assertIsNone(r.recommended_format)
        self.assertEqual(r.confidence, 0.0)

    def test_short_but_needs_deep_explanation_overrides_to_longform(self):
        r = FormatSelector().decide(ContentCharacteristics(duration_seconds=50, requires_deep_explanation=True))
        self.assertEqual(r.recommended_format, ContentFormat.LONG_FORM)


class DistributionPlannerTests(unittest.TestCase):
    def test_full_plan_ready_to_publish(self):
        planner = DistributionPlanner()
        plan = planner.plan_distribution(
            channel_id="c1", title="Tutorial de edição de vídeo",
            characteristics=ContentCharacteristics(duration_seconds=45), thumbnail_matches_content=True,
        )
        self.assertTrue(plan["ready_to_publish"])

    def test_blocked_thumbnail_never_ready(self):
        planner = DistributionPlanner()
        plan = planner.plan_distribution(
            channel_id="c1", title="titulo normal", characteristics=ContentCharacteristics(duration_seconds=45),
            thumbnail_matches_content=False,
        )
        self.assertFalse(plan["ready_to_publish"])

    def test_uses_real_validated_learnings_scoped_by_channel(self):
        loop = LearningLoop()
        for _ in range(6):
            p = loop.record_prediction(Prediction.new(
                agent_id="PULSE", metric_name="ctr", predicted_value=0.5, context="x", scope_channel_id="c1",
            ))
            loop.record_outcome(prediction_id=p.prediction_id, actual_value=0.6)
        loop.promote_validated_learning(metric_name="ctr", statement="thumbnails com rosto convertem mais", validated_by="ECHO", scope_channel_id="c1")

        planner = DistributionPlanner(learning_loop=loop)
        plan = planner.plan_distribution(
            channel_id="c1", title="x", characteristics=ContentCharacteristics(duration_seconds=45),
            thumbnail_matches_content=True,
        )
        self.assertEqual(len(plan["applicable_learnings"]), 1)

        # canal diferente nunca ve o aprendizado do canal c1
        plan_other = planner.plan_distribution(
            channel_id="c2", title="x", characteristics=ContentCharacteristics(duration_seconds=45),
            thumbnail_matches_content=True,
        )
        self.assertEqual(len(plan_other["applicable_learnings"]), 0)

    def test_video_level_prediction_recorded(self):
        planner = DistributionPlanner()
        pred = planner.record_video_prediction(
            channel_id="c1", video_id="vid_123", metric_name="retention", predicted_value=0.6, context="teste",
        )
        self.assertIn("vid_123", pred.context)
        self.assertEqual(pred.scope_channel_id, "c1")


if __name__ == "__main__":
    unittest.main()
