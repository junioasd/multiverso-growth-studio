import os
import tempfile
import threading
import unittest

from atlas.escalation import (
    AlreadyDecided,
    ApprovalStatus,
    EscalationDesk,
    EscalationNotFound,
)
from core.governance.idempotency import IdempotencyConflict
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.rbac import AccessDenied, Principal, Role

"""
Testes ATLAS+RADAR - PERSISTENCIA DE APROVACAO DO OWNER, RESTART COM
APROVACAO PENDENTE, IDEMPOTENCIA APOS RESTART, TENTATIVA DE DUPLA
EXECUCAO, APROVACAO, REJEICAO, EXPIRACAO.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
OPERATOR = Principal(subject="op-1", role=Role.OPERATOR, scopes={"*"})


class PersistenceTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)


class InMemoryBackwardCompatibilityTests(unittest.TestCase):
    def test_default_behavior_unchanged(self):
        desk = EscalationDesk(AuditTrail())
        item = desk.escalate(reason="teste", context={}, actor="atlas@system")
        self.assertFalse(item.resolved)
        resolved = desk.resolve(item.escalation_id, resolution="ok", resolved_by=OWNER)
        self.assertTrue(resolved.resolved)


class RestartSurvivalTests(PersistenceTestBase):
    def test_pending_request_survives_restart(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk1.escalate(
            reason="gastar em anuncios", context={"amount": 500}, actor="atlas@system",
            operation="spend_money", risk="HIGH",
        )
        self.assertEqual(item.status, ApprovalStatus.PENDING_OWNER_APPROVAL)

        desk2 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        pending = desk2.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].status, ApprovalStatus.PENDING_OWNER_APPROVAL)

    def test_never_auto_approved_just_because_process_restarted(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk1.escalate(reason="x", context={}, actor="atlas@system")

        for _ in range(5):
            desk_n = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
            current = desk_n.get(item.escalation_id)
            self.assertEqual(current.status, ApprovalStatus.PENDING_OWNER_APPROVAL)

    def test_decision_persists_and_is_visible_from_new_instance(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk1.escalate(reason="x", context={}, actor="atlas@system")
        desk1.approve(item.escalation_id, decision="aprovado", resolved_by=OWNER)

        desk2 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        current = desk2.get(item.escalation_id)
        self.assertEqual(current.status, ApprovalStatus.APPROVED)
        self.assertEqual(current.decided_by, "owner@x.com")

    def test_all_persisted_fields_preserved_across_restart(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk1.escalate(
            reason="motivo detalhado", context={"channel": "chan_1"}, actor="atlas@system",
            operation="publish_content", risk="MEDIUM", evidence={"views": "UNKNOWN"},
        )
        desk2 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        recovered = desk2.get(item.escalation_id)
        self.assertEqual(recovered.operation, "publish_content")
        self.assertEqual(recovered.origin, "atlas@system")
        self.assertEqual(recovered.reason, "motivo detalhado")
        self.assertEqual(recovered.risk, "MEDIUM")
        self.assertEqual(recovered.evidence, {"views": "UNKNOWN"})
        self.assertEqual(recovered.context, {"channel": "chan_1"})
        self.assertEqual(recovered.required_authority, "OWNER")


class IdempotencyAfterRestartTests(PersistenceTestBase):
    def test_same_idempotency_key_after_restart_returns_same_request(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk1.escalate(
            reason="gastar", context={"amount": 100}, actor="atlas@system",
            operation="spend_money", idempotency_key="req-1",
        )

        desk2 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        same = desk2.escalate(
            reason="gastar", context={"amount": 100}, actor="atlas@system",
            operation="spend_money", idempotency_key="req-1",
        )
        self.assertEqual(item.escalation_id, same.escalation_id)

        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM owner_approval_requests").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_conflicting_content_same_key_after_restart_raises_conflict(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        desk1.escalate(
            reason="gastar 100", context={}, actor="atlas@system", operation="spend_money",
            risk="LOW", idempotency_key="req-1",
        )
        desk2 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        with self.assertRaises(IdempotencyConflict):
            desk2.escalate(
                reason="gastar 100", context={}, actor="atlas@system", operation="spend_money",
                risk="HIGH", idempotency_key="req-1",
            )


class DoubleExecutionAttemptTests(PersistenceTestBase):
    def test_retry_same_decision_after_restart_is_idempotent(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk1.escalate(reason="x", context={}, actor="atlas@system")
        desk1.approve(item.escalation_id, decision="ok", resolved_by=OWNER)

        desk2 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        retry = desk2.approve(item.escalation_id, decision="ok", resolved_by=OWNER)
        self.assertEqual(retry.status, ApprovalStatus.APPROVED)

    def test_retry_different_decision_after_restart_raises_already_decided(self):
        desk1 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk1.escalate(reason="x", context={}, actor="atlas@system")
        desk1.approve(item.escalation_id, decision="ok", resolved_by=OWNER)

        desk2 = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        with self.assertRaises(AlreadyDecided):
            desk2.reject(item.escalation_id, decision="mudei de ideia", resolved_by=OWNER)

    def test_concurrent_decision_attempts_only_one_wins(self):
        desk_setup = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk_setup.escalate(reason="disputa", context={}, actor="atlas@system")

        results = {}
        barrier = threading.Barrier(2)

        def attempt(name, decision):
            desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
            barrier.wait()
            try:
                results[name] = desk.approve(item.escalation_id, decision=decision, resolved_by=OWNER).decision
            except AlreadyDecided:
                results[name] = "AlreadyDecided"

        t1 = threading.Thread(target=attempt, args=("A", "decisao-A"))
        t2 = threading.Thread(target=attempt, args=("B", "decisao-B"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        with db_module.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT decision, status FROM owner_approval_requests WHERE request_id=?", (item.escalation_id,)
            ).fetchone()
        self.assertEqual(row["status"], "APPROVED")
        self.assertIn(row["decision"], ("decisao-A", "decisao-B"))


class ApprovalRejectionExpirationTests(PersistenceTestBase):
    def test_approval_flow(self):
        desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk.escalate(reason="x", context={}, actor="atlas@system")
        approved = desk.approve(item.escalation_id, decision="aprovado", resolved_by=OWNER)
        self.assertEqual(approved.status, ApprovalStatus.APPROVED)

    def test_rejection_flow(self):
        desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk.escalate(reason="x", context={}, actor="atlas@system")
        rejected = desk.reject(item.escalation_id, decision="risco alto demais", resolved_by=OWNER)
        self.assertEqual(rejected.status, ApprovalStatus.REJECTED)

    def test_cancellation_flow(self):
        desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk.escalate(reason="x", context={}, actor="atlas@system")
        cancelled = desk.cancel(item.escalation_id, resolved_by=OWNER)
        self.assertEqual(cancelled.status, ApprovalStatus.CANCELLED)

    def test_expiration_flow(self):
        desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk.escalate(reason="x", context={}, actor="atlas@system")
        expired = desk.expire_stale(ttl_seconds=-1)
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].escalation_id, item.escalation_id)
        current = desk.get(item.escalation_id)
        self.assertEqual(current.status, ApprovalStatus.EXPIRED)

    def test_expiration_never_treats_as_approved(self):
        desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk.escalate(reason="x", context={}, actor="atlas@system")
        desk.expire_stale(ttl_seconds=-1)
        current = desk.get(item.escalation_id)
        self.assertNotEqual(current.status, ApprovalStatus.APPROVED)

    def test_fresh_items_not_expired(self):
        desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        desk.escalate(reason="x", context={}, actor="atlas@system")
        expired = desk.expire_stale(ttl_seconds=999999)
        self.assertEqual(len(expired), 0)


class AttemptToExceedAuthorityInApprovalTests(PersistenceTestBase):
    def test_non_owner_cannot_approve(self):
        desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk.escalate(reason="x", context={}, actor="atlas@system")
        with self.assertRaises(AccessDenied):
            desk.approve(item.escalation_id, decision="ok", resolved_by=OPERATOR)

    def test_non_owner_cannot_reject(self):
        desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk.escalate(reason="x", context={}, actor="atlas@system")
        with self.assertRaises(AccessDenied):
            desk.reject(item.escalation_id, decision="nao", resolved_by=OPERATOR)

    def test_non_owner_cannot_cancel(self):
        desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        item = desk.escalate(reason="x", context={}, actor="atlas@system")
        with self.assertRaises(AccessDenied):
            desk.cancel(item.escalation_id, resolved_by=OPERATOR)

    def test_nonexistent_escalation_raises_not_found(self):
        desk = EscalationDesk(AuditTrail(db_path=self.db_path), db_path=self.db_path)
        with self.assertRaises(EscalationNotFound):
            desk.get("esc_never_existed")


if __name__ == "__main__":
    unittest.main()
