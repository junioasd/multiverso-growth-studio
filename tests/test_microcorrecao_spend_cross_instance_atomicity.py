import os
import tempfile
import threading
import unittest

from atlas.escalation import EscalationDesk
from bridge.execution.spend_guard import SpendGuard, SpendNotApproved
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

"""
MICROCORREÇÃO — SPEND CROSS-INSTANCE ATOMICITY.

Falha real confirmada pela auditoria: duas instâncias DIFERENTES de
`SpendGuard`, compartilhando o MESMO SQLite, confirmaram
simultaneamente a mesma `escalation_id` e criaram DOIS `approval_id`
diferentes — cada um autorizou gasto independentemente, resultando em
R$160 de gasto efetivo sobre uma aprovação de R$100.

Testes aqui usam MÚLTIPLAS INSTÂNCIAS REAIS de `SpendGuard` (nunca a
mesma instância reaproveitada) compartilhando o mesmo `db_path` —
exatamente o cenário que expôs a falha original.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


class SpendCrossInstanceAtomicityTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit, db_path=self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _approved_escalation(self, *, amount: float = 100.0) -> str:
        setup_guard = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        esc_id = setup_guard.request_spend_approval(
            platform="google_ads", campaign_reference="camp1", requested_amount=amount, currency="BRL",
            actor="teste", reason="teste",
        )
        self.desk.approve(esc_id, decision="ok", resolved_by=OWNER)
        return esc_id

    def _confirm_with_n_instances(self, *, escalation_id: str, n: int) -> list:
        instances = [SpendGuard(escalation_desk=self.desk, db_path=self.db_path) for _ in range(n)]
        approval_ids = []

        def worker(instance):
            approval = instance.confirm_approval(
                escalation_id=escalation_id, platform="google_ads", campaign_reference="camp1", currency="BRL",
            )
            approval_ids.append(approval.approval_id)

        threads = [threading.Thread(target=worker, args=(inst,)) for inst in instances]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return approval_ids

    def test_two_instances_converge_to_single_approval(self):
        esc_id = self._approved_escalation()
        approval_ids = self._confirm_with_n_instances(escalation_id=esc_id, n=2)
        self.assertEqual(len(set(approval_ids)), 1)

    def test_ten_instances_converge_to_single_approval(self):
        esc_id = self._approved_escalation()
        approval_ids = self._confirm_with_n_instances(escalation_id=esc_id, n=10)
        self.assertEqual(len(set(approval_ids)), 1)

    def test_fifty_instances_converge_to_single_approval(self):
        esc_id = self._approved_escalation()
        approval_ids = self._confirm_with_n_instances(escalation_id=esc_id, n=50)
        self.assertEqual(len(set(approval_ids)), 1)

    def test_budget_never_duplicates_across_instances(self):
        esc_id = self._approved_escalation(amount=100.0)
        self._confirm_with_n_instances(escalation_id=esc_id, n=20)
        verifier = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        self.assertEqual(verifier.total_approved_budget(), 100.0)

    def test_two_independent_instances_same_db(self):
        """Cenário exato reportado: duas instâncias independentes no
        mesmo DB."""
        esc_id = self._approved_escalation(amount=100.0)
        guard_a = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        guard_b = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)

        results = {}

        def confirm(name, guard):
            results[name] = guard.confirm_approval(
                escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL",
            )

        t1 = threading.Thread(target=confirm, args=("a", guard_a))
        t2 = threading.Thread(target=confirm, args=("b", guard_b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results["a"].approval_id, results["b"].approval_id)

    def test_concurrent_80_plus_80_spend_over_100_never_exceeds_limit(self):
        """Cenário exato reportado: gasto concorrente de 80 + 80 sobre
        aprovação de 100 — somente uma operação pode ter sucesso
        completo; total persistido nunca ultrapassa 100."""
        esc_id = self._approved_escalation(amount=100.0)
        guard_a = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        guard_b = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)

        approval_a = guard_a.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        approval_b = guard_b.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        self.assertEqual(approval_a.approval_id, approval_b.approval_id)

        outcomes = {"success": [], "rejected": []}

        def spend(guard, amount):
            try:
                guard.record_spend(approval_id=approval_a.approval_id, amount=amount)
                outcomes["success"].append(amount)
            except SpendNotApproved:
                outcomes["rejected"].append(amount)

        t1 = threading.Thread(target=spend, args=(guard_a, 80.0))
        t2 = threading.Thread(target=spend, args=(guard_b, 80.0))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(outcomes["success"]), 1)
        self.assertEqual(len(outcomes["rejected"]), 1)
        verifier = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        final = verifier.approval_for_escalation(esc_id)
        self.assertLessEqual(final.spent_so_far, 100.0)
        self.assertEqual(final.spent_so_far, 80.0)

    def test_heavy_concurrent_spend_across_many_instances_never_exceeds_limit(self):
        """30 tentativas de gasto de R$10 cada (R$300 no total),
        vindas de 30 instâncias DIFERENTES, sobre uma aprovação de
        R$100 — o total persistido NUNCA pode ultrapassar R$100."""
        esc_id = self._approved_escalation(amount=100.0)
        setup = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        approval = setup.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")

        instances = [SpendGuard(escalation_desk=self.desk, db_path=self.db_path) for _ in range(30)]
        outcomes = {"success": [], "rejected": []}

        def worker(instance):
            try:
                instance.record_spend(approval_id=approval.approval_id, amount=10.0)
                outcomes["success"].append(1)
            except SpendNotApproved:
                outcomes["rejected"].append(1)

        threads = [threading.Thread(target=worker, args=(inst,)) for inst in instances]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(outcomes["success"]), 10)  # exatamente 10 * 10 = 100
        verifier = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        final = verifier.approval_for_escalation(esc_id)
        self.assertEqual(final.spent_so_far, 100.0)

    def test_restart_preserves_approval_id_and_balance(self):
        esc_id = self._approved_escalation(amount=100.0)
        guard1 = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        approval1 = guard1.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        guard1.record_spend(approval_id=approval1.approval_id, amount=45.0)

        # restart -- nova instancia, mesmo db_path
        guard2 = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        approval2 = guard2.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")

        self.assertEqual(approval1.approval_id, approval2.approval_id)
        self.assertEqual(approval2.spent_so_far, 45.0)
        self.assertEqual(guard2.total_approved_budget(), 100.0)


if __name__ == "__main__":
    unittest.main()
