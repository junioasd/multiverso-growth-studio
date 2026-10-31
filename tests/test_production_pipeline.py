import unittest

from core.production.copyright_gate import AssetRights, LicenseStatus
from core.production.format_engine import BlindCropRejected, PlatformFormat, plan_reframe
from core.production.narration import NarrationLeakDetected, assert_clean_for_voice, split_raw_script
from core.production.pipeline import PipelineRun, ProductionPipeline
from core.production.scene_engine import Niche, ScenePlanRequest, StaticVideoRejected, plan_scenes
from core.production.script_engine import FactClaim, fact_check
from core.production.sync_validator import SyncCheckInput, validate_sync
from core.quality.gates import Verdict


class NarrationSeparationTests(unittest.TestCase):
    def test_splits_labeled_script_correctly(self):
        raw = (
            "NARRATION: Bem-vindo ao vídeo de hoje.\n"
            "SCENE: cena de abertura com cidade ao fundo\n"
            "VISUAL: prompt para gerar skyline noturno\n"
            "NARRATION: Vamos explorar um tema fascinante.\n"
            "MUSIC: trilha instrumental suave\n"
        )
        structured = split_raw_script(raw)
        self.assertIn("Bem-vindo ao vídeo de hoje.", structured.narration_text)
        self.assertIn("Vamos explorar um tema fascinante.", structured.narration_text)
        self.assertEqual(len(structured.scene_instructions), 1)
        self.assertEqual(len(structured.visual_prompts), 1)
        self.assertEqual(len(structured.music_instructions), 1)

    def test_strips_bracket_tags_and_timestamps_from_narration(self):
        raw = "NARRATION: [CUT] Isso aconteceu às 00:12 e foi incrível [IMAGE:city]\n"
        structured = split_raw_script(raw)
        self.assertNotIn("[", structured.narration_text)
        self.assertNotIn("00:12", structured.narration_text)

    def test_assert_clean_for_voice_passes_on_clean_text(self):
        assert_clean_for_voice("Texto limpo sem marcações técnicas.")

    def test_assert_clean_for_voice_blocks_leaked_tags(self):
        with self.assertRaises(NarrationLeakDetected):
            assert_clean_for_voice("Texto com [SCENE] vazado.")


class FormatEngineTests(unittest.TestCase):
    def test_same_aspect_ratio_is_native(self):
        plan = plan_reframe(PlatformFormat.TIKTOK, PlatformFormat.YOUTUBE_SHORTS)
        self.assertEqual(plan.strategy, "native")

    def test_different_aspect_requires_recompose(self):
        plan = plan_reframe(PlatformFormat.YOUTUBE_LONG, PlatformFormat.YOUTUBE_SHORTS)
        self.assertEqual(plan.strategy, "recompose")

    def test_blind_crop_explicitly_rejected(self):
        with self.assertRaises(BlindCropRejected):
            plan_reframe(PlatformFormat.YOUTUBE_LONG, PlatformFormat.TIKTOK, allow_blind_crop=True)


class SceneEngineTests(unittest.TestCase):
    def test_plan_scenes_returns_multiple_scenes_for_long_video(self):
        plan = plan_scenes(ScenePlanRequest(total_duration_seconds=120, niche=Niche.DOCUMENTARY))
        self.assertGreater(plan.scene_count, 1)

    def test_short_form_has_faster_pace(self):
        long_plan = plan_scenes(ScenePlanRequest(total_duration_seconds=60, niche=Niche.ENTERTAINMENT, format_short_form=False))
        short_plan = plan_scenes(ScenePlanRequest(total_duration_seconds=60, niche=Niche.ENTERTAINMENT, format_short_form=True))
        self.assertLess(short_plan.avg_interval_seconds, long_plan.avg_interval_seconds)


class SyncValidatorTests(unittest.TestCase):
    def test_valid_sync_passes(self):
        check = SyncCheckInput(
            word_count=300, words_per_minute=150, planned_scene_count=10, actual_scene_count=10,
            video_duration_seconds=120, audio_duration_seconds=120, trailing_silence_seconds=1.0,
        )
        result = validate_sync(check)
        self.assertTrue(result.ok)

    def test_cut_narration_detected(self):
        check = SyncCheckInput(
            word_count=600, words_per_minute=150, planned_scene_count=10, actual_scene_count=10,
            video_duration_seconds=120, audio_duration_seconds=100, trailing_silence_seconds=1.0,
        )
        result = validate_sync(check)
        self.assertFalse(result.ok)
        self.assertTrue(any("cortada" in issue for issue in result.issues))

    def test_missing_scenes_detected(self):
        check = SyncCheckInput(
            word_count=150, words_per_minute=150, planned_scene_count=10, actual_scene_count=6,
            video_duration_seconds=60, audio_duration_seconds=60, trailing_silence_seconds=1.0,
        )
        result = validate_sync(check)
        self.assertFalse(result.ok)
        self.assertTrue(any("ausentes" in issue for issue in result.issues))

    def test_excessive_trailing_silence_detected(self):
        check = SyncCheckInput(
            word_count=150, words_per_minute=150, planned_scene_count=5, actual_scene_count=5,
            video_duration_seconds=70, audio_duration_seconds=60, trailing_silence_seconds=10.0,
        )
        result = validate_sync(check)
        self.assertFalse(result.ok)


class FactCheckTests(unittest.TestCase):
    def test_all_confident_claims_pass(self):
        claims = [FactClaim("A", source="wiki", confidence=0.9)]
        report = fact_check(claims)
        self.assertEqual(report.status.value, "OK")

    def test_unsourced_claim_is_review_required(self):
        claims = [FactClaim("B", source=None, confidence=None)]
        report = fact_check(claims)
        self.assertEqual(report.status.value, "REVIEW_REQUIRED")

    def test_very_low_confidence_fails(self):
        claims = [FactClaim("C", source="x", confidence=0.1)]
        report = fact_check(claims)
        self.assertEqual(report.status.value, "FAILED")


class CopyrightGateTests(unittest.TestCase):
    def test_own_asset_approved(self):
        from core.production.copyright_gate import CopyrightGate
        gate = CopyrightGate()
        result = gate.evaluate([AssetRights("a1", "self", LicenseStatus.OWN_ASSET)])
        self.assertTrue(result.approved)

    def test_unknown_license_blocked(self):
        from core.production.copyright_gate import CopyrightGate
        gate = CopyrightGate()
        result = gate.evaluate([AssetRights("a1", "web", LicenseStatus.UNKNOWN)])
        self.assertFalse(result.approved)

    def test_ambiguous_license_blocked(self):
        from core.production.copyright_gate import CopyrightGate
        gate = CopyrightGate()
        result = gate.evaluate([AssetRights("a1", "web", LicenseStatus.AMBIGUOUS)])
        self.assertFalse(result.approved)


class PipelineEndToEndTests(unittest.TestCase):
    def test_pipeline_blocks_on_unknown_copyright_and_never_reaches_authorization(self):
        pipeline = ProductionPipeline()
        run = PipelineRun(project_id="p1", channel_id="c1")

        pipeline.run_fact_check(run, [FactClaim("fato", source="fonte", confidence=0.9)])
        pipeline.run_copyright_stage(run, [AssetRights("a1", "web", LicenseStatus.UNKNOWN)])
        pipeline.finalize_preview(run)

        self.assertTrue(run.blocked)
        last_stage = run.stage_results[-1]
        self.assertEqual(last_stage.stage.value, "PREVIEW")
        self.assertFalse(last_stage.ok)

    def test_pipeline_reaches_owner_authorization_when_all_clear(self):
        pipeline = ProductionPipeline()
        run = PipelineRun(project_id="p1", channel_id="c1")

        pipeline.run_fact_check(run, [FactClaim("fato", source="fonte", confidence=0.95)])
        pipeline.run_copyright_stage(run, [AssetRights("a1", "self", LicenseStatus.OWN_ASSET)])
        result = pipeline.finalize_preview(run)

        self.assertTrue(result.ok)
        self.assertEqual(result.stage.value, "AWAITING_OWNER_AUTHORIZATION")

    def test_quality_gate_blocks_on_copyright_unknown_video(self):
        pipeline = ProductionPipeline()
        run = PipelineRun(project_id="p1", channel_id="c1")
        result = pipeline.run_quality_gate(run, {"copyright_status": "UNKNOWN"})
        self.assertEqual(run.final_verdict, Verdict.BLOCKED)
        self.assertFalse(result.ok)

    def test_quality_gate_approves_clean_video(self):
        pipeline = ProductionPipeline()
        run = PipelineRun(project_id="p1", channel_id="c1")
        subject = {
            "audio_complete": True,
            "narration_complete": True,
            "duration_expected_seconds": 100,
            "duration_actual_seconds": 102,
            "static_ratio": 0.1,
            "repeated_single_image": False,
            "irrelevant_scenes": False,
            "deformation_detected": False,
            "sync_offset_ms": 50,
            "captions_error_rate": 0.01,
            "pronunciation_score": 0.9,
            "thumbnail_score": 0.8,
            "fact_check_status": "OK",
            "cta_quality_score": 0.7,
            "music_fit_score": 0.6,
            "copyright_status": "CLEARED",
            "policy_violation": False,
        }
        result = pipeline.run_quality_gate(run, subject)
        self.assertEqual(run.final_verdict, Verdict.APPROVED)
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
