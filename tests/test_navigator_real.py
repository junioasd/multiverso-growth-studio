import json
import unittest
from unittest.mock import MagicMock, patch

from atlas.escalation import EscalationDesk
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.execution.platforms import YouTubeAdapter
from bridge.execution.vault import TokenType, TokenVault
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

"""
Testes BLOCO FINAL 2 (item 2 — NAVIGATOR REAL): dispatch por
prioridade real (API oficial > navegador > mock fallback), aprovação
obrigatória do Owner para ações críticas, MOCK_FALLBACK nunca
apresentado como sucesso real — sempre com rede mockada.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


def _fake_response(payload: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.status = 200
    response.__enter__ = lambda self: response
    response.__exit__ = lambda self, *a: None
    return response


class NavigatorRealDispatchTests(unittest.TestCase):
    def setUp(self):
        self.audit = AuditTrail()
        self.hub = BridgeHub()
        self.desk = EscalationDesk(self.audit)
        self.vault = TokenVault()
        ref = self.vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
        self.yt_adapter = YouTubeAdapter(vault=self.vault, account_reference_id=ref)
        self.nav = NavigatorReal(
            bridge_hub=self.hub, escalation_desk=self.desk, adapters_by_platform={"youtube": self.yt_adapter},
            audit=self.audit,
        )

    def test_no_connector_reports_unavailable(self):
        result = self.nav.execute_action(
            platform="youtube", action="GET_CHANNEL_INFO", criticality=ActionCriticality.ROUTINE,
            params={"channel_id": "c1"}, actor="teste", adapter_method="get_channel_info",
        )
        self.assertFalse(result.success)

    def test_official_api_used_when_viable(self):
        self.hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True,
            health=HealthStatus.OK, permissions_ok=True,
        )
        with patch("urllib.request.urlopen", return_value=_fake_response({"items": []})):
            result = self.nav.execute_action(
                platform="youtube", action="GET_CHANNEL_INFO", criticality=ActionCriticality.ROUTINE,
                params={"channel_id": "c1"}, actor="teste", adapter_method="get_channel_info",
            )
        self.assertTrue(result.success)
        self.assertEqual(result.mechanism_used, "OFFICIAL_API")

    def test_mock_fallback_never_reported_as_success(self):
        hub2 = BridgeHub()
        hub2.register_connector(platform="instagram", connector_type=ConnectorType.MOCK_FALLBACK, is_configured=True)
        nav2 = NavigatorReal(bridge_hub=hub2, escalation_desk=self.desk, audit=self.audit)
        # MICROCORREÇÃO BF2: usa uma ação genuinamente ROUTINE (não
        # "POST", que o PolicyActionRegistry agora classifica
        # corretamente como CRITICAL) — este teste verifica o
        # comportamento de MOCK_FALLBACK no dispatch, não a
        # criticidade em si (coberta em outro arquivo de teste).
        result = nav2.execute_action(
            platform="instagram", action="GET_FEED", criticality=ActionCriticality.ROUTINE, params={}, actor="teste",
        )
        self.assertFalse(result.success)
        self.assertTrue(result.is_mock_fallback)


class NavigatorRealOwnerApprovalTests(unittest.TestCase):
    def setUp(self):
        self.audit = AuditTrail()
        self.hub = BridgeHub()
        self.desk = EscalationDesk(self.audit)
        self.hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True,
            health=HealthStatus.OK, permissions_ok=True,
        )
        self.vault = TokenVault()
        ref = self.vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
        self.yt_adapter = YouTubeAdapter(vault=self.vault, account_reference_id=ref)
        self.nav = NavigatorReal(
            bridge_hub=self.hub, escalation_desk=self.desk, adapters_by_platform={"youtube": self.yt_adapter},
            audit=self.audit,
        )

    def test_critical_action_blocked_without_approval(self):
        result = self.nav.execute_action(
            platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL, params={}, actor="teste",
        )
        self.assertFalse(result.success)
        self.assertIsNotNone(result.escalation_id)

    def test_critical_action_proceeds_after_approval(self):
        blocked = self.nav.execute_action(
            platform="youtube", action="GET_CHANNEL_INFO", criticality=ActionCriticality.CRITICAL,
            params={"channel_id": "c1"}, actor="teste", adapter_method="get_channel_info",
        )
        self.desk.approve(blocked.escalation_id, decision="aprovado", resolved_by=OWNER)
        with patch("urllib.request.urlopen", return_value=_fake_response({"items": []})):
            result = self.nav.execute_action(
                platform="youtube", action="GET_CHANNEL_INFO", criticality=ActionCriticality.CRITICAL,
                params={"channel_id": "c1"}, actor="teste", adapter_method="get_channel_info",
                approved_escalation_id=blocked.escalation_id,
            )
        self.assertTrue(result.success)

    def test_rejected_escalation_never_proceeds(self):
        blocked = self.nav.execute_action(
            platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL, params={}, actor="teste",
        )
        self.desk.reject(blocked.escalation_id, decision="rejeitado", resolved_by=OWNER)
        result = self.nav.execute_action(
            platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL, params={},
            actor="teste", approved_escalation_id=blocked.escalation_id,
        )
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
