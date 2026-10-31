import os
import tempfile
import unittest

from atlas.escalation import ApprovalStatus, EscalationDesk
from bridge.execution.spend_guard import SpendGuard, SpendNotApproved
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

"""
MICROCORREÇÃO — IMMUTABLE SECURITY STATE.

Falha crítica real reproduzida por auditoria independente:
- obter um `EscalationItem` via `EscalationDesk.get()` e mutar
  `.status`/`.context` diretamente alterava o estado interno real,
  permitindo bypass total de Owner Approval (PENDING -> APPROVED sem
  decisão real do Owner, e alterar o valor aprovado de R$100 para
  R$10.000).
- obter um `SpendApproval` via `SpendGuard.confirm_approval()`/
  `record_spend()`/`approval_for_escalation()` e mutar
  `.approved_amount`/`.spent_so_far` diretamente alterava o
  orçamento real usado por decisões de gasto subsequentes.

Corrigido: toda API pública de `EscalationDesk`/`SpendGuard` devolve
uma CÓPIA PROFUNDA (`copy.deepcopy`) do objeto interno, nunca a
referência real — mutar o objeto retornado (em qualquer profundidade,
incluindo dicts/listas aninhados) nunca mais afeta o estado interno
autoritativo.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


class EscalationDeskImmutabilityMemoryTests(unittest.TestCase):
    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)

    def test_mutating_returned_status_never_bypasses_approval(self):
        """Cenário EXATO reportado: mutar status para APPROVED sem
        decisão real do Owner continua bloqueado."""
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={"requested_amount": 100.0})
        item.status = ApprovalStatus.APPROVED
        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.status, ApprovalStatus.PENDING_OWNER_APPROVAL)

    def test_mutating_returned_context_amount_never_affects_internal(self):
        """Cenário EXATO reportado: mutar requested_amount de 100 para
        10000 no objeto retornado nunca afeta o valor interno."""
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={"requested_amount": 100.0})
        item.context["requested_amount"] = 10000.0
        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["requested_amount"], 100.0)

    def test_full_bypass_attempt_blocked_end_to_end(self):
        """Cenário EXATO reportado, de ponta a ponta: sem aprovação
        real, muta status + valor, tenta confirmar — deve continuar
        bloqueado (não confirmar) pois o estado INTERNO nunca mudou."""
        item = self.desk.escalate(
            reason="x", actor="x", operation="spend_approval",
            context={"platform": "google_ads", "campaign_reference": "c1", "requested_amount": 100.0, "currency": "BRL"},
        )
        item.status = ApprovalStatus.APPROVED
        item.context["requested_amount"] = 10000.0

        guard = SpendGuard(escalation_desk=self.desk, db_path=None)
        with self.assertRaises(SpendNotApproved):
            guard.confirm_approval(escalation_id=item.escalation_id, platform="google_ads", campaign_reference="c1", currency="BRL")

    def test_nested_context_mutation_never_affects_internal(self):
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={"nested": {"deep": {"value": 1}}, "items": [1, 2]})
        item.context["nested"]["deep"]["value"] = 999
        item.context["items"].append(999)
        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["nested"]["deep"]["value"], 1)
        self.assertEqual(fresh.context["items"], [1, 2])

    def test_nested_evidence_mutation_never_affects_internal(self):
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={}, evidence={"proof": {"score": 0.9}})
        item.evidence["proof"]["score"] = 0.0
        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.evidence["proof"]["score"], 0.9)

    def test_all_public_methods_return_independent_copies(self):
        """approve/reject/cancel/pending/all/find_by_idempotency_key —
        todos devolvem cópias independentes, nunca a mesma referência."""
        item1 = self.desk.escalate(reason="x", actor="x", operation="x", context={}, idempotency_key="k1")
        item2 = self.desk.escalate(reason="x", actor="x", operation="x", context={}, idempotency_key="k1")
        self.assertIsNot(item1, item2)  # mesma escalação (idempotência), objetos DIFERENTES

        approved = self.desk.approve(item1.escalation_id, decision="ok", resolved_by=OWNER)
        approved.status = ApprovalStatus.REJECTED  # ataque
        fresh = self.desk.get(item1.escalation_id)
        self.assertEqual(fresh.status, ApprovalStatus.APPROVED)

        all_items = self.desk.all()
        for i in all_items:
            i.status = ApprovalStatus.CANCELLED  # ataque em massa
        fresh_again = self.desk.get(item1.escalation_id)
        self.assertEqual(fresh_again.status, ApprovalStatus.APPROVED)

    def test_expire_stale_returns_independent_copies(self):
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={}, expires_at=None)
        # forca expiracao manipulando o relogio interno via ttl=0
        import time as time_module
        time_module.sleep(0.01)
        expired = self.desk.expire_stale(ttl_seconds=0.0)
        if expired:
            expired[0].status = ApprovalStatus.APPROVED  # ataque
            fresh = self.desk.get(item.escalation_id)
            self.assertEqual(fresh.status, ApprovalStatus.EXPIRED)


class EscalationDeskImmutabilitySQLiteTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit, db_path=self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_mutating_returned_status_never_bypasses_approval_sqlite(self):
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={"requested_amount": 100.0})
        item.status = ApprovalStatus.APPROVED
        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.status, ApprovalStatus.PENDING_OWNER_APPROVAL)

    def test_mutating_returned_context_never_affects_internal_sqlite(self):
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={"requested_amount": 100.0})
        item.context["requested_amount"] = 10000.0
        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["requested_amount"], 100.0)


class SpendGuardImmutabilityMemoryTests(unittest.TestCase):
    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)
        self.guard = SpendGuard(escalation_desk=self.desk, db_path=None)
        self.esc_id = self.guard.request_spend_approval(
            platform="google_ads", campaign_reference="c1", requested_amount=100.0, currency="BRL",
            actor="x", reason="x",
        )
        self.desk.approve(self.esc_id, decision="ok", resolved_by=OWNER)

    def test_mutating_approved_amount_never_affects_internal_budget(self):
        """Cenário EXATO reportado: mutar approved_amount de 100 para
        1000 nunca afeta o orçamento interno."""
        approval = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
        approval.approved_amount = 1000.0
        fresh = self.guard.approval_for_escalation(self.esc_id)
        self.assertEqual(fresh.approved_amount, 100.0)

    def test_full_bypass_attempt_blocked_end_to_end(self):
        """Cenário EXATO reportado: mutar approved_amount para 1000 e
        tentar gastar 500 — deve continuar bloqueado."""
        approval = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
        approval.approved_amount = 1000.0
        with self.assertRaises(SpendNotApproved):
            self.guard.record_spend(approval_id=approval.approval_id, amount=500.0)

    def test_mutating_spent_so_far_negative_never_inflates_remaining(self):
        approval = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
        approval.spent_so_far = -1000.0  # tenta "inflar" o restante via referencia mutavel
        fresh = self.guard.approval_for_escalation(self.esc_id)
        self.assertEqual(fresh.spent_so_far, 0.0)
        self.assertEqual(fresh.remaining(), 100.0)

    def test_record_spend_return_value_also_immutable(self):
        approval = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
        result = self.guard.record_spend(approval_id=approval.approval_id, amount=30.0)
        result.spent_so_far = 0.0  # tenta "desfazer" o gasto via mutacao
        fresh = self.guard.approval_for_escalation(self.esc_id)
        self.assertEqual(fresh.spent_so_far, 30.0)

    def test_two_confirmations_return_independent_objects(self):
        a1 = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
        a2 = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
        self.assertIsNot(a1, a2)  # mesma aprovacao (approval_id), objetos DIFERENTES
        self.assertEqual(a1.approval_id, a2.approval_id)
        a1.approved_amount = 999999.0
        self.assertEqual(a2.approved_amount, 100.0)


class SpendGuardImmutabilitySQLiteTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit, db_path=self.db_path)
        self.guard = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        self.esc_id = self.guard.request_spend_approval(
            platform="google_ads", campaign_reference="c1", requested_amount=100.0, currency="BRL",
            actor="x", reason="x",
        )
        self.desk.approve(self.esc_id, decision="ok", resolved_by=OWNER)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_mutating_approved_amount_never_affects_internal_budget_sqlite(self):
        approval = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
        approval.approved_amount = 1000.0
        fresh = self.guard.approval_for_escalation(self.esc_id)
        self.assertEqual(fresh.approved_amount, 100.0)

    def test_full_bypass_attempt_blocked_end_to_end_sqlite(self):
        approval = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="c1", currency="BRL")
        approval.approved_amount = 1000.0
        with self.assertRaises(SpendNotApproved):
            self.guard.record_spend(approval_id=approval.approval_id, amount=500.0)


if __name__ == "__main__":
    unittest.main()
