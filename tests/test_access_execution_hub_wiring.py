import unittest

from api.bootstrap import build_app
from bridge.execution.navigator_real import ActionCriticality
from core.security.auth import hash_password

"""
Testes BLOCO FINAL 2 — integração completa do Access & Execution Hub
com AtlasDirector/GrowthStudioApp: todos os componentes presentes,
compartilhando instâncias reais, funcionando de ponta a ponta.
"""


class AccessExecutionHubWiringTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app(owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), persistent=False)
        self.atlas = self.app.atlas

    def tearDown(self):
        self.app.shutdown()

    def test_all_hub_components_present(self):
        for attr in (
            "token_vault", "rate_limiter", "browser_executor", "navigator_real", "radar_real",
            "webhook_dispatcher", "spend_guard", "execution_observability",
        ):
            self.assertTrue(hasattr(self.atlas, attr), f"AtlasDirector não tem '{attr}'")

    def test_navigator_shares_same_escalation_desk_as_atlas(self):
        self.assertIs(self.atlas.navigator_real.escalation_desk, self.atlas.escalation_desk)

    def test_navigator_shares_same_bridge_hub(self):
        self.assertIs(self.atlas.navigator_real.bridge_hub, self.atlas.bridge)

    def test_spend_guard_shares_same_escalation_desk(self):
        self.assertIs(self.atlas.spend_guard.escalation_desk, self.atlas.escalation_desk)

    def test_platform_adapters_share_same_vault(self):
        yt_adapter = self.atlas.navigator_real.adapters_by_platform["youtube"]
        self.assertIs(yt_adapter.vault, self.atlas.token_vault)

    def test_critical_action_without_approval_blocked_end_to_end(self):
        result = self.atlas.navigator_real.execute_action(
            platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL,
            params={}, actor="teste",
        )
        self.assertFalse(result.success)
        self.assertIsNotNone(result.escalation_id)

    def test_zero_spend_budget_by_default(self):
        self.assertEqual(self.atlas.spend_guard.total_approved_budget(), 0.0)


if __name__ == "__main__":
    unittest.main()
