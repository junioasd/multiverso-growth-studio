import unittest

from youtube_strategy.alignment_checker import AlignmentCheckInput, YouTubeAlignmentChecker
from youtube_strategy.strategy_engine import (
    LegitimateSignals,
    ProhibitedTacticRequested,
    YouTubeStrategyEngine,
)


class ProhibitedTacticsTests(unittest.TestCase):
    def setUp(self):
        self.engine = YouTubeStrategyEngine()

    def test_fake_views_rejected(self):
        with self.assertRaises(ProhibitedTacticRequested):
            self.engine.request_growth_action("fake_views")

    def test_bot_farms_rejected(self):
        with self.assertRaises(ProhibitedTacticRequested):
            self.engine.request_growth_action("bot_farms")

    def test_metadata_stuffing_rejected(self):
        with self.assertRaises(ProhibitedTacticRequested):
            self.engine.request_growth_action("metadata_stuffing")

    def test_legitimate_action_not_rejected(self):
        # não deve levantar exceção
        self.engine.request_growth_action("improve_retention")


class StrategyRecommendationTests(unittest.TestCase):
    def test_low_retention_flagged(self):
        engine = YouTubeStrategyEngine()
        rec = engine.recommend_focus(LegitimateSignals(retention_rate=0.2))
        self.assertIn("retenção", rec.focus_areas)

    def test_no_signals_defaults_to_maintain(self):
        engine = YouTubeStrategyEngine()
        rec = engine.recommend_focus(LegitimateSignals())
        self.assertIn("manter_estrategia_atual", rec.focus_areas)


class AlignmentCheckerTests(unittest.TestCase):
    def test_all_unresolved_when_no_data(self):
        checker = YouTubeAlignmentChecker()
        report = checker.check(AlignmentCheckInput())
        self.assertFalse(report.ready_to_publish)
        self.assertGreater(len(report.unresolved_questions), 0)

    def test_ready_when_all_positive_and_no_problems(self):
        checker = YouTubeAlignmentChecker()
        data = AlignmentCheckInput(
            audience_appropriate=True,
            thumbnail_matches_content=True,
            title_matches_promise=True,
            strong_opening_seconds=True,
            maintains_interest=True,
            has_original_value=True,
            excessive_repetition=False,
            reused_without_transformation=False,
            feels_industrial_repetitive=False,
            fulfills_search_intent=True,
            coherent_channel_content_audience=True,
        )
        report = checker.check(data)
        self.assertTrue(report.ready_to_publish)

    def test_industrial_repetitive_blocks_publish(self):
        checker = YouTubeAlignmentChecker()
        data = AlignmentCheckInput(
            audience_appropriate=True, thumbnail_matches_content=True, title_matches_promise=True,
            strong_opening_seconds=True, maintains_interest=True, has_original_value=True,
            excessive_repetition=False, reused_without_transformation=False,
            feels_industrial_repetitive=True, fulfills_search_intent=True,
            coherent_channel_content_audience=True,
        )
        report = checker.check(data)
        self.assertFalse(report.ready_to_publish)
        self.assertIn("Parece produção industrial repetitiva?", report.problems)


if __name__ == "__main__":
    unittest.main()
