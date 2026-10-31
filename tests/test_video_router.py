import unittest

from core.adapters.base import AdapterMode
from core.adapters.video.router import SceneRequirements, VideoModelRouter


class VideoModelRouterTests(unittest.TestCase):
    def setUp(self):
        self.router = VideoModelRouter()

    def test_no_provider_configured_falls_back_to_mock(self):
        req = SceneRequirements(scene_type="cinematic", needs_3d=True)
        decision = self.router.choose_provider(req)
        self.assertEqual(decision.mode, AdapterMode.MOCK)
        self.assertEqual(decision.chosen_provider, "mock")

    def test_generate_scene_returns_mock_asset(self):
        req = SceneRequirements(scene_type="infographic", duration_seconds=8)
        result = self.router.generate_scene(req)
        self.assertTrue(result.success)
        self.assertEqual(result.mode, AdapterMode.MOCK)
        self.assertIn("asset_ref", result.data)

    def test_requirements_filter_by_3d_support(self):
        req = SceneRequirements(scene_type="character_dialogue", needs_3d=True, needs_character_consistency=True)
        decision = self.router.choose_provider(req)
        # sem credenciais reais, sempre cai em mock, mas não deve quebrar
        self.assertEqual(decision.mode, AdapterMode.MOCK)

    def test_never_claims_real_execution_without_config(self):
        req = SceneRequirements(scene_type="cinematic")
        result = self.router.generate_scene(req)
        self.assertEqual(result.mode, AdapterMode.MOCK)
        self.assertIn("nenhuma chamada real", result.data.get("note", "").lower())


if __name__ == "__main__":
    unittest.main()
