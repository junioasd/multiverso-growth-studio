import os
import tempfile
import unittest
from tests._auth_test_helpers import fast_hash_password

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.sales_tracking import SalesTracker
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.models.entities import JobState, LeadStatus, ValueType
from core.security.audit import AuditTrail

"""
Testes de regressão — MICROCORREÇÃO 2, item 5 (PERSISTÊNCIA).

Falha original: as tabelas existiam no schema SQLite, mas nenhum
serviço realmente as usava — tudo vivia apenas em memória e se perdia
a cada reinício. Estes testes criam uma instância, mutam estado,
DESCARTAM a instância e criam uma NOVA apontando para o mesmo arquivo
de banco — simulando um reinício real do processo.
"""


class PersistenceTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)  # deixa o init_db criar do zero
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)


class CRMPersistenceTests(PersistenceTestBase):
    def test_lead_survives_restart(self):
        crm1 = CRM(db_path=self.db_path)
        lead = crm1.create_lead("chan_1", source="youtube", campaign="camp-1")
        del crm1

        crm2 = CRM(db_path=self.db_path)
        recovered = crm2.get(lead.lead_id)
        self.assertEqual(recovered.channel_id, "chan_1")
        self.assertEqual(recovered.source, "youtube")
        self.assertEqual(recovered.campaign, "camp-1")

    def test_lead_status_transition_survives_restart(self):
        crm1 = CRM(db_path=self.db_path)
        lead = crm1.create_lead("chan_1", source="youtube")
        crm1.transition(lead.lead_id, LeadStatus.ENGAGED)
        del crm1

        crm2 = CRM(db_path=self.db_path)
        recovered = crm2.get(lead.lead_id)
        self.assertEqual(recovered.status, LeadStatus.ENGAGED)

    def test_idempotency_index_survives_restart(self):
        crm1 = CRM(db_path=self.db_path)
        lead = crm1.create_lead("chan_1", idempotency_key="dup-1", source="youtube")
        del crm1

        crm2 = CRM(db_path=self.db_path)
        same_lead = crm2.create_lead("chan_1", idempotency_key="dup-1", source="youtube")
        self.assertEqual(same_lead.lead_id, lead.lead_id)
        self.assertEqual(len(crm2.by_channel("chan_1")), 1)


class SalesTrackerPersistenceTests(PersistenceTestBase):
    def test_events_and_summary_survive_restart(self):
        tracker1 = SalesTracker(db_path=self.db_path)
        tracker1.record_event(
            channel_id="chan_1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="s1"
        )
        tracker1.record_event(
            channel_id="chan_1", event_type="refund", value_type=ValueType.REAL, amount=40, idempotency_key="r1"
        )
        del tracker1

        tracker2 = SalesTracker(db_path=self.db_path)
        summary = tracker2.summarize("chan_1")
        self.assertEqual(summary.sales, 1)
        self.assertEqual(summary.refunds, 1)
        self.assertEqual(summary.real_revenue, 60)  # 100 - 40, líquido preservado

    def test_idempotency_survives_restart_and_still_blocks_duplicate(self):
        tracker1 = SalesTracker(db_path=self.db_path)
        tracker1.record_event(
            channel_id="chan_1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="s1"
        )
        del tracker1

        tracker2 = SalesTracker(db_path=self.db_path)
        event2 = tracker2.record_event(
            channel_id="chan_1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="s1"
        )
        # não duplicou a venda mesmo após o restart
        self.assertEqual(tracker2.summarize("chan_1").sales, 1)


class FinanceLedgerPersistenceTests(PersistenceTestBase):
    def test_entries_and_snapshot_survive_restart(self):
        ledger1 = FinanceLedger(db_path=self.db_path)
        ledger1.record(entry_type="revenue", amount=500, channel_id="chan_1")
        ledger1.record(entry_type="commission", amount=50, channel_id="chan_1")
        del ledger1

        ledger2 = FinanceLedger(db_path=self.db_path)
        snap = ledger2.snapshot("chan_1")
        self.assertEqual(snap.revenue, 500)
        self.assertEqual(snap.commission, 50)
        self.assertEqual(snap.margin, 450)


class JobQueuePersistenceTests(PersistenceTestBase):
    def test_job_survives_restart(self):
        queue1 = JobQueue(db_path=self.db_path)
        job = queue1.enqueue(job_type="produce_video", idempotency_key="j1", channel_id="chan_1")
        del queue1

        queue2 = JobQueue(db_path=self.db_path)
        recovered = queue2.get(job.job_id)
        self.assertEqual(recovered.channel_id, "chan_1")
        self.assertEqual(recovered.state, JobState.QUEUED)

    def test_job_idempotency_survives_restart(self):
        queue1 = JobQueue(db_path=self.db_path)
        job = queue1.enqueue(job_type="produce_video", idempotency_key="j1", channel_id="chan_1")
        del queue1

        queue2 = JobQueue(db_path=self.db_path)
        same_job = queue2.enqueue(job_type="produce_video", idempotency_key="j1", channel_id="chan_1")
        self.assertEqual(same_job.job_id, job.job_id)
        self.assertEqual(len(queue2.all()), 1)

    def test_job_state_progress_survives_restart(self):
        queue1 = JobQueue(db_path=self.db_path)
        job = queue1.enqueue(job_type="produce_video", idempotency_key="j1", channel_id="chan_1")
        queue1.start(job.job_id)
        queue1.complete(job.job_id, {"ok": True})
        del queue1

        queue2 = JobQueue(db_path=self.db_path)
        recovered = queue2.get(job.job_id)
        self.assertEqual(recovered.state, JobState.COMPLETED)
        self.assertEqual(recovered.result, {"ok": True})


class AuditTrailPersistenceTests(PersistenceTestBase):
    def test_events_survive_restart(self):
        audit1 = AuditTrail(db_path=self.db_path)
        audit1.record(actor="owner", action="test_action", resource="res-1", outcome="SUCCESS", details={"x": 1})
        del audit1

        audit2 = AuditTrail(db_path=self.db_path)
        events = audit2.all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "test_action")
        self.assertEqual(events[0].outcome, "SUCCESS")

    def test_secrets_never_persisted_even_across_restart(self):
        audit1 = AuditTrail(db_path=self.db_path)
        audit1.record(
            actor="owner", action="login_attempt", resource="owner_session", outcome="DENIED",
            details={"password": "hunter2", "email": "a@b.com"},
        )
        del audit1

        audit2 = AuditTrail(db_path=self.db_path)
        events = audit2.all()
        self.assertEqual(events[0].details["password"], "[REDACTED]")


class FullAppPersistenceTests(PersistenceTestBase):
    def test_full_app_restart_via_bootstrap(self):
        from api.bootstrap import build_app
        from core.security.rbac import Principal, Role

        app1 = build_app(
            owner_email="owner@x.com", owner_password_hash=fast_hash_password("StrongPass123"),
            db_path=self.db_path, init_database=False,
        )
        owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
        app1.facade.create_lead(owner, channel_id="chan_1", source="youtube", idempotency_key="lead-1")
        app1.facade.record_sales_event(
            owner, channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
            amount=300, idempotency_key="sale-1",
        )
        del app1

        app2 = build_app(
            owner_email="owner@x.com", owner_password_hash=fast_hash_password("StrongPass123"),
            db_path=self.db_path, init_database=False,
        )
        self.assertEqual(len(app2.multigerenciador.crm.by_channel("chan_1")), 1)
        self.assertEqual(app2.multigerenciador.sales_tracker.summarize("chan_1").sales, 1)
        self.assertEqual(app2.multigerenciador.sales_tracker.summarize("chan_1").real_revenue, 300)


if __name__ == "__main__":
    unittest.main()
