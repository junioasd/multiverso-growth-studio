import os
import sqlite3
import tempfile
import time
import unittest

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.sales_tracking import SalesTracker
from core.governance.idempotency import IdempotencyConflict, stable_fingerprint
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.models.entities import ValueType

"""
Testes de regressão — MICROCORREÇÃO 4, item 1
(IDEMPOTÊNCIA SEM CONFLITO ENTRE MÓDULOS).

Falha original: a tabela `idempotency_records` usava uma única string
concatenada (`composite_key`) como PRIMARY KEY, sem separar o domínio
(lead vs sales_event vs finance_entry vs job) — a mesma
`idempotency_key` usada no mesmo canal por dois módulos diferentes
colidia, bloqueando uma operação legítima.

Corrigido com colunas próprias (domain, operation, channel_id,
project_id, idempotency_key) formando uma PRIMARY KEY composta real —
sem concatenação de string.
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


class NoCrossDomainConflictTests(PersistenceTestBase):
    def test_same_key_same_channel_lead_and_sale_never_conflict(self):
        crm = CRM(db_path=self.db_path)
        tracker = SalesTracker(db_path=self.db_path)

        lead = crm.create_lead("chan_1", idempotency_key="shared-key", source="youtube")
        event = tracker.record_event(
            channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
            amount=100, idempotency_key="shared-key",
        )
        self.assertIsNotNone(lead.lead_id)
        self.assertIsNotNone(event.event_id)

    def test_same_key_same_channel_lead_and_finance_never_conflict(self):
        crm = CRM(db_path=self.db_path)
        ledger = FinanceLedger(db_path=self.db_path)

        lead = crm.create_lead("chan_1", idempotency_key="shared-key", source="youtube")
        entry = ledger.record(entry_type="revenue", amount=50, channel_id="chan_1", idempotency_key="shared-key")
        self.assertIsNotNone(lead.lead_id)
        self.assertIsNotNone(entry.entry_id)

    def test_same_key_same_channel_all_four_domains_never_conflict(self):
        crm = CRM(db_path=self.db_path)
        tracker = SalesTracker(db_path=self.db_path)
        ledger = FinanceLedger(db_path=self.db_path)
        queue = JobQueue(db_path=self.db_path)

        lead = crm.create_lead("chan_1", idempotency_key="k", source="youtube")
        event = tracker.record_event(channel_id="chan_1", event_type="click", idempotency_key="k")
        entry = ledger.record(entry_type="expense", amount=10, channel_id="chan_1", idempotency_key="k")
        job = queue.enqueue(job_type="produce_video", idempotency_key="k", channel_id="chan_1")

        self.assertIsNotNone(lead.lead_id)
        self.assertIsNotNone(event.event_id)
        self.assertIsNotNone(entry.entry_id)
        self.assertIsNotNone(job.job_id)

        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(count, 4)

    def test_dedup_still_works_within_same_domain_and_scope(self):
        crm = CRM(db_path=self.db_path)
        lead1 = crm.create_lead("chan_1", idempotency_key="k", source="youtube")
        lead2 = crm.create_lead("chan_1", idempotency_key="k", source="youtube")
        self.assertEqual(lead1.lead_id, lead2.lead_id)

    def test_conflict_still_detected_within_same_domain_and_scope(self):
        crm = CRM(db_path=self.db_path)
        crm.create_lead("chan_1", idempotency_key="k", source="youtube")
        with self.assertRaises(IdempotencyConflict):
            crm.create_lead("chan_1", idempotency_key="k", source="tiktok")

    def test_different_channels_same_key_same_domain_never_conflict(self):
        crm = CRM(db_path=self.db_path)
        lead_a = crm.create_lead("chan_A", idempotency_key="k", source="youtube")
        lead_b = crm.create_lead("chan_B", idempotency_key="k", source="youtube")
        self.assertNotEqual(lead_a.lead_id, lead_b.lead_id)

    def test_in_memory_mode_also_never_cross_domain_conflicts(self):
        crm = CRM()
        tracker = SalesTracker()
        lead = crm.create_lead("chan_1", idempotency_key="k", source="youtube")
        event = tracker.record_event(channel_id="chan_1", event_type="click", idempotency_key="k")
        self.assertIsNotNone(lead.lead_id)
        self.assertIsNotNone(event.event_id)


class IdempotencyRecordsSchemaTests(PersistenceTestBase):
    def test_table_has_no_composite_key_column(self):
        with db_module.connect(self.db_path) as conn:
            columns = db_module._table_columns(conn, "idempotency_records")
        self.assertNotIn("composite_key", columns)
        for expected in ("domain", "operation", "channel_id", "project_id", "idempotency_key"):
            self.assertIn(expected, columns)

    def test_primary_key_is_composite_of_five_columns(self):
        with db_module.connect(self.db_path) as conn:
            pk_columns = [
                row[1] for row in conn.execute("PRAGMA table_info(idempotency_records)").fetchall() if row[5] > 0
            ]
        self.assertEqual(
            set(pk_columns), {"domain", "operation", "channel_id", "project_id", "idempotency_key"}
        )


class V3ToV4MigrationTests(unittest.TestCase):
    """Simula um banco no formato ANTIGO (V3, composite_key concatenado)
    com dados sintéticos, roda a migração, e confirma que os registros
    e a proteção contra duplicação sobrevivem."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def _create_v3_database(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE leads (
                lead_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, source TEXT, campaign TEXT,
                content_id TEXT, offer_id TEXT, product_id TEXT, status TEXT NOT NULL DEFAULT 'NEW',
                consent_state TEXT NOT NULL DEFAULT 'UNKNOWN', conversion_state TEXT NOT NULL DEFAULT 'UNKNOWN',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE sales_events (
                event_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, lead_id TEXT, offer_id TEXT,
                event_type TEXT NOT NULL, value_type TEXT NOT NULL, amount REAL, currency TEXT DEFAULT 'BRL',
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE idempotency_records (
                composite_key TEXT PRIMARY KEY, domain TEXT NOT NULL, record_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, created_at REAL NOT NULL
            )
        """)
        now = time.time()
        conn.execute(
            "INSERT INTO leads VALUES ('lead_v3_1','chan_1','youtube',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO sales_events VALUES ('sevt_v3_1','chan_1',NULL,NULL,'sale','REAL',100.0,'BRL',?)", (now,)
        )
        lead_fingerprint = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        sale_fingerprint = stable_fingerprint("chan_1", "sale", "REAL", 100.0, None, None)
        conn.execute(
            "INSERT INTO idempotency_records VALUES (?,?,?,?,?)",
            ("chan_1::synthetic-lead-key", "lead", "lead_v3_1", lead_fingerprint, now),
        )
        conn.execute(
            "INSERT INTO idempotency_records VALUES (?,?,?,?,?)",
            ("chan_1::synthetic-sale-key", "sales_event", "sevt_v3_1", sale_fingerprint, now),
        )
        conn.commit()
        conn.close()

    def test_migration_preserves_existing_leads_and_sales(self):
        self._create_v3_database()
        db_module.init_db(self.db_path)

        crm = CRM(db_path=self.db_path)
        lead = crm.get("lead_v3_1")
        self.assertEqual(lead.channel_id, "chan_1")
        self.assertEqual(lead.source, "youtube")

        tracker = SalesTracker(db_path=self.db_path)
        summary = tracker.summarize("chan_1")
        self.assertEqual(summary.sales, 1)
        self.assertEqual(summary.real_revenue, 100.0)

    def test_migration_preserves_idempotency_dedup_for_lead(self):
        self._create_v3_database()
        db_module.init_db(self.db_path)

        crm = CRM(db_path=self.db_path)
        same_lead = crm.create_lead("chan_1", idempotency_key="synthetic-lead-key", source="youtube")
        self.assertEqual(same_lead.lead_id, "lead_v3_1")

        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
        self.assertEqual(count, 1, "não pode duplicar após migração")

    def test_migration_preserves_idempotency_conflict_detection(self):
        self._create_v3_database()
        db_module.init_db(self.db_path)

        crm = CRM(db_path=self.db_path)
        with self.assertRaises(IdempotencyConflict):
            crm.create_lead("chan_1", idempotency_key="synthetic-lead-key", source="tiktok")

    def test_migration_does_not_erase_database(self):
        self._create_v3_database()
        with db_module.connect(self.db_path) as conn:
            leads_before = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]

        db_module.init_db(self.db_path)

        with db_module.connect(self.db_path) as conn:
            leads_after = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
        self.assertEqual(leads_before, leads_after)

    def test_migration_is_idempotent_safe_to_run_twice(self):
        self._create_v3_database()
        db_module.init_db(self.db_path)
        db_module.init_db(self.db_path)

        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(count, 2)

    def test_migrated_lead_and_sale_no_longer_conflict_with_each_other(self):
        self._create_v3_database()
        db_module.init_db(self.db_path)

        tracker = SalesTracker(db_path=self.db_path)
        event = tracker.record_event(
            channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
            amount=200, idempotency_key="synthetic-lead-key",
        )
        self.assertIsNotNone(event.event_id)


if __name__ == "__main__":
    unittest.main()
