import threading
import unittest

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.multigerenciador import MultiGerenciadorComercial
from commercial.sales_tracking import SalesTracker
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.models.entities import ValueType
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role


def run_concurrently(fn, n_threads: int) -> list:
    results = [None] * n_threads
    errors = [None] * n_threads

    def worker(i):
        try:
            results[i] = fn(i)
        except Exception as exc:  # noqa: BLE001 - queremos capturar qualquer exceção do worker
            errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


class JobQueueConcurrencyTests(unittest.TestCase):
    def test_concurrent_enqueue_same_idempotency_key_creates_one_job(self):
        queue = JobQueue()
        results, errors = run_concurrently(
            lambda i: queue.enqueue(job_type="produce_video", idempotency_key="shared-key"),
            n_threads=25,
        )
        self.assertTrue(all(e is None for e in errors))
        job_ids = {job.job_id for job in results}
        self.assertEqual(len(job_ids), 1)
        self.assertEqual(len(queue.all()), 1)

    def test_concurrent_start_respects_capacity_control(self):
        queue = JobQueue(max_concurrent_running=3)
        jobs = [queue.enqueue(job_type="t", idempotency_key=f"k{i}") for i in range(20)]

        results, errors = run_concurrently(lambda i: queue.start(jobs[i].job_id), n_threads=20)
        self.assertTrue(all(e is None for e in errors))

        running_count = sum(1 for j in queue.all() if j.state.value == "RUNNING")
        self.assertLessEqual(running_count, 3)


class CRMConcurrencyTests(unittest.TestCase):
    def test_concurrent_create_lead_same_idempotency_key_creates_one_lead(self):
        crm = CRM()
        results, errors = run_concurrently(
            lambda i: crm.create_lead("chan_1", idempotency_key="dup-lead", source="youtube"),
            n_threads=30,
        )
        self.assertTrue(all(e is None for e in errors))
        lead_ids = {lead.lead_id for lead in results}
        self.assertEqual(len(lead_ids), 1)
        self.assertEqual(len(crm.by_channel("chan_1")), 1)

    def test_concurrent_create_lead_without_key_creates_many(self):
        crm = CRM()
        run_concurrently(lambda i: crm.create_lead("chan_1", source="youtube"), n_threads=30)
        self.assertEqual(len(crm.by_channel("chan_1")), 30)


class SalesTrackerConcurrencyTests(unittest.TestCase):
    def test_concurrent_duplicate_sale_events_do_not_double_count_revenue(self):
        tracker = SalesTracker()
        results, errors = run_concurrently(
            lambda i: tracker.record_event(
                channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
                amount=100.0, idempotency_key="sale-dup",
            ),
            n_threads=30,
        )
        self.assertTrue(all(e is None for e in errors))
        summary = tracker.summarize("chan_1")
        self.assertEqual(summary.sales, 1)
        self.assertEqual(summary.real_revenue, 100.0)

    def test_concurrent_distinct_sales_all_counted(self):
        tracker = SalesTracker()
        run_concurrently(
            lambda i: tracker.record_event(
                channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
                amount=10.0, idempotency_key=f"sale-{i}",
            ),
            n_threads=30,
        )
        summary = tracker.summarize("chan_1")
        self.assertEqual(summary.sales, 30)
        self.assertEqual(summary.real_revenue, 300.0)


class FinanceConcurrencyTests(unittest.TestCase):
    def test_concurrent_duplicate_commission_entries_do_not_double_count(self):
        ledger = FinanceLedger()
        run_concurrently(
            lambda i: ledger.record(
                entry_type="commission", amount=50.0, channel_id="chan_1", idempotency_key="commission-dup"
            ),
            n_threads=25,
        )
        snap = ledger.snapshot("chan_1")
        self.assertEqual(snap.commission, 50.0)


class FacadeConcurrencyTests(unittest.TestCase):
    def test_concurrent_governed_sales_events_with_same_operation_id_only_one_succeeds(self):
        job_queue = JobQueue()
        mgc = MultiGerenciadorComercial()
        audit = AuditTrail()
        facade = GovernedFacade(job_queue=job_queue, multigerenciador=mgc, audit=audit)
        owner = Principal(subject="owner@example.com", role=Role.OWNER, scopes={"*"})

        def attempt(i):
            return facade.record_sales_event(
                owner, channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
                amount=100.0, idempotency_key="sale-x", operation_id="replay-x",
            )

        results, errors = run_concurrently(attempt, n_threads=15)
        successes = [r for r in results if r is not None]
        failures = [e for e in errors if e is not None]
        # Idempotência de domínio garante 1 evento real; a proteção de
        # replay pode rejeitar tentativas concorrentes adicionais — em
        # qualquer caso, nunca deve haver mais de uma venda real contada.
        summary = facade.sales_tracker.summarize("chan_1")
        self.assertEqual(summary.sales, 1)
        self.assertEqual(summary.real_revenue, 100.0)
        self.assertGreaterEqual(len(successes) + len(failures), 15)


if __name__ == "__main__":
    unittest.main()
