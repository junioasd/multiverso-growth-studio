import unittest

from core.adapters.base import AdapterMode
from core.adapters.voice.engine import CelebrityVoiceDenied, VoiceEngine, VoiceProfile, VoiceRequest


class VoiceEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = VoiceEngine()

    def test_mock_synthesis_when_not_configured(self):
        req = VoiceRequest(text="Olá, este é um teste.", profile=VoiceProfile.DOCUMENTAL)
        result = self.engine.synthesize(req)
        self.assertEqual(result.mode, AdapterMode.MOCK)
        self.assertTrue(result.success)

    def test_all_profiles_accepted(self):
        for profile in VoiceProfile:
            req = VoiceRequest(text="teste", profile=profile)
            result = self.engine.synthesize(req)
            self.assertTrue(result.success)

    def test_celebrity_voice_without_authorization_denied(self):
        req = VoiceRequest(text="teste", is_celebrity_voice=True, celebrity_authorization_ref=None)
        with self.assertRaises(CelebrityVoiceDenied):
            self.engine.synthesize(req)

    def test_celebrity_voice_with_authorization_allowed(self):
        req = VoiceRequest(
            text="teste", is_celebrity_voice=True, celebrity_authorization_ref="license_ref_123"
        )
        result = self.engine.synthesize(req)
        self.assertTrue(result.success)

    def test_quality_assessment_present_in_mock(self):
        req = VoiceRequest(text="teste de qualidade")
        result = self.engine.synthesize(req)
        self.assertIn("assessment", result.data)
        self.assertIn("naturalness_score", result.data["assessment"])


if __name__ == "__main__":
    unittest.main()
