import os
import sqlite3
import tempfile
import time
import unittest

from core.governance.idempotency import stable_fingerprint
from core.models import db as db_module

"""
Testes de regressao - MICROCORRECAO 6 (auditoria da entrega V5,
reprovada - dois problemas reproduzidos).

Falha 1: a V4 real deixava a tabela antiga com o nome
idempotency_records_v3_old; a V5 so reconhecia
idempotency_records_migration_staging - nunca detectava o estado
real deixado pela V4 interrompida.

Falha 2: a recuperacao da V5 apagava idempotency_records (que podia
ja ter operacoes legitimas confirmadas DEPOIS da interrupcao) para
reconstruir so a partir da tabela antiga - perdendo esses registros e
permitindo duplicacao no retry.

Corrigido com um algoritmo de mesclagem unificado.
"""


class FailureInjectingConnection(sqlite3.Connection):
    trigger_sql_substring = ""
    fail_on_call_number = 1
    _call_count = 0

    def execute(self, sql, params=()):
        if self.trigger_sql_substring and self.trigger_sql_substring != "__COMMIT__" and self.trigger_sql_substring in sql:
            type(self)._call_count += 1
            if type(self)._call_count == self.fail_on_call_number:
                raise RuntimeError("falha simulada durante a recuperacao")
        return super().execute(sql, params)

    def commit(self):
        if self.trigger_sql_substring == "__COMMIT__":
            type(self)._call_count += 1
            if type(self)._call_count == self.fail_on_call_number:
                raise RuntimeError("falha simulada especificamente no COMMIT da recuperacao")
        return super().commit()

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


class RecoveryTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def _base_schema(self, conn: sqlite3.Connection) -> None:
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

    def _legacy_table_sql(self, table_name: str) -> str:
        return (
            f"CREATE TABLE {table_name} (composite_key TEXT PRIMARY KEY, domain TEXT NOT NULL, "
            "record_id TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at REAL NOT NULL)"
        )

    def _new_schema_table_sql(self) -> str:
        return (
            "CREATE TABLE idempotency_records ("
            "domain TEXT NOT NULL, operation TEXT NOT NULL, channel_id TEXT NOT NULL DEFAULT '', "
            "project_id TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL, record_id TEXT NOT NULL, "
            "fingerprint TEXT NOT NULL, created_at REAL NOT NULL, "
            "PRIMARY KEY (domain, operation, channel_id, project_id, idempotency_key))"
        )


class RealV4InterruptedStateTests(RecoveryTestBase):
    def test_recognizes_real_v4_legacy_table_name(self):
        conn = sqlite3.connect(self.db_path)
        self._base_schema(conn)
        conn.execute(self._legacy_table_sql("idempotency_records_v3_old"))
        now = time.time()
        conn.execute(
            "INSERT INTO leads VALUES ('lead_1','chan_1','youtube',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        fp = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        conn.execute(
            "INSERT INTO idempotency_records_v3_old VALUES (?,?,?,?,?)",
            ("chan_1::realkey", "lead", "lead_1", fp, now),
        )
        conn.commit()
        conn.close()

        db_module.init_db(self.db_path)

        with db_module.connect(self.db_path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertNotIn("idempotency_records_v3_old", tables)
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
            self.assertEqual(n, 1)

        from commercial.crm import CRM
        crm = CRM(db_path=self.db_path)
        same = crm.create_lead("chan_1", idempotency_key="realkey", source="youtube")
        self.assertEqual(same.lead_id, "lead_1")
        with db_module.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"], 1)

    def test_never_treats_bare_existence_of_new_schema_as_completion_proof(self):
        conn = sqlite3.connect(self.db_path)
        self._base_schema(conn)
        conn.execute(self._legacy_table_sql("idempotency_records_v3_old"))
        conn.execute(self._new_schema_table_sql())
        now = time.time()
        conn.execute(
            "INSERT INTO leads VALUES ('lead_1','chan_1','youtube',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        fp = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        conn.execute(
            "INSERT INTO idempotency_records_v3_old VALUES (?,?,?,?,?)",
            ("chan_1::realkey", "lead", "lead_1", fp, now),
        )
        conn.commit()
        conn.close()

        db_module.init_db(self.db_path)

        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(n, 1)


class ConfirmedAfterInterruptionTests(RecoveryTestBase):
    def _prepare_state(self, *, legacy_table_name: str):
        conn = sqlite3.connect(self.db_path)
        self._base_schema(conn)
        conn.execute(self._legacy_table_sql(legacy_table_name))
        conn.execute(self._new_schema_table_sql())
        now = time.time()

        conn.execute(
            "INSERT INTO leads VALUES ('lead_old','chan_1','youtube',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        fp_old = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        conn.execute(
            f"INSERT INTO {legacy_table_name} VALUES (?,?,?,?,?)",
            ("chan_1::oldkey", "lead", "lead_old", fp_old, now),
        )

        conn.execute(
            "INSERT INTO leads VALUES ('lead_new','chan_2','tiktok',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        fp_new = stable_fingerprint("chan_2", "tiktok", None, None, None, None)
        conn.execute(
            "INSERT INTO idempotency_records VALUES (?,?,?,?,?,?,?,?)",
            ("lead", "create_lead", "chan_2", "", "newkey", "lead_new", fp_new, now),
        )
        conn.commit()
        conn.close()

    def test_new_confirmed_record_survives_recovery_v3_old_name(self):
        self._prepare_state(legacy_table_name="idempotency_records_v3_old")
        db_module.init_db(self.db_path)

        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(n, 2)

        from commercial.crm import CRM
        crm = CRM(db_path=self.db_path)
        old = crm.create_lead("chan_1", idempotency_key="oldkey", source="youtube")
        new = crm.create_lead("chan_2", idempotency_key="newkey", source="tiktok")
        self.assertEqual(old.lead_id, "lead_old")
        self.assertEqual(new.lead_id, "lead_new")
        with db_module.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"], 2)

    def test_new_confirmed_record_survives_recovery_staging_name(self):
        self._prepare_state(legacy_table_name="idempotency_records_migration_staging")
        db_module.init_db(self.db_path)

        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(n, 2)

        from commercial.crm import CRM
        crm = CRM(db_path=self.db_path)
        old = crm.create_lead("chan_1", idempotency_key="oldkey", source="youtube")
        new = crm.create_lead("chan_2", idempotency_key="newkey", source="tiktok")
        self.assertEqual(old.lead_id, "lead_old")
        self.assertEqual(new.lead_id, "lead_new")

    def test_two_simultaneous_legacy_tables_both_merged(self):
        conn = sqlite3.connect(self.db_path)
        self._base_schema(conn)
        conn.execute(self._legacy_table_sql("idempotency_records_v3_old"))
        conn.execute(self._legacy_table_sql("idempotency_records_migration_staging"))
        now = time.time()
        conn.execute(
            "INSERT INTO leads VALUES ('lead_1','chan_1','youtube',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO leads VALUES ('lead_2','chan_2','tiktok',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        fp1 = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        fp2 = stable_fingerprint("chan_2", "tiktok", None, None, None, None)
        conn.execute(
            "INSERT INTO idempotency_records_v3_old VALUES (?,?,?,?,?)", ("chan_1::key1", "lead", "lead_1", fp1, now)
        )
        conn.execute(
            "INSERT INTO idempotency_records_migration_staging VALUES (?,?,?,?,?)",
            ("chan_2::key2", "lead", "lead_2", fp2, now),
        )
        conn.commit()
        conn.close()

        db_module.init_db(self.db_path)

        with db_module.connect(self.db_path) as conn:
            for name in ("idempotency_records_v3_old", "idempotency_records_migration_staging"):
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).fetchone()
                self.assertIsNone(row)
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(n, 2)


class ConflictBetweenSourcesTests(RecoveryTestBase):
    def test_incompatible_identity_blocks_recovery_preserving_all_sources(self):
        conn = sqlite3.connect(self.db_path)
        self._base_schema(conn)
        conn.execute(self._legacy_table_sql("idempotency_records_v3_old"))
        conn.execute(self._new_schema_table_sql())
        now = time.time()

        conn.execute(
            "INSERT INTO leads VALUES ('lead_A','chan_1','youtube',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO leads VALUES ('lead_B','chan_1','tiktok',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        fp_a = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        fp_b = stable_fingerprint("chan_1", "tiktok", None, None, None, None)
        conn.execute(
            "INSERT INTO idempotency_records_v3_old VALUES (?,?,?,?,?)",
            ("chan_1::samekey", "lead", "lead_A", fp_a, now),
        )
        conn.execute(
            "INSERT INTO idempotency_records VALUES (?,?,?,?,?,?,?,?)",
            ("lead", "create_lead", "chan_1", "", "samekey", "lead_B", fp_b, now),
        )
        conn.commit()
        conn.close()

        with self.assertRaises(db_module.MigrationBlocked):
            db_module.init_db(self.db_path)

        with db_module.connect(self.db_path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("idempotency_records_v3_old", tables)
            old_count = conn.execute("SELECT COUNT(*) as n FROM idempotency_records_v3_old").fetchone()["n"]
            new_count = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
            leads_count = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
        self.assertEqual(old_count, 1)
        self.assertEqual(new_count, 1)
        self.assertEqual(leads_count, 2)

    def test_recovery_possible_after_conflict_free_state(self):
        conn = sqlite3.connect(self.db_path)
        self._base_schema(conn)
        conn.execute(self._legacy_table_sql("idempotency_records_v3_old"))
        conn.execute(self._new_schema_table_sql())
        now = time.time()
        conn.execute(
            "INSERT INTO leads VALUES ('lead_A','chan_1','youtube',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        fp_a = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        conn.execute(
            "INSERT INTO idempotency_records_v3_old VALUES (?,?,?,?,?)",
            ("chan_1::samekey", "lead", "lead_A", fp_a, now),
        )
        conn.commit()
        conn.close()

        db_module.init_db(self.db_path)
        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(n, 1)


class RecoveryFailureInjectionTests(RecoveryTestBase):
    def _prepare_simple_v4_state(self):
        conn = sqlite3.connect(self.db_path)
        self._base_schema(conn)
        conn.execute(self._legacy_table_sql("idempotency_records_v3_old"))
        now = time.time()
        conn.execute(
            "INSERT INTO leads VALUES ('lead_1','chan_1','youtube',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        fp = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        conn.execute(
            "INSERT INTO idempotency_records_v3_old VALUES (?,?,?,?,?)",
            ("chan_1::key1", "lead", "lead_1", fp, now),
        )
        conn.commit()
        conn.close()

    def test_failure_during_copy_rolls_back_and_preserves_sources(self):
        self._prepare_simple_v4_state()
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _inject_failure(f"INSERT INTO {db_module._REBUILD_TABLE_NAME}")
        try:
            with self.assertRaises(RuntimeError):
                db_module.init_db(self.db_path)
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("idempotency_records_v3_old", tables)
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records_v3_old").fetchone()["n"]
        self.assertEqual(n, 1)

        db_module.init_db(self.db_path)
        from commercial.crm import CRM
        crm = CRM(db_path=self.db_path)
        same = crm.create_lead("chan_1", idempotency_key="key1", source="youtube")
        self.assertEqual(same.lead_id, "lead_1")
        with db_module.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"], 1)

    def test_failure_during_table_swap_rolls_back_and_preserves_sources(self):
        self._prepare_simple_v4_state()
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _inject_failure(
            f"ALTER TABLE {db_module._REBUILD_TABLE_NAME} RENAME TO idempotency_records"
        )
        try:
            with self.assertRaises(RuntimeError):
                db_module.init_db(self.db_path)
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("idempotency_records_v3_old", tables)
            self.assertNotIn(db_module._REBUILD_TABLE_NAME, tables)

        db_module.init_db(self.db_path)
        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_failure_on_commit_rolls_back_closes_connection_other_writer_succeeds(self):
        self._prepare_simple_v4_state()
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = _inject_failure("__COMMIT__")
        try:
            with self.assertRaises(RuntimeError):
                db_module.init_db(self.db_path)
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("idempotency_records_v3_old", tables)

        db_module.init_db(self.db_path)
        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_two_consecutive_initializations_are_safe(self):
        self._prepare_simple_v4_state()
        db_module.init_db(self.db_path)
        db_module.init_db(self.db_path)
        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(n, 1)


class AllDomainsPreservationTests(RecoveryTestBase):
    def test_all_four_domains_preserved_with_confirmed_new_records_mixed_in(self):
        conn = sqlite3.connect(self.db_path)
        self._base_schema(conn)
        conn.execute(self._legacy_table_sql("idempotency_records_v3_old"))
        conn.execute(self._new_schema_table_sql())
        now = time.time()

        conn.execute(
            "INSERT INTO leads VALUES ('lead_1','chan_1','youtube',NULL,NULL,NULL,NULL,'NEW','UNKNOWN','UNKNOWN',?,?)",
            (now, now),
        )
        fp_lead = stable_fingerprint("chan_1", "youtube", None, None, None, None)
        conn.execute(
            "INSERT INTO idempotency_records_v3_old VALUES (?,?,?,?,?)",
            ("chan_1::leadkey", "lead", "lead_1", fp_lead, now),
        )

        conn.execute("INSERT INTO sales_events VALUES ('sevt_1','chan_1',NULL,NULL,'sale','REAL',100.0,'BRL',?)", (now,))
        fp_sale = stable_fingerprint("chan_1", "sale", "REAL", 100.0, None, None)
        conn.execute(
            "INSERT INTO idempotency_records VALUES (?,?,?,?,?,?,?,?)",
            ("sales_event", "record_sales_event", "chan_1", "", "salekey", "sevt_1", fp_sale, now),
        )

        conn.execute("INSERT INTO finance_entries VALUES ('fin_1',NULL,'revenue',50.0,'BRL','',?)", (now,))
        fp_fin = stable_fingerprint("GLOBAL", "revenue", 50.0, "")
        conn.execute(
            "INSERT INTO idempotency_records_v3_old VALUES (?,?,?,?,?)",
            ("GLOBAL::finkey", "finance_entry", "fin_1", fp_fin, now),
        )

        conn.execute(
            "INSERT INTO jobs VALUES ('job_1','proj_1','chan_1','produce_video','QUEUED','jobkey',0,3,'{}',NULL,NULL,?,?)",
            (now, now),
        )
        fp_job = stable_fingerprint("produce_video", "chan_1", "proj_1", "{}")
        conn.execute(
            "INSERT INTO idempotency_records VALUES (?,?,?,?,?,?,?,?)",
            ("job", "produce_video", "chan_1", "proj_1", "jobkey", "job_1", fp_job, now),
        )

        conn.commit()
        conn.close()

        db_module.init_db(self.db_path)

        from commercial.crm import CRM
        from commercial.finance import FinanceLedger
        from commercial.sales_tracking import SalesTracker
        from core.jobs.queue import JobQueue

        self.assertEqual(CRM(db_path=self.db_path).get("lead_1").channel_id, "chan_1")
        self.assertEqual(SalesTracker(db_path=self.db_path).summarize("chan_1").real_revenue, 100.0)
        self.assertEqual(FinanceLedger(db_path=self.db_path).snapshot(None).revenue, 50.0)
        self.assertEqual(JobQueue(db_path=self.db_path).get("job_1").job_type, "produce_video")

        with db_module.connect(self.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(n, 4)


if __name__ == "__main__":
    unittest.main()
