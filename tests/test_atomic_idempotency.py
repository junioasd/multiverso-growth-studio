import os
import sqlite3
import tempfile
import threading
import unittest

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.sales_tracking import SalesTracker
from core.governance.idempotency import IdempotencyConflict
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.models.entities import ValueType

"""
Testes de regressão — MICROCORREÇÃO 3, item 2
(PERSISTÊNCIA ATÔMICA E IDEMPOTÊNCIA).

Falha original: era possível duplicar leads (a) com duas instâncias
conectadas ao mesmo banco usando a mesma chave, e (b) após falha entre
a gravação do registro e a gravação da idempotência, seguida de
reinicialização e retry.

Corrigido gravando o registro de negócio + sua idempotência na MESMA
transação SQLite (`BEGIN IMMEDIATE` ... `COMMIT`), com `INSERT` puro
(nunca `INSERT OR REPLACE`) no registro de idempotência — um conflito
de PRIMARY KEY reverte a transação inteira, e a resolução do conflito
sempre consulta o banco, nunca o cache em memória.
"""


class FailureInjectingConnection(sqlite3.Connection):
    """Conexão que levanta uma exceção na N-ésima chamada de execute()
    que casar com `trigger_sql_substring` — simula um crash do processo
    exatamente entre as duas gravações da transação atômica."""

    trigger_sql_substring = ""
    fail_on_call_number = 1
    _call_count = 0

    def execute(self, sql, params=()):
        if self.trigger_sql_substring and self.trigger_sql_substring in sql:
            type(self)._call_count += 1
            if type(self)._call_count == self.fail_on_call_number:
                raise RuntimeError("falha simulada (crash) entre as duas gravações da transação")
        return super().execute(sql, params)

    @classmethod
    def reset(cls):
        cls._call_count = 0


def _make_failing_autocommit_factory(trigger_sql_substring: str):
    FailureInjectingConnection.trigger_sql_substring = trigger_sql_substring
    FailureInjectingConnection.reset()

    def factory(db_path: str):
        conn = sqlite3.connect(db_path, isolation_level=None, factory=FailureInjectingConnection)
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


class CrossInstanceIdempotencyTests(PersistenceTestBase):
    """Duas instâncias apontando para o mesmo arquivo de banco (o
    cenário relevante é que nenhuma tem visibilidade do cache da
    outra) usando a mesma chave nunca duplicam o registro."""

    def test_crm_two_instances_same_key_no_duplication(self):
        crm1 = CRM(db_path=self.db_path)
        crm2 = CRM(db_path=self.db_path)

        lead1 = crm1.create_lead("chan_1", idempotency_key="dup", source="youtube")
        lead2 = crm2.create_lead("chan_1", idempotency_key="dup", source="youtube")

        self.assertEqual(lead1.lead_id, lead2.lead_id)
        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_crm_two_instances_same_key_incompatible_content_conflicts(self):
        crm1 = CRM(db_path=self.db_path)
        crm2 = CRM(db_path=self.db_path)

        crm1.create_lead("chan_1", idempotency_key="dup", source="youtube")
        with self.assertRaises(IdempotencyConflict):
            crm2.create_lead("chan_1", idempotency_key="dup", source="tiktok")

        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_sales_tracker_two_instances_same_key_no_duplication(self):
        t1 = SalesTracker(db_path=self.db_path)
        t2 = SalesTracker(db_path=self.db_path)

        e1 = t1.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="dup"
        )
        e2 = t2.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="dup"
        )
        self.assertEqual(e1.event_id, e2.event_id)
        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM sales_events").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_finance_ledger_two_instances_same_key_no_duplication(self):
        f1 = FinanceLedger(db_path=self.db_path)
        f2 = FinanceLedger(db_path=self.db_path)

        e1 = f1.record(entry_type="revenue", amount=100, channel_id="c1", idempotency_key="dup")
        e2 = f2.record(entry_type="revenue", amount=100, channel_id="c1", idempotency_key="dup")
        self.assertEqual(e1.entry_id, e2.entry_id)
        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM finance_entries").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_job_queue_two_instances_same_key_no_duplication(self):
        q1 = JobQueue(db_path=self.db_path)
        q2 = JobQueue(db_path=self.db_path)

        j1 = q1.enqueue(job_type="produce_video", idempotency_key="dup", channel_id="c1")
        j2 = q2.enqueue(job_type="produce_video", idempotency_key="dup", channel_id="c1")
        self.assertEqual(j1.job_id, j2.job_id)
        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM jobs").fetchone()["n"]
        self.assertEqual(count, 1)


class ConcurrentDurableWriteTests(PersistenceTestBase):
    """Concorrência real de threads sobre o MESMO arquivo de banco
    (cada thread com sua própria instância de serviço)."""

    def _run_concurrently(self, fn, n_threads: int):
        results, errors = [None] * n_threads, [None] * n_threads

        def worker(i):
            try:
                results[i] = fn(i)
            except Exception as exc:  # noqa: BLE001
                errors[i] = exc

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results, errors

    def test_concurrent_durable_lead_creation_same_key_single_row(self):
        def attempt(i):
            crm = CRM(db_path=self.db_path)
            return crm.create_lead("chan_1", idempotency_key="race", source="youtube")

        results, errors = self._run_concurrently(attempt, 12)
        succeeded = [r for r in results if r is not None]
        self.assertTrue(all(e is None for e in errors))
        ids = {r.lead_id for r in succeeded}
        self.assertEqual(len(ids), 1)
        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_concurrent_durable_job_enqueue_same_key_single_row(self):
        def attempt(i):
            queue = JobQueue(db_path=self.db_path)
            return queue.enqueue(job_type="produce_video", idempotency_key="race", channel_id="c1")

        results, errors = self._run_concurrently(attempt, 12)
        self.assertTrue(all(e is None for e in errors))
        ids = {r.job_id for r in results if r is not None}
        self.assertEqual(len(ids), 1)
        with db_module.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as n FROM jobs").fetchone()["n"]
        self.assertEqual(count, 1)


class InjectedFailureRollbackTests(PersistenceTestBase):
    """Injeta uma falha EXATAMENTE entre a gravação do registro de
    negócio e a gravação da idempotência, dentro da mesma transação, e
    confirma: (1) nenhum registro parcial fica visível no banco, (2)
    retry após 'reinicialização' funciona normalmente e cria
    exatamente um registro."""

    def test_crm_rollback_on_failure_between_writes_then_retry(self):
        factory = _make_failing_autocommit_factory("INSERT INTO idempotency_records")
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = lambda path: factory(path)
        try:
            crm = CRM(db_path=self.db_path)
            with self.assertRaises(RuntimeError):
                crm.create_lead("chan_1", idempotency_key="dup", source="youtube")
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            leads = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
            idem = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(leads, 0, "não pode haver lead órfão após rollback")
        self.assertEqual(idem, 0)

        crm2 = CRM(db_path=self.db_path)
        lead = crm2.create_lead("chan_1", idempotency_key="dup", source="youtube")
        self.assertIsNotNone(lead.lead_id)
        with db_module.connect(self.db_path) as conn:
            leads = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
        self.assertEqual(leads, 1)

    def test_sales_tracker_rollback_on_failure_between_writes_then_retry(self):
        factory = _make_failing_autocommit_factory("INSERT INTO idempotency_records")
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = lambda path: factory(path)
        try:
            tracker = SalesTracker(db_path=self.db_path)
            with self.assertRaises(RuntimeError):
                tracker.record_event(
                    channel_id="c1", event_type="sale", value_type=ValueType.REAL,
                    amount=100, idempotency_key="dup",
                )
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            events = conn.execute("SELECT COUNT(*) as n FROM sales_events").fetchone()["n"]
        self.assertEqual(events, 0)

        tracker2 = SalesTracker(db_path=self.db_path)
        event = tracker2.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="dup"
        )
        self.assertIsNotNone(event.event_id)
        with db_module.connect(self.db_path) as conn:
            events = conn.execute("SELECT COUNT(*) as n FROM sales_events").fetchone()["n"]
        self.assertEqual(events, 1)

    def test_finance_rollback_on_failure_between_writes_then_retry(self):
        factory = _make_failing_autocommit_factory("INSERT INTO idempotency_records")
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = lambda path: factory(path)
        try:
            ledger = FinanceLedger(db_path=self.db_path)
            with self.assertRaises(RuntimeError):
                ledger.record(entry_type="revenue", amount=100, channel_id="c1", idempotency_key="dup")
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            entries = conn.execute("SELECT COUNT(*) as n FROM finance_entries").fetchone()["n"]
        self.assertEqual(entries, 0)

        ledger2 = FinanceLedger(db_path=self.db_path)
        entry = ledger2.record(entry_type="revenue", amount=100, channel_id="c1", idempotency_key="dup")
        self.assertIsNotNone(entry.entry_id)

    def test_job_queue_rollback_on_failure_between_writes_then_retry(self):
        factory = _make_failing_autocommit_factory("INSERT INTO idempotency_records")
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = lambda path: factory(path)
        try:
            queue = JobQueue(db_path=self.db_path)
            with self.assertRaises(RuntimeError):
                queue.enqueue(job_type="produce_video", idempotency_key="dup", channel_id="c1")
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            jobs = conn.execute("SELECT COUNT(*) as n FROM jobs").fetchone()["n"]
        self.assertEqual(jobs, 0)

        queue2 = JobQueue(db_path=self.db_path)
        job = queue2.enqueue(job_type="produce_video", idempotency_key="dup", channel_id="c1")
        self.assertIsNotNone(job.job_id)
        with db_module.connect(self.db_path) as conn:
            jobs = conn.execute("SELECT COUNT(*) as n FROM jobs").fetchone()["n"]
        self.assertEqual(jobs, 1)

    def test_failure_on_business_row_insert_itself_also_leaves_nothing(self):
        """Falha na PRIMEIRA gravação (o registro de negócio em si, antes
        de chegar na idempotência) — também não pode deixar nada."""
        factory = _make_failing_autocommit_factory("INSERT INTO leads")
        original = db_module.get_autocommit_connection
        db_module.get_autocommit_connection = lambda path: factory(path)
        try:
            crm = CRM(db_path=self.db_path)
            with self.assertRaises(RuntimeError):
                crm.create_lead("chan_1", idempotency_key="dup", source="youtube")
        finally:
            db_module.get_autocommit_connection = original

        with db_module.connect(self.db_path) as conn:
            leads = conn.execute("SELECT COUNT(*) as n FROM leads").fetchone()["n"]
            idem = conn.execute("SELECT COUNT(*) as n FROM idempotency_records").fetchone()["n"]
        self.assertEqual(leads, 0)
        self.assertEqual(idem, 0)


class NoInMemoryOnlyRelianceTests(PersistenceTestBase):
    """Confirma que a resolução de conflito consulta o BANCO, não
    apenas o cache local — simulando uma instância cujo cache está
    desatualizado em relação ao que outra instância já persistiu."""

    def test_second_instance_with_stale_cache_still_detects_conflict_from_db(self):
        # crm2 é criada ANTES de crm1 escrever — seu cache local nasce
        # vazio e nunca é atualizado localmente pela escrita de crm1.
        crm2 = CRM(db_path=self.db_path)
        self.assertEqual(len(crm2._idempotency_index), 0)  # cache local realmente vazio

        crm1 = CRM(db_path=self.db_path)
        crm1.create_lead("chan_1", idempotency_key="dup", source="youtube")

        # mesmo assim, crm2 detecta o conflito consultando o BANCO na
        # hora — nunca confiando apenas no próprio cache desatualizado.
        with self.assertRaises(IdempotencyConflict):
            crm2.create_lead("chan_1", idempotency_key="dup", source="tiktok")

    def test_second_instance_with_stale_cache_returns_existing_on_same_content(self):
        crm2 = CRM(db_path=self.db_path)  # criada antes, cache vazio

        crm1 = CRM(db_path=self.db_path)
        original_lead = crm1.create_lead("chan_1", idempotency_key="dup", source="youtube")

        same_lead = crm2.create_lead("chan_1", idempotency_key="dup", source="youtube")
        self.assertEqual(same_lead.lead_id, original_lead.lead_id)


if __name__ == "__main__":
    unittest.main()
