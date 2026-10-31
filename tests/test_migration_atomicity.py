import os
import sqlite3
import tempfile
import time
import unittest

from core.governance.idempotency import stable_fingerprint
from core.models import db as db_module

"""
Testes de regressão — MICROCORREÇÃO 5, item 1
(MIGRAÇÃO ATÔMICA E RECUPERÁVEL).

Falha original: a migração V3->V4 fazia rename/create/copia/drop como
passos SEPARADOS. Uma falha no meio deixava a tabela nova incompleta;
na reinicializacao, o codigo via idempotency_records ja existindo com
schema novo e concluia (errado) que a migracao tinha terminado -
permitindo duplicacao, pois os registros de idempotencia da tabela
antiga nunca foram copiados.

Corrigido: toda a migracao roda dentro de UMA transacao SQLite
explicita - uma falha em qualquer ponto reverte TUDO (SQLite tem DDL
transacional), deixando a tabela antiga intacta e utilizavel.
"""


class FailureInjectingConnection(sqlite3.Connection):
    trigger_sql_substring = ""
    fail_on_call_number = 1
    _call_count = 0

    def execute(self, sql, params=()):
        if self.trigger_sql_substring and self.trigger_sql_substring in sql:
            type(self)._call_count += 1
            if type(self)._call_count == self.fail_on_call_number:
                raise RuntimeError("falha simulada durante a migracao")
        return super().execute(sql, params)

    @classmethod
    def reset(cls):
        cls._call_count = 0


def _inject_failure(trigger_sql_substring: str):
    FailureInjectingConnection.trigger_sql_substring = trigger_sql_substring
    FailureInjectingConnection.reset()

    def factory(db_path: str):
        conn = sqlite3.connect(db_path, isolation_level=None, factory=FailureInjectingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    return factory


class MigrationTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def _create_v3_database_with(self, *, leads=(), sales_events=(), finance_entries=(), jobs=(), idem_records=()):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""CREATE TABLE leads (lead_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, source TEXT,
            campaign TEXT, content_id TEXT, offer_id TEXT, product_id TEXT, status TEXT NOT NULL DEFAULT 'NEW',
            consent_state TEXT NOT NULL DEFAULT 'UNKNOWN', conversion_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE sales_events (event_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
            lead_id TEXT, offer_id TEXT, event_type TEXT NOT NULL, value_type TEXT NOT NULL, amount REAL,
            currency TEXT DEFAULT 'BRL', created_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE finance_entries (entry_id TEXT PRIMARY KEY, channel_id TEXT,
            entry_type TEXT NOT NULL, amount REAL NOT NULL, currency TEXT DEFAULT 'BRL', description TEXT,
            created_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE jobs (job_id TEXT PRIMARY KEY, project_id TEXT, channel_id TEXT,
            job_type TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING', idempotency_key TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, payload TEXT,
            result TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE idempotency_records (composite_key TEXT PRIMARY KEY, domain TEXT NOT NULL,
            record_id TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at REAL NOT NULL)""")

        for lead in leads:
            conn.execute("INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", lead)
        for evt in sales_events:
            conn.execute("INSERT INTO sales_events VALUES (?,?,?,?,?,?,?,?,?)", evt)
        for fin in finance_entries:
            conn.execute("INSERT INTO finance_entries VALUES (?,?,?,?,?,?,?)", fin)
        for job in jobs:
            conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", job)
        for rec in idem_records:
            conn.execute("INSERT INTO idempotency_records VALUES (?,?,?,?,?)", rec)
        conn.commit()
        conn.close()


class NormalMigrationTests(MigrationTestBase):
    def test_migration_with_synthetic_data_across_all_four_domains(self):
        now = time.time()
        lead_fp = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        sale_fp = stable_fingerprint("chan_1", "sale", "REAL", 100.0, None, None)
        fin_fp = stable_fingerprint("GLOBAL", "revenue", 50.0, "")
        job_fp = stable_fingerprint("produce_video", "chan_1", "proj_1", "{}")

        self._create_v3_database_with(
            leads=[("lead_1", "chan_1", "youtube", None, None, None, None, "NEW", "UNKNOWN", "UNKNOWN", now, now)],
            sales_events=[("sevt_1", "chan_1", None, None, "sale", "REAL", 100.0, "BRL", now)],
            finance_entries=[("fin_1", None, "revenue", 50.0, "BRL", "", now)],
            jobs=[("job_1", "proj_1", "chan_1", "produce_video", "QUEUED", "jobkey", 0, 3, "{}", None, None, now, now)],
            idem_records=[
                ("chan_1::leadkey", "lead", "lead_1", lead_fp, now),
                ("chan_1::salekey", "sales_event", "sevt_1", sale_fp, now),
                ("GLOBAL::finkey", "finance_entry", "fin_1", fin_fp, now),
                ("chan_1::proj_1::produce_video::jobkey", "job", "job_1", job_fp, now),
            ],
        )

        db_module.init_db(self.db_path)

        from commercial.crm import CRM
        from commercial.finance import FinanceLedger
        from commercial.sales_tracking import SalesTracker
        from core.jobs.queue import JobQueue

        self.assertEqual(CRM(db_path=self.db_path).get("lead_1").channel_id, "chan_1")
        self.assertEqual(SalesTracker(db_path=self.db_path).summarize("chan_1").real_revenue, 100.0)
        self.assertEqual(FinanceLedger(db_path=self.db_path).snapshot(None).revenue, 50.0)
        self.assertEqual(JobQueue(db_path=self.db_path).get("job_1").job_type, "produce_video")

    def test_migration_is_safe_to_run_on_fresh_database(self):
        db_module.init_db(self.db_path)
        db_module.init_db(self.db_path)
        with db_module.connect(self.db_path) as conn:
            columns = db_module._table_columns(conn, "idempotency_records")
        self.assertIn("operation", columns)


class InterruptedMigrationTests(MigrationTestBase):
    def test_crash_between_rename_and_drop_leaves_everything_rolled_back(self):
        now = time.time()
        fp = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        self._create_v3_database_with(
            leads=[("lead_1", "chan_1", "youtube", None, None, None, None, "NEW", "UNKNOWN", "UNKNOWN", now, now)],
            idem_records=[("chan_1::leadkey", "lead", "lead_1", fp, now)],
        )

        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _inject_failure("DROP TABLE IF EXISTS idempotency_records_inline_legacy_v6")
        try:
            with self.assertRaises(RuntimeError):
                db_module.init_db(self.db_path)
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertNotIn("idempotency_records_migration_staging", tables)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(idempotency_records)").fetchall()}
            self.assertIn("composite_key", columns, "schema antigo deve continuar intacto após rollback")
            n = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
            self.assertEqual(n, 1)

    def test_retry_after_crash_completes_migration_without_duplication(self):
        now = time.time()
        fp = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        self._create_v3_database_with(
            leads=[("lead_1", "chan_1", "youtube", None, None, None, None, "NEW", "UNKNOWN", "UNKNOWN", now, now)],
            idem_records=[("chan_1::leadkey", "lead", "lead_1", fp, now)],
        )

        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _inject_failure("DROP TABLE IF EXISTS idempotency_records_inline_legacy_v6")
        try:
            with self.assertRaises(RuntimeError):
                db_module.init_db(self.db_path)
        finally:
            db_module.get_autocommit_connection = original

        db_module.init_db(self.db_path)

        from commercial.crm import CRM
        crm = CRM(db_path=self.db_path)
        same_lead = crm.create_lead("chan_1", idempotency_key="leadkey", source="youtube")
        self.assertEqual(same_lead.lead_id, "lead_1")
        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
        self.assertEqual(n, 1, "não pode ter duplicado após crash + retry")

    def test_unparseable_row_blocks_migration_instead_of_silently_dropping(self):
        now = time.time()
        self._create_v3_database_with(
            leads=[("lead_1", "chan_1", "youtube", None, None, None, None, "NEW", "UNKNOWN", "UNKNOWN", now, now)],
            idem_records=[("formato-totalmente-invalido-sem-separador", "lead", "lead_1", "{}", now)],
        )
        with self.assertRaises(db_module.MigrationBlocked):
            db_module.init_db(self.db_path)

        with db_module.connect(self.db_path) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(idempotency_records)").fetchall()}
            self.assertIn("composite_key", columns)
            n = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
            self.assertEqual(n, 1)


class OrphanedStagingRecoveryTests(MigrationTestBase):
    def _create_orphaned_staging_state(self):
        now = time.time()
        fp = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""CREATE TABLE leads (lead_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, source TEXT,
            campaign TEXT, content_id TEXT, offer_id TEXT, product_id TEXT, status TEXT NOT NULL DEFAULT 'NEW',
            consent_state TEXT NOT NULL DEFAULT 'UNKNOWN', conversion_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE idempotency_records_migration_staging (composite_key TEXT PRIMARY KEY,
            domain TEXT NOT NULL, record_id TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE idempotency_records (
            domain TEXT NOT NULL, operation TEXT NOT NULL, channel_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL, record_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY (domain, operation, channel_id, project_id, idempotency_key))""")
        conn.execute(
            "INSERT INTO leads VALUES ('lead_orphan','chan_1','youtube',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO idempotency_records_migration_staging VALUES (?,?,?,?,?)",
            ("chan_1::orphankey", "lead", "lead_orphan", fp, now),
        )
        conn.commit()
        conn.close()

    def test_recovers_from_orphaned_staging_left_by_old_buggy_code(self):
        self._create_orphaned_staging_state()
        db_module.init_db(self.db_path)

        with db_module.connect(self.db_path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertNotIn("idempotency_records_migration_staging", tables)
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
            self.assertEqual(n, 1)

        from commercial.crm import CRM
        crm = CRM(db_path=self.db_path)
        recovered = crm.create_lead("chan_1", idempotency_key="orphankey", source="youtube")
        self.assertEqual(recovered.lead_id, "lead_orphan")

    def test_malformed_staging_blocks_startup_with_diagnostic(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE idempotency_records_migration_staging (only_one_weird_column TEXT)")
        conn.commit()
        conn.close()

        with self.assertRaises(db_module.MigrationBlocked):
            db_module.init_db(self.db_path)


if __name__ == "__main__":
    unittest.main()
