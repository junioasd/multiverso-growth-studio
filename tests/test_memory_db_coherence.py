import os
import tempfile
import unittest

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.sales_tracking import SalesTracker
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.models.entities import JobState, LeadStatus, ValueType

"""
Testes de regressão — MICROCORREÇÃO 4, item 2
(COERÊNCIA ENTRE MEMÓRIA E BANCO APÓS FALHAS).

Falha original:
- Criação sem idempotency_key: o objeto era colocado no cache em
  memória ANTES da gravação no banco — uma falha na gravação deixava o
  registro "existir" em memória sem existir no banco.
- Transição de lead / mutação de job: o objeto compartilhado no cache
  era mutado in-place (`lead.status = novo_status`) ANTES de persistir
  — uma falha na persistência deixava o estado avançado em memória
  enquanto o banco mantinha o estado anterior.

Corrigido: toda mutação primeiro persiste (quando `db_path` está
configurado); o cache em memória só é tocado/substituído DEPOIS da
confirmação. Em caso de erro, nem o cache nem o banco mudam.
"""


class PersistenceTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def _break_connect(self):
        self._original_connect = db_module.connect

        def failing_connect(path):
            raise RuntimeError("falha simulada de gravação")

        db_module.connect = failing_connect

    def _restore_connect(self):
        db_module.connect = self._original_connect


class CreateWithoutIdempotencyKeyCoherenceTests(PersistenceTestBase):
    def test_crm_create_lead_failure_leaves_nothing_in_memory(self):
        crm = CRM(db_path=self.db_path)
        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                crm.create_lead("chan_1", source="youtube")
        finally:
            self._restore_connect()

        self.assertEqual(len(crm._leads), 0)
        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_sales_tracker_record_event_failure_leaves_nothing_in_memory(self):
        tracker = SalesTracker(db_path=self.db_path)
        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                tracker.record_event(channel_id="c1", event_type="click")
        finally:
            self._restore_connect()

        self.assertEqual(len(tracker._events), 0)
        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM sales_events").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_finance_ledger_record_failure_leaves_nothing_in_memory(self):
        ledger = FinanceLedger(db_path=self.db_path)
        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                ledger.record(entry_type="expense", amount=10, channel_id="c1")
        finally:
            self._restore_connect()

        self.assertEqual(len(ledger._entries), 0)
        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM finance_entries").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_successful_create_after_failed_attempt_works_normally(self):
        crm = CRM(db_path=self.db_path)
        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                crm.create_lead("chan_1", source="youtube")
        finally:
            self._restore_connect()

        lead = crm.create_lead("chan_1", source="youtube")
        self.assertIsNotNone(lead.lead_id)
        self.assertEqual(len(crm.by_channel("chan_1")), 1)


class TransitionCoherenceTests(PersistenceTestBase):
    def test_lead_transition_failure_preserves_previous_status_in_memory_and_db(self):
        crm = CRM(db_path=self.db_path)
        lead = crm.create_lead("chan_1", source="youtube")
        self.assertEqual(lead.status, LeadStatus.NEW)

        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                crm.transition(lead.lead_id, LeadStatus.ENGAGED)
        finally:
            self._restore_connect()

        cached = crm._leads[lead.lead_id]
        self.assertEqual(cached.status, LeadStatus.NEW, "cache não pode ter avançado sem persistir")

        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT status FROM leads WHERE lead_id=?", (lead.lead_id,)).fetchone()
        self.assertEqual(row["status"], "NEW")

    def test_lead_transition_never_mutates_shared_object_before_persist(self):
        """Verifica que o objeto Lead retornado por create_lead() e
        mantido em cache não é o MESMO objeto mutado por uma tentativa
        de transição que falhou — deve ser tratado como imutável até a
        persistência confirmar."""
        crm = CRM(db_path=self.db_path)
        lead = crm.create_lead("chan_1", source="youtube")
        original_reference = crm._leads[lead.lead_id]

        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                crm.transition(lead.lead_id, LeadStatus.ENGAGED)
        finally:
            self._restore_connect()

        # a referência em cache continua com o status original — não foi
        # mutada in-place por uma tentativa que falhou.
        self.assertEqual(original_reference.status, LeadStatus.NEW)

    def test_successful_transition_after_failed_attempt_works_normally(self):
        crm = CRM(db_path=self.db_path)
        lead = crm.create_lead("chan_1", source="youtube")

        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                crm.transition(lead.lead_id, LeadStatus.ENGAGED)
        finally:
            self._restore_connect()

        updated = crm.transition(lead.lead_id, LeadStatus.ENGAGED)
        self.assertEqual(updated.status, LeadStatus.ENGAGED)
        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT status FROM leads WHERE lead_id=?", (lead.lead_id,)).fetchone()
        self.assertEqual(row["status"], "ENGAGED")


class JobStateCoherenceTests(PersistenceTestBase):
    def test_job_start_failure_preserves_previous_state(self):
        queue = JobQueue(db_path=self.db_path)
        job = queue.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")
        self.assertEqual(job.state, JobState.QUEUED)

        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                queue.start(job.job_id)
        finally:
            self._restore_connect()

        cached = queue._jobs[job.job_id]
        self.assertEqual(cached.state, JobState.QUEUED, "cache não pode ter avançado sem persistir")
        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT state FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
        self.assertEqual(row["state"], "QUEUED")

    def test_job_complete_failure_preserves_running_state(self):
        queue = JobQueue(db_path=self.db_path)
        job = queue.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")
        queue.start(job.job_id)

        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                queue.complete(job.job_id, {"ok": True})
        finally:
            self._restore_connect()

        cached = queue._jobs[job.job_id]
        self.assertEqual(cached.state, JobState.RUNNING)
        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT state FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
        self.assertEqual(row["state"], "RUNNING")

    def test_job_never_mutates_shared_object_before_persist(self):
        queue = JobQueue(db_path=self.db_path)
        job = queue.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")
        original_reference = queue._jobs[job.job_id]

        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                queue.start(job.job_id)
        finally:
            self._restore_connect()

        self.assertEqual(original_reference.state, JobState.QUEUED)

    def test_successful_start_after_failed_attempt_works_normally(self):
        queue = JobQueue(db_path=self.db_path)
        job = queue.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")

        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                queue.start(job.job_id)
        finally:
            self._restore_connect()

        started = queue.start(job.job_id)
        self.assertEqual(started.state, JobState.RUNNING)


class AuditCoherenceTests(PersistenceTestBase):
    def test_facade_never_logs_success_when_underlying_write_fails(self):
        from commercial.multigerenciador import MultiGerenciadorComercial
        from core.governance.facade import GovernedFacade
        from core.jobs.queue import JobQueue as JQ
        from core.security.audit import AuditTrail
        from core.security.rbac import Principal, Role

        mgc = MultiGerenciadorComercial(db_path=self.db_path)
        audit = AuditTrail(db_path=self.db_path)
        facade = GovernedFacade(job_queue=JQ(db_path=self.db_path), multigerenciador=mgc, audit=audit)
        owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})

        self._break_connect()
        try:
            with self.assertRaises(RuntimeError):
                facade.create_lead(owner, channel_id="chan_1", source="youtube")
        finally:
            self._restore_connect()

        events = [e for e in audit.all() if e.action == "create_lead"]
        self.assertTrue(all(e.outcome != "SUCCESS" for e in events))
        self.assertTrue(any(e.outcome == "ERROR" for e in events))


if __name__ == "__main__":
    unittest.main()
