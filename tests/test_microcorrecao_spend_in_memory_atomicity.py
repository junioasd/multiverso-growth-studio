import threading
import unittest

from atlas.escalation import EscalationDesk
from bridge.execution.spend_guard import SpendGuard, SpendNotApproved
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

"""
MICROCORREÇÃO — SPEND IN-MEMORY ATOMICITY.

Falha real reproduzida por auditoria independente: `SpendGuard` com
`db_path=None`, sob concorrência de threads na MESMA instância, podia
criar múltiplos `approval_id` para uma única escalação aprovada (1
aprovação de R$100 gerou 3 `approval_id` e `total_approved_budget`
de R$300).

Causa raiz: `confirm_approval()` fazia "criar aprovação" (dentro de
uma aquisição de lock, em `_confirm_approval_memory`) e "registrar em
`_approvals`/`_escalation_to_approval`" (em OUTRA aquisição de lock
separada, no método chamador) como dois passos NÃO atômicos entre si
— a janela entre eles permitia múltiplas threads passarem pela
checagem "ainda não existe" antes de qualquer uma registrar.

Corrigido: para o caminho em memória, checar+criar+registrar em
AMBOS os dicionários agora acontece inteiramente dentro de UMA única
aquisição de lock, sem nenhuma janela.

Estes testes rodam a mesma verificação MÚLTIPLAS VEZES (repetição
explícita) para reduzir a chance de uma correção "por sorte de
timing" passar sem genuinamente eliminar a janela de corrida.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
_REPEAT_ROUNDS = 15


class SpendInMemoryAtomicityTests(unittest.TestCase):
    def _fresh_guard_with_approved_escalation(self, *, amount: float = 100.0):
        audit = AuditTrail()
        desk = EscalationDesk(audit)
        guard = SpendGuard(escalation_desk=desk, db_path=None)
        esc_id = guard.request_spend_approval(
            platform="google_ads", campaign_reference="camp1", requested_amount=amount, currency="BRL",
            actor="teste", reason="teste",
        )
        desk.approve(esc_id, decision="ok", resolved_by=OWNER)
        return guard, esc_id

    def _confirm_concurrently(self, *, guard, escalation_id, n_threads: int) -> list:
        approval_ids = []

        def worker():
            approval = guard.confirm_approval(
                escalation_id=escalation_id, platform="google_ads", campaign_reference="camp1", currency="BRL",
            )
            approval_ids.append(approval.approval_id)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return approval_ids

    def test_two_threads_always_single_approval_repeated(self):
        for _ in range(_REPEAT_ROUNDS):
            guard, esc_id = self._fresh_guard_with_approved_escalation()
            approval_ids = self._confirm_concurrently(guard=guard, escalation_id=esc_id, n_threads=2)
            self.assertEqual(len(set(approval_ids)), 1)
            self.assertEqual(guard.total_approved_budget(), 100.0)

    def test_ten_threads_always_single_approval_repeated(self):
        for _ in range(_REPEAT_ROUNDS):
            guard, esc_id = self._fresh_guard_with_approved_escalation()
            approval_ids = self._confirm_concurrently(guard=guard, escalation_id=esc_id, n_threads=10)
            self.assertEqual(len(set(approval_ids)), 1)
            self.assertEqual(guard.total_approved_budget(), 100.0)

    def test_fifty_threads_always_single_approval_repeated(self):
        for _ in range(_REPEAT_ROUNDS):
            guard, esc_id = self._fresh_guard_with_approved_escalation()
            approval_ids = self._confirm_concurrently(guard=guard, escalation_id=esc_id, n_threads=50)
            self.assertEqual(len(set(approval_ids)), 1)
            self.assertEqual(guard.total_approved_budget(), 100.0)

    def test_hundred_threads_always_single_approval_repeated(self):
        for _ in range(_REPEAT_ROUNDS):
            guard, esc_id = self._fresh_guard_with_approved_escalation()
            approval_ids = self._confirm_concurrently(guard=guard, escalation_id=esc_id, n_threads=100)
            self.assertEqual(len(set(approval_ids)), 1)
            self.assertEqual(guard.total_approved_budget(), 100.0)

    def test_concurrent_spend_never_exceeds_approved_budget_in_memory(self):
        """30 threads tentando gastar R$10 cada (R$300 no total) sobre
        uma aprovação de R$100 — repetido várias vezes; o total gasto
        NUNCA pode ultrapassar R$100."""
        for _ in range(_REPEAT_ROUNDS):
            guard, esc_id = self._fresh_guard_with_approved_escalation(amount=100.0)
            approval = guard.confirm_approval(
                escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL",
            )

            successes = []

            def worker():
                try:
                    guard.record_spend(approval_id=approval.approval_id, amount=10.0)
                    successes.append(1)
                except SpendNotApproved:
                    pass

            threads = [threading.Thread(target=worker) for _ in range(30)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            final = guard.approval_for_escalation(esc_id)
            self.assertLessEqual(final.spent_so_far, 100.0)
            self.assertEqual(len(successes), 10)  # exatamente 10 * 10 = 100
            self.assertEqual(final.spent_so_far, 100.0)

    def test_concurrent_80_plus_80_in_memory_never_exceeds_limit(self):
        """Mesmo cenário do teste cross-instance (R$80 + R$80 sobre
        R$100), agora dentro de UMA única instância em memória."""
        for _ in range(_REPEAT_ROUNDS):
            guard, esc_id = self._fresh_guard_with_approved_escalation(amount=100.0)
            approval = guard.confirm_approval(
                escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL",
            )

            outcomes = {"success": [], "rejected": []}

            def spend(amount):
                try:
                    guard.record_spend(approval_id=approval.approval_id, amount=amount)
                    outcomes["success"].append(amount)
                except SpendNotApproved:
                    outcomes["rejected"].append(amount)

            t1 = threading.Thread(target=spend, args=(80.0,))
            t2 = threading.Thread(target=spend, args=(80.0,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(len(outcomes["success"]), 1)
            self.assertEqual(len(outcomes["rejected"]), 1)
            final = guard.approval_for_escalation(esc_id)
            self.assertEqual(final.spent_so_far, 80.0)


if __name__ == "__main__":
    unittest.main()
