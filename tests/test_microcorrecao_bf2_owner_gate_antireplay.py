import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from atlas.escalation import EscalationDesk
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.execution.policy_registry import PolicyActionRegistry
from bridge.execution.spend_guard import SpendGuard
from bridge.execution.vault import TokenType, TokenVault
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

"""
MICROCORREÇÃO BF2 — OWNER GATE + ANTI-REPLAY + SPEND SINGLE-USE.
Testes obrigatórios exatamente como especificados pela auditoria.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


def _fake_response(payload: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.status = 200
    response.__enter__ = lambda self: response
    response.__exit__ = lambda self, *a: None
    return response


class _FakeAdapter(PlatformApiAdapter):
    platform = "youtube"
    base_url = "https://api.fake.example/v1"

    def do_something(self, **kwargs):
        return self._request(action="DO_SOMETHING", method="POST", path="/action", body=kwargs)


class PolicyActionRegistryTests(unittest.TestCase):
    """Item 1 — classificação autoritativa, nunca controlável pelo chamador."""

    def setUp(self):
        self.registry = PolicyActionRegistry()

    def test_update_action_always_critical(self):
        self.assertEqual(self.registry.resolve_required_criticality(action="UPDATE_VIDEO_METADATA"), ActionCriticality.CRITICAL)

    def test_publish_action_always_critical(self):
        self.assertEqual(self.registry.resolve_required_criticality(action="PUBLISH_POST"), ActionCriticality.CRITICAL)

    def test_delete_action_always_critical(self):
        self.assertEqual(self.registry.resolve_required_criticality(action="DELETE_VIDEO"), ActionCriticality.CRITICAL)

    def test_spend_action_always_critical(self):
        self.assertEqual(self.registry.resolve_required_criticality(action="SPEND_BUDGET"), ActionCriticality.CRITICAL)

    def test_unknown_action_defaults_to_critical(self):
        self.assertEqual(self.registry.resolve_required_criticality(action="SOME_NEW_ACTION_NEVER_SEEN"), ActionCriticality.CRITICAL)

    def test_known_read_action_is_routine(self):
        self.assertEqual(self.registry.resolve_required_criticality(action="GET_CHANNEL_INFO"), ActionCriticality.ROUTINE)

    def test_caller_cannot_downgrade_critical_action(self):
        effective = self.registry.effective_criticality(action="UPDATE_VIDEO_METADATA", caller_requested=ActionCriticality.ROUTINE)
        self.assertEqual(effective, ActionCriticality.CRITICAL)

    def test_caller_can_escalate_routine_action(self):
        effective = self.registry.effective_criticality(action="GET_CHANNEL_INFO", caller_requested=ActionCriticality.CRITICAL)
        self.assertEqual(effective, ActionCriticality.CRITICAL)


class CriticalityCannotBeDowngradedRegressionTests(unittest.TestCase):
    """Testes obrigatórios exatos pedidos pela auditoria — via
    NavigatorReal.execute_action() completo, não apenas o registro
    isolado."""

    def setUp(self):
        self.audit = AuditTrail()
        self.hub = BridgeHub()
        self.desk = EscalationDesk(self.audit)
        self.nav = NavigatorReal(bridge_hub=self.hub, escalation_desk=self.desk, audit=self.audit)

    def test_update_video_metadata_as_routine_blocked(self):
        result = self.nav.execute_action(
            platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.ROUTINE,
            params={"video_id": "v1"}, actor="teste",
        )
        self.assertFalse(result.success)
        self.assertIsNotNone(result.escalation_id)

    def test_publish_as_routine_blocked(self):
        result = self.nav.execute_action(
            platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.ROUTINE,
            params={}, actor="teste",
        )
        self.assertFalse(result.success)
        self.assertIsNotNone(result.escalation_id)

    def test_delete_as_routine_blocked(self):
        result = self.nav.execute_action(
            platform="youtube", action="DELETE_VIDEO", criticality=ActionCriticality.ROUTINE,
            params={}, actor="teste",
        )
        self.assertFalse(result.success)
        self.assertIsNotNone(result.escalation_id)

    def test_spend_as_routine_blocked(self):
        result = self.nav.execute_action(
            platform="google_ads", action="SPEND_BUDGET", criticality=ActionCriticality.ROUTINE,
            params={}, actor="teste",
        )
        self.assertFalse(result.success)
        self.assertIsNotNone(result.escalation_id)

    def test_genuinely_routine_action_never_blocked(self):
        result = self.nav.execute_action(
            platform="youtube", action="GET_CHANNEL_INFO", criticality=ActionCriticality.ROUTINE,
            params={"channel_id": "c1"}, actor="teste", adapter_method="get_channel_info",
        )
        self.assertIsNone(result.escalation_id)


class SpendApprovalSingleUseRegressionTests(unittest.TestCase):
    """Item 2 — testes obrigatórios exatos."""

    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)
        self.guard = SpendGuard(escalation_desk=self.desk)
        self.esc_id = self.guard.request_spend_approval(
            platform="google_ads", campaign_reference="camp1", requested_amount=100.0, currency="BRL",
            actor="teste", reason="teste",
        )
        self.desk.approve(self.esc_id, decision="ok", resolved_by=OWNER)

    def test_confirm_twice_budget_stays_100(self):
        for _ in range(2):
            self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        self.assertEqual(self.guard.total_approved_budget(), 100.0)

    def test_confirm_ten_times_budget_stays_100(self):
        for _ in range(10):
            self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        self.assertEqual(self.guard.total_approved_budget(), 100.0)

    def test_confirm_hundred_times_budget_stays_100(self):
        for _ in range(100):
            self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        self.assertEqual(self.guard.total_approved_budget(), 100.0)

    def test_concurrent_confirmation_yields_single_approval(self):
        results = []

        def worker():
            a = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
            results.append(a.approval_id)

        threads = [threading.Thread(target=worker) for _ in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.guard.total_approved_budget(), 100.0)

    def test_persists_across_restart_no_duplication(self):
        import os
        import tempfile

        from core.models import db as db_module

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            desk = EscalationDesk(self.audit, db_path=db_path)
            guard1 = SpendGuard(escalation_desk=desk, db_path=db_path)
            esc_id = guard1.request_spend_approval(
                platform="google_ads", campaign_reference="c1", requested_amount=100.0, currency="BRL",
                actor="teste", reason="teste",
            )
            desk.approve(esc_id, decision="ok", resolved_by=OWNER)
            a1 = guard1.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")

            guard2 = SpendGuard(escalation_desk=desk, db_path=db_path)
            a2 = guard2.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
            self.assertEqual(a1.approval_id, a2.approval_id)
            self.assertEqual(guard2.total_approved_budget(), 100.0)
        finally:
            db_module.remove_db_file(db_path)


class OwnerApprovalAntiReplayRegressionTests(unittest.TestCase):
    """Item 3 — testes obrigatórios exatos."""

    def setUp(self):
        self.audit = AuditTrail()
        self.hub = BridgeHub()
        self.hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True,
            health=HealthStatus.OK, permissions_ok=True,
        )
        self.desk = EscalationDesk(self.audit)
        self.vault = TokenVault()
        ref = self.vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
        self.adapter = _FakeAdapter(vault=self.vault, account_reference_id=ref)
        self.nav = NavigatorReal(
            bridge_hub=self.hub, escalation_desk=self.desk, adapters_by_platform={"youtube": self.adapter},
            audit=self.audit,
        )

    def _approve_and_execute_once(self, idempotency_key="idem-1"):
        blocked = self.nav.execute_action(
            platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
            params={"video_id": "v1"}, actor="teste", idempotency_key=idempotency_key,
        )
        self.desk.approve(blocked.escalation_id, decision="ok", resolved_by=OWNER)
        with patch("urllib.request.urlopen", return_value=_fake_response({"ok": True})):
            result = self.nav.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                idempotency_key=idempotency_key, approved_escalation_id=blocked.escalation_id,
            )
        return blocked.escalation_id, result

    def test_reuse_consumed_approval_without_idempotency_blocked(self):
        esc_id, first = self._approve_and_execute_once(idempotency_key="idem-1")
        self.assertTrue(first.success)

        with patch("urllib.request.urlopen", return_value=_fake_response({"ok": True})) as mock_urlopen:
            second = self.nav.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                idempotency_key="idem-DIFERENTE", approved_escalation_id=esc_id,
            )
        self.assertFalse(second.success)
        self.assertEqual(mock_urlopen.call_count, 0)

    def test_retry_with_same_idempotency_key_returns_cache_no_duplicate_call(self):
        esc_id, first = self._approve_and_execute_once(idempotency_key="idem-1")
        with patch("urllib.request.urlopen", return_value=_fake_response({"ok": True})) as mock_urlopen:
            retry = self.nav.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                idempotency_key="idem-1", approved_escalation_id=esc_id,
            )
        self.assertTrue(retry.success)
        self.assertTrue(retry.replayed_from_cache)
        self.assertEqual(mock_urlopen.call_count, 0)

    def test_restart_does_not_allow_replay(self):
        import os
        import tempfile

        from core.models import db as db_module

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            hub = BridgeHub()
            hub.register_connector(
                platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True,
                health=HealthStatus.OK, permissions_ok=True,
            )
            desk = EscalationDesk(self.audit, db_path=db_path)
            vault = TokenVault(db_path=db_path)
            ref = vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
            adapter = _FakeAdapter(vault=vault, account_reference_id=ref)

            nav1 = NavigatorReal(
                bridge_hub=hub, escalation_desk=desk, adapters_by_platform={"youtube": adapter}, db_path=db_path,
                audit=self.audit,
            )
            blocked = nav1.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", idempotency_key="idem-1",
            )
            desk.approve(blocked.escalation_id, decision="ok", resolved_by=OWNER)
            with patch("urllib.request.urlopen", return_value=_fake_response({"ok": True})):
                first = nav1.execute_action(
                    platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                    params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                    idempotency_key="idem-1", approved_escalation_id=blocked.escalation_id,
                )
            self.assertTrue(first.success)

            # restart -- novo processo/instancia, mesmo db_path
            nav2 = NavigatorReal(
                bridge_hub=hub, escalation_desk=desk, adapters_by_platform={"youtube": adapter}, db_path=db_path,
                audit=self.audit,
            )
            with patch("urllib.request.urlopen", return_value=_fake_response({"ok": True})) as mock_after_restart:
                replay = nav2.execute_action(
                    platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                    params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                    idempotency_key="idem-OUTRA", approved_escalation_id=blocked.escalation_id,
                )
            self.assertFalse(replay.success)
            self.assertEqual(mock_after_restart.call_count, 0)
        finally:
            db_module.remove_db_file(db_path)

    def test_failed_execution_never_consumes_approval(self):
        """Uma tentativa que FALHOU (nunca disparou o efeito externo)
        nunca "queima" a aprovação — o Owner aprovou a AÇÃO, não uma
        falha; deve ser possível tentar de novo com a mesma aprovação."""
        blocked = self.nav.execute_action(
            platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
            params={"video_id": "v1"}, actor="teste", idempotency_key="idem-1",
        )
        self.desk.approve(blocked.escalation_id, decision="ok", resolved_by=OWNER)

        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("falha simulada")):
            failed = self.nav.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                idempotency_key="idem-1", approved_escalation_id=blocked.escalation_id,
            )
        self.assertFalse(failed.success)

        # tenta de novo com a MESMA aprovacao -- deve ainda funcionar (nunca consumida por uma falha)
        with patch("urllib.request.urlopen", return_value=_fake_response({"ok": True})):
            retry = self.nav.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                idempotency_key="idem-1", approved_escalation_id=blocked.escalation_id,
            )
        self.assertTrue(retry.success)


if __name__ == "__main__":
    unittest.main()
