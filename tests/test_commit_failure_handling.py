import os
import sqlite3
import tempfile
import unittest

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.sales_tracking import SalesTracker
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.models.entities import ValueType

"""
Testes de regressão — MICROCORREÇÃO 5, item 2
(TRATAMENTO COMPLETO DE FALHAS NO COMMIT).

Falha original: `conn.commit()` ficava num bloco `else:` FORA do
try/except — uma falha no próprio commit (não na inserção) escapava
sem rollback nem fechamento da conexão, deixando uma transação aberta
e travando outros escritores.

Corrigido: BEGIN + gravações + COMMIT sempre no MESMO try/except.
"""


class FailOnCommitConnection(sqlite3.Connection):
    """Falha especificamente no commit() — nunca na execução das
    instruções — para isolar exatamente o cenário relatado."""

    should_fail = True

    def commit(self):
        if type(self).should_fail:
            type(self).should_fail = False  # falha só na primeira vez
            raise RuntimeError("falha simulada especificamente no COMMIT")
        return super().commit()

    @classmethod
    def reset(cls):
        cls.should_fail = True


def _commit_failing_factory():
    FailOnCommitConnection.reset()

    def factory(db_path: str):
        conn = sqlite3.connect(db_path, isolation_level=None, factory=FailOnCommitConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    return factory


class PersistenceTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def _assert_no_stuck_lock_and_other_writer_succeeds(self, write_fn):
        """write_fn() deve conseguir escrever normalmente, provando que
        a falha anterior não deixou a conexão/lock presos."""
        write_fn()


class CRMCommitFailureTests(PersistenceTestBase):
    def test_commit_failure_on_create_leaves_no_partial_state(self):
        crm = CRM(db_path=self.db_path)
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                crm.create_lead("chan_1", idempotency_key="k1", source="youtube")
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"], 0)
        self.assertEqual(len(crm._leads), 0, "cache não pode ter avançado com um commit que falhou")

    def test_other_connection_can_write_after_commit_failure(self):
        crm = CRM(db_path=self.db_path)
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                crm.create_lead("chan_1", idempotency_key="k1", source="youtube")
        finally:
            db_module.get_autocommit_connection = original

        crm2 = CRM(db_path=self.db_path)
        lead = crm2.create_lead("chan_1", idempotency_key="other-key", source="youtube")
        self.assertIsNotNone(lead.lead_id)

    def test_retry_with_same_key_after_commit_failure_succeeds_without_duplication(self):
        crm = CRM(db_path=self.db_path)
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                crm.create_lead("chan_1", idempotency_key="k1", source="youtube")
        finally:
            db_module.get_autocommit_connection = original

        lead = crm.create_lead("chan_1", idempotency_key="k1", source="youtube")
        self.assertIsNotNone(lead.lead_id)
        with db_module.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"], 1)

    def test_audit_never_logs_success_for_commit_failure(self):
        from commercial.multigerenciador import MultiGerenciadorComercial
        from core.governance.facade import GovernedFacade
        from core.jobs.queue import JobQueue as JQ
        from core.security.audit import AuditTrail
        from core.security.rbac import Principal, Role

        mgc = MultiGerenciadorComercial(db_path=self.db_path)
        audit = AuditTrail(db_path=self.db_path)
        facade = GovernedFacade(job_queue=JQ(db_path=self.db_path), multigerenciador=mgc, audit=audit)
        owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})

        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                facade.create_lead(owner, channel_id="chan_1", idempotency_key="k1", source="youtube")
        finally:
            db_module.get_autocommit_connection = original

        events = [e for e in audit.all() if e.action == "create_lead"]
        self.assertTrue(all(e.outcome != "SUCCESS" for e in events))
        self.assertTrue(any(e.outcome == "ERROR" for e in events))


class SalesTrackerCommitFailureTests(PersistenceTestBase):
    def test_commit_failure_leaves_no_partial_state(self):
        tracker = SalesTracker(db_path=self.db_path)
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                tracker.record_event(
                    channel_id="c1", event_type="sale", value_type=ValueType.REAL,
                    amount=100, idempotency_key="k1",
                )
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) as n FROM sales_events").fetchone()["n"], 0)

    def test_other_connection_can_write_after_commit_failure(self):
        tracker = SalesTracker(db_path=self.db_path)
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                tracker.record_event(channel_id="c1", event_type="click", idempotency_key="k1")
        finally:
            db_module.get_autocommit_connection = original

        tracker2 = SalesTracker(db_path=self.db_path)
        event = tracker2.record_event(channel_id="c1", event_type="click", idempotency_key="k2")
        self.assertIsNotNone(event.event_id)

    def test_retry_after_commit_failure_no_duplication(self):
        tracker = SalesTracker(db_path=self.db_path)
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                tracker.record_event(
                    channel_id="c1", event_type="sale", value_type=ValueType.REAL,
                    amount=100, idempotency_key="k1",
                )
        finally:
            db_module.get_autocommit_connection = original

        event = tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="k1"
        )
        self.assertEqual(tracker.summarize("c1").sales, 1)
        self.assertIsNotNone(event.event_id)


class FinanceLedgerCommitFailureTests(PersistenceTestBase):
    def test_commit_failure_leaves_no_partial_state(self):
        ledger = FinanceLedger(db_path=self.db_path)
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                ledger.record(entry_type="revenue", amount=100, channel_id="c1", idempotency_key="k1")
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) as n FROM finance_entries").fetchone()["n"], 0)

    def test_retry_after_commit_failure_no_duplication(self):
        ledger = FinanceLedger(db_path=self.db_path)
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                ledger.record(entry_type="revenue", amount=100, channel_id="c1", idempotency_key="k1")
        finally:
            db_module.get_autocommit_connection = original

        entry = ledger.record(entry_type="revenue", amount=100, channel_id="c1", idempotency_key="k1")
        self.assertEqual(ledger.snapshot("c1").revenue, 100)
        self.assertIsNotNone(entry.entry_id)


class JobQueueCommitFailureTests(PersistenceTestBase):
    def test_enqueue_commit_failure_leaves_no_partial_state(self):
        queue = JobQueue(db_path=self.db_path)
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                queue.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) as n FROM jobs").fetchone()["n"], 0)

    def test_start_commit_failure_leaves_previous_state(self):
        queue = JobQueue(db_path=self.db_path)
        job = queue.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")

        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                queue.start(job.job_id)
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT state FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
        self.assertEqual(row["state"], "QUEUED", "não pode ter avançado para RUNNING com commit falho")

    def test_other_connection_can_start_job_after_commit_failure(self):
        queue = JobQueue(db_path=self.db_path)
        job_a = queue.enqueue(job_type="produce_video", idempotency_key="a", channel_id="c1")
        job_b = queue.enqueue(job_type="produce_video", idempotency_key="b", channel_id="c1")

        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                queue.start(job_a.job_id)
        finally:
            db_module.get_autocommit_connection = original

        queue2 = JobQueue(db_path=self.db_path)
        started = queue2.start(job_b.job_id)
        self.assertEqual(started.state.value, "RUNNING")

    def test_retry_start_after_commit_failure_succeeds(self):
        queue = JobQueue(db_path=self.db_path)
        job = queue.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")

        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _commit_failing_factory()
        try:
            with self.assertRaises(RuntimeError):
                queue.start(job.job_id)
        finally:
            db_module.get_autocommit_connection = original

        started = queue.start(job.job_id)
        self.assertEqual(started.state.value, "RUNNING")


if __name__ == "__main__":
    unittest.main()
