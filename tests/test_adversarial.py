import unittest
from tests._auth_test_helpers import fast_hash_password

from commercial.sales_tracking import SalesTracker, UnverifiedAmountRejected
from core.adapters.base import AdapterMode
from core.adapters.platforms.adapters import PublicationRequest, YouTubeAdapter
from core.adapters.voice.engine import CelebrityVoiceDenied, VoiceEngine, VoiceRequest
from core.isolation.isolation import IsolationGuard, IsolationViolation, ResourceScope
from core.jobs.queue import JobQueue
from core.models.entities import ValueType
from core.production.copyright_gate import AssetRights, CopyrightGate, LicenseStatus
from core.security.audit import AuditTrail
from core.security.auth import AuthError, OwnerAuthService
from core.security.rbac import AccessDenied, Permission, Principal, Role, require_permission
from core.security.sanitize import assert_no_secrets, sanitize_dict
from youtube_strategy.strategy_engine import ProhibitedTacticRequested, YouTubeStrategyEngine


class SecretLeakageTests(unittest.TestCase):
    def test_audit_trail_never_stores_raw_password(self):
        audit = AuditTrail()
        event = audit.record(
            actor="attacker",
            action="login_attempt",
            resource="owner_session",
            outcome="DENIED",
            details={"email": "x@y.com", "password": "leak_me_123"},
        )
        self.assertNotIn("leak_me_123", str(event.details))

    def test_sanitize_catches_token_and_api_key(self):
        offenders = assert_no_secrets({"api_key": "sk-123", "token": "abc", "channel_id": "chan_1"})
        self.assertIn("api_key", offenders)
        self.assertIn("token", offenders)
        self.assertNotIn("channel_id", offenders)


class OwnerSpoofTests(unittest.TestCase):
    def test_forged_token_rejected(self):
        service = OwnerAuthService(owner_email="owner@example.com", stored_password_hash=fast_hash_password("realpass123"))
        real_token = service.login("owner@example.com", "realpass123")
        forged = real_token[:-4] + "AAAA"  # adultera a assinatura
        with self.assertRaises(AuthError):
            service.current_session(forged)

    def test_agent_cannot_claim_owner_permissions(self):
        principal = Principal(subject="agent-x", role=Role.AGENT)
        with self.assertRaises(AccessDenied):
            require_permission(principal, Permission.MANAGE_CREDENTIALS)


class CrossScopeAccessTests(unittest.TestCase):
    def test_cross_channel_access_denied(self):
        with self.assertRaises(IsolationViolation):
            IsolationGuard.check({"channel:chan_A"}, ResourceScope(channel_id="chan_B"))

    def test_cross_project_access_denied(self):
        with self.assertRaises(IsolationViolation):
            IsolationGuard.check({"project:proj_A"}, ResourceScope(project_id="proj_B"))


class CopyrightAdversarialTests(unittest.TestCase):
    def test_unknown_copyright_always_blocks(self):
        gate = CopyrightGate()
        result = gate.evaluate([AssetRights("a1", "unknown_source", LicenseStatus.UNKNOWN)])
        self.assertFalse(result.approved)


class UnauthorizedVoiceCloningTests(unittest.TestCase):
    def test_celebrity_voice_without_license_blocked(self):
        engine = VoiceEngine()
        with self.assertRaises(CelebrityVoiceDenied):
            engine.synthesize(VoiceRequest(text="teste", is_celebrity_voice=True))


class FakeMetricsTests(unittest.TestCase):
    def test_fake_sale_without_value_type_rejected(self):
        tracker = SalesTracker()
        with self.assertRaises(UnverifiedAmountRejected):
            tracker.record_event(channel_id="c1", event_type="sale", amount=1_000_000)

    def test_caller_cannot_report_revenue_as_fact_without_type(self):
        tracker = SalesTracker()
        with self.assertRaises(UnverifiedAmountRejected):
            tracker.record_event(channel_id="c1", event_type="checkout", amount=500)


class ProhibitedGrowthTacticsTests(unittest.TestCase):
    def test_fake_engagement_blocked(self):
        engine = YouTubeStrategyEngine()
        with self.assertRaises(ProhibitedTacticRequested):
            engine.request_growth_action("engagement_manipulation")

    def test_captcha_bypass_style_tactic_blocked_as_policy_bypass(self):
        engine = YouTubeStrategyEngine()
        with self.assertRaises(ProhibitedTacticRequested):
            engine.request_growth_action("policy_bypass")

    def test_account_farming_blocked(self):
        engine = YouTubeStrategyEngine()
        with self.assertRaises(ProhibitedTacticRequested):
            engine.request_growth_action("account_farming")

    def test_spam_blocked(self):
        engine = YouTubeStrategyEngine()
        with self.assertRaises(ProhibitedTacticRequested):
            engine.request_growth_action("spam")


class UnauthorizedPublishTests(unittest.TestCase):
    def test_publish_without_configured_credentials_is_mock_not_real(self):
        adapter = YouTubeAdapter()
        request = PublicationRequest(
            channel_id="chan_1", title="teste", description="desc", video_ref="mock://video/1"
        )
        result = adapter.publish(request)
        # Nunca deve afirmar publicação real sem OAuth configurado.
        self.assertEqual(result.mode, AdapterMode.MOCK)
        self.assertTrue(result.success)  # sucesso do MOCK, não de uma publicação real


class DuplicateJobIdempotencyTests(unittest.TestCase):
    def test_duplicate_job_same_key_does_not_create_two_jobs(self):
        queue = JobQueue()
        job1 = queue.enqueue(job_type="produce_video", idempotency_key="dup-key")
        job2 = queue.enqueue(job_type="produce_video", idempotency_key="dup-key")
        self.assertEqual(job1.job_id, job2.job_id)
        self.assertEqual(len(queue.all()), 1)


class MaliciousInputTests(unittest.TestCase):
    def test_sql_like_input_does_not_break_isolation_check(self):
        malicious = "chan_1'; DROP TABLE channels; --"
        with self.assertRaises(IsolationViolation):
            IsolationGuard.check({"channel:chan_1"}, ResourceScope(channel_id=malicious))

    def test_sanitize_handles_non_string_gracefully(self):
        safe = sanitize_dict({"password": None, "count": 5})
        self.assertEqual(safe["count"], 5)


if __name__ == "__main__":
    unittest.main()
