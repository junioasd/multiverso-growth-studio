import unittest

from atlas.escalation import EscalationDesk
from bridge.execution.spend_guard import SpendGuard, SpendNotApproved
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

"""
Testes BLOCO FINAL 2 (item 6 — operação orgânica primeiro): orçamento
zero por padrão, gasto sempre bloqueado sem aprovação explícita do
Owner, nunca ultrapassa o valor aprovado.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


class SpendGuardTests(unittest.TestCase):
    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)
        self.guard = SpendGuard(escalation_desk=self.desk)

    def test_zero_budget_by_default(self):
        self.assertEqual(self.guard.total_approved_budget(), 0.0)

    def test_spend_blocked_without_approval(self):
        esc_id = self.guard.request_spend_approval(
            platform="google_ads", campaign_reference="c1", requested_amount=50.0, currency="BRL",
            actor="ARGOS", reason="teste",
        )
        with self.assertRaises(SpendNotApproved):
            self.guard.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")

    def test_spend_allowed_after_explicit_approval(self):
        esc_id = self.guard.request_spend_approval(
            platform="google_ads", campaign_reference="c1", requested_amount=50.0, currency="BRL",
            actor="ARGOS", reason="teste",
        )
        self.desk.approve(esc_id, decision="ok", resolved_by=OWNER)
        approval = self.guard.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
        self.assertEqual(approval.approved_amount, 50.0)

    def test_rejected_escalation_never_allows_spend(self):
        esc_id = self.guard.request_spend_approval(
            platform="google_ads", campaign_reference="c1", requested_amount=50.0, currency="BRL",
            actor="ARGOS", reason="teste",
        )
        self.desk.reject(esc_id, decision="negado", resolved_by=OWNER)
        with self.assertRaises(SpendNotApproved):
            self.guard.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")

    def test_spend_never_exceeds_approved_amount(self):
        esc_id = self.guard.request_spend_approval(
            platform="google_ads", campaign_reference="c1", requested_amount=100.0, currency="BRL",
            actor="ARGOS", reason="teste",
        )
        self.desk.approve(esc_id, decision="ok", resolved_by=OWNER)
        approval = self.guard.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
        self.guard.record_spend(approval_id=approval.approval_id, amount=100.0)
        with self.assertRaises(SpendNotApproved):
            self.guard.record_spend(approval_id=approval.approval_id, amount=0.01)

    def test_zero_or_negative_request_rejected(self):
        with self.assertRaises(ValueError):
            self.guard.request_spend_approval(
                platform="google_ads", campaign_reference="c1", requested_amount=0.0, currency="BRL",
                actor="ARGOS", reason="teste",
            )

    def test_unknown_approval_id_never_allows_spend(self):
        with self.assertRaises(SpendNotApproved):
            self.guard.record_spend(approval_id="nao_existe", amount=1.0)


if __name__ == "__main__":
    unittest.main()
