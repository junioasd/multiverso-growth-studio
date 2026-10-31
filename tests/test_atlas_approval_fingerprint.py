import os
import tempfile
import threading
import unittest

from atlas.escalation import EscalationDesk
from core.governance.idempotency import IdempotencyConflict
from core.models import db as db_module
from core.security.audit import AuditTrail

"""
Testes ATLAS+RADAR - CORRECAO RESIDUAL 2 (FINGERPRINT DAS APROVACOES
CRITICAS DO OWNER).
"""


class InMemoryAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.desk = EscalationDesk(AuditTrail())

    def test_spend_money_amount_change_same_key_conflicts(self):
        self.desk.escalate(
            reason="gasto em anúncios", context={"amount": 100}, actor="atlas@system",
            operation="spend_money", idempotency_key="k1",
        )
        with self.assertRaises(IdempotencyConflict):
            self.desk.escalate(
                reason="gasto em anúncios", context={"amount": 9999}, actor="atlas@system",
                operation="spend_money", idempotency_key="k1",
            )

    def test_context_change_conflicts(self):
        self.desk.escalate(
            reason="r", context={"vendor": "A"}, actor="atlas@system", operation="contract_service",
            idempotency_key="k2",
        )
        with self.assertRaises(IdempotencyConflict):
            self.desk.escalate(
                reason="r", context={"vendor": "B"}, actor="atlas@system", operation="contract_service",
                idempotency_key="k2",
            )

    def test_context_extra_key_conflicts(self):
        self.desk.escalate(
            reason="r", context={"amount": 100}, actor="atlas@system", operation="spend_money",
            idempotency_key="k3",
        )
        with self.assertRaises(IdempotencyConflict):
            self.desk.escalate(
                reason="r", context={"amount": 100, "currency": "USD"}, actor="atlas@system",
                operation="spend_money", idempotency_key="k3",
            )

    def test_evidence_change_conflicts(self):
        self.desk.escalate(
            reason="r", context={}, actor="atlas@system", operation="publish_content",
            evidence={"views": "UNKNOWN"}, idempotency_key="k4",
        )
        with self.assertRaises(IdempotencyConflict):
            self.desk.escalate(
                reason="r", context={}, actor="atlas@system", operation="publish_content",
                evidence={"views": 1000}, idempotency_key="k4",
            )

    def test_risk_change_conflicts(self):
        self.desk.escalate(
            reason="r", context={}, actor="atlas@system", operation="spend_money", risk="LOW",
            idempotency_key="k5",
        )
        with self.assertRaises(IdempotencyConflict):
            self.desk.escalate(
                reason="r", context={}, actor="atlas@system", operation="spend_money", risk="HIGH",
                idempotency_key="k5",
            )

    def test_reason_change_conflicts(self):
        self.desk.escalate(
            reason="motivo A", context={}, actor="atlas@system", operation="spend_money",
            idempotency_key="k6",
        )
        with self.assertRaises(IdempotencyConflict):
            self.desk.escalate(
                reason="motivo B completamente diferente", context={}, actor="atlas@system",
                operation="spend_money", idempotency_key="k6",
            )

    def test_expires_at_change_conflicts(self):
        self.desk.escalate(
            reason="r", context={}, actor="atlas@system", operation="spend_money",
            expires_at=1000.0, idempotency_key="k7",
        )
        with self.assertRaises(IdempotencyConflict):
            self.desk.escalate(
                reason="r", context={}, actor="atlas@system", operation="spend_money",
                expires_at=2000.0, idempotency_key="k7",
            )

    def test_equivalent_dict_different_key_order_same_request(self):
        first = self.desk.escalate(
            reason="r", context={"a": 1, "b": 2, "c": 3}, actor="atlas@system", operation="op",
            idempotency_key="k8",
        )
        second = self.desk.escalate(
            reason="r", context={"c": 3, "a": 1, "b": 2}, actor="atlas@system", operation="op",
            idempotency_key="k8",
        )
        self.assertEqual(first.escalation_id, second.escalation_id)

    def test_equivalent_nested_dict_different_key_order_same_request(self):
        first = self.desk.escalate(
            reason="r", context={"details": {"x": 1, "y": 2}}, actor="atlas@system", operation="op",
            idempotency_key="k9",
        )
        second = self.desk.escalate(
            reason="r", context={"details": {"y": 2, "x": 1}}, actor="atlas@system", operation="op",
            idempotency_key="k9",
        )
        self.assertEqual(first.escalation_id, second.escalation_id)

    def test_fully_identical_request_returns_same_no_conflict(self):
        first = self.desk.escalate(
            reason="r", context={"amount": 500}, actor="atlas@system", operation="spend_money",
            risk="HIGH", evidence={"x": 1}, idempotency_key="k10",
        )
        second = self.desk.escalate(
            reason="r", context={"amount": 500}, actor="atlas@system", operation="spend_money",
            risk="HIGH", evidence={"x": 1}, idempotency_key="k10",
        )
        self.assertEqual(first.escalation_id, second.escalation_id)

    def test_different_idempotency_key_never_conflicts_even_with_different_content(self):
        self.desk.escalate(
            reason="r", context={"amount": 100}, actor="atlas@system", operation="spend_money",
            idempotency_key="k11a",
        )
        second = self.desk.escalate(
            reason="r", context={"amount": 9999}, actor="atlas@system", operation="spend_money",
            idempotency_key="k11b",
        )
        self.assertIsNotNone(second.escalation_id)


class PersistenceTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)


class PersistedAdversarialTests(PersistenceTestBase):
    def test_amount_change_conflicts_persisted(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        desk1.escalate(
            reason="gasto", context={"amount": 100}, actor="atlas@system", operation="spend_money",
            idempotency_key="k1",
        )
        with self.assertRaises(IdempotencyConflict):
            desk1.escalate(
                reason="gasto", context={"amount": 9999}, actor="atlas@system", operation="spend_money",
                idempotency_key="k1",
            )

    def test_behavior_maintained_after_restart(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        desk1.escalate(
            reason="gasto", context={"amount": 100}, actor="atlas@system", operation="spend_money",
            idempotency_key="k1",
        )

        desk2 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        with self.assertRaises(IdempotencyConflict):
            desk2.escalate(
                reason="gasto", context={"amount": 9999}, actor="atlas@system", operation="spend_money",
                idempotency_key="k1",
            )

    def test_equivalent_content_after_restart_returns_same(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        first = desk1.escalate(
            reason="r", context={"a": 1, "b": 2}, actor="atlas@system", operation="op",
            idempotency_key="k2",
        )

        desk2 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        second = desk2.escalate(
            reason="r", context={"b": 2, "a": 1}, actor="atlas@system", operation="op",
            idempotency_key="k2",
        )
        self.assertEqual(first.escalation_id, second.escalation_id)

        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM owner_approval_requests").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_evidence_change_conflicts_after_restart(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        desk1.escalate(
            reason="r", context={}, actor="atlas@system", operation="publish_content",
            evidence={"views": 100}, idempotency_key="k3",
        )
        desk2 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        with self.assertRaises(IdempotencyConflict):
            desk2.escalate(
                reason="r", context={}, actor="atlas@system", operation="publish_content",
                evidence={"views": 999999}, idempotency_key="k3",
            )

    def test_concurrent_conflicting_requests_only_one_persists_others_conflict(self):
        desk_setup = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        desk_setup.escalate(
            reason="r", context={"amount": 1}, actor="atlas@system", operation="spend_money",
            idempotency_key="race-key",
        )

        results = {}
        barrier = threading.Barrier(3)

        def attempt(name, amount):
            desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
            barrier.wait()
            try:
                item = desk.escalate(
                    reason="r", context={"amount": amount}, actor="atlas@system", operation="spend_money",
                    idempotency_key="race-key",
                )
                results[name] = ("ok", item.escalation_id)
            except IdempotencyConflict:
                results[name] = ("conflict", None)

        threads = [
            threading.Thread(target=attempt, args=("same", 1)),
            threading.Thread(target=attempt, args=("diff1", 2)),
            threading.Thread(target=attempt, args=("diff2", 3)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results["same"][0], "ok")
        self.assertEqual(results["diff1"][0], "conflict")
        self.assertEqual(results["diff2"][0], "conflict")

        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM owner_approval_requests").fetchone()["n"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
