import unittest

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.sales_tracking import SalesTracker
from core.governance.idempotency import IdempotencyConflict
from core.jobs.queue import JobQueue
from core.models.entities import ValueType

"""
Testes de regressão — MICROCORREÇÃO 2, item 1 (ISOLAMENTO).

Falha original: a idempotência indexava apenas pela `idempotency_key`
crua, sem considerar canal/projeto/operação. Reaproveitar a mesma chave
em outro canal podia retornar (ou sobrescrever) o registro do canal
errado. Estes testes travam esse comportamento definitivamente.
"""


class CRMScopedIdempotencyTests(unittest.TestCase):
    def test_same_key_different_channels_never_cross_leaks(self):
        crm = CRM()
        lead_a = crm.create_lead("chan_A", idempotency_key="retry-1", source="youtube")
        lead_b = crm.create_lead("chan_B", idempotency_key="retry-1", source="tiktok")

        self.assertNotEqual(lead_a.lead_id, lead_b.lead_id)
        self.assertEqual(lead_a.channel_id, "chan_A")
        self.assertEqual(lead_b.channel_id, "chan_B")
        self.assertEqual(len(crm.by_channel("chan_A")), 1)
        self.assertEqual(len(crm.by_channel("chan_B")), 1)

    def test_same_key_same_channel_same_content_is_idempotent(self):
        crm = CRM()
        lead1 = crm.create_lead("chan_A", idempotency_key="retry-1", source="youtube", campaign="c1")
        lead2 = crm.create_lead("chan_A", idempotency_key="retry-1", source="youtube", campaign="c1")
        self.assertEqual(lead1.lead_id, lead2.lead_id)
        self.assertEqual(len(crm.by_channel("chan_A")), 1)

    def test_same_key_same_channel_different_content_conflicts(self):
        crm = CRM()
        crm.create_lead("chan_A", idempotency_key="retry-1", source="youtube")
        with self.assertRaises(IdempotencyConflict):
            crm.create_lead("chan_A", idempotency_key="retry-1", source="tiktok")

    def test_conflict_does_not_mutate_existing_lead(self):
        crm = CRM()
        original = crm.create_lead("chan_A", idempotency_key="retry-1", source="youtube")
        try:
            crm.create_lead("chan_A", idempotency_key="retry-1", source="tiktok")
        except IdempotencyConflict:
            pass
        still_there = crm.get(original.lead_id)
        self.assertEqual(still_there.source, "youtube")
        self.assertEqual(len(crm.by_channel("chan_A")), 1)


class SalesTrackerScopedIdempotencyTests(unittest.TestCase):
    def test_same_key_different_channels_never_cross_leaks(self):
        tracker = SalesTracker()
        e1 = tracker.record_event(
            channel_id="chan_A", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="dup"
        )
        e2 = tracker.record_event(
            channel_id="chan_B", event_type="sale", value_type=ValueType.REAL, amount=250, idempotency_key="dup"
        )
        self.assertNotEqual(e1.event_id, e2.event_id)
        self.assertEqual(tracker.summarize("chan_A").real_revenue, 100)
        self.assertEqual(tracker.summarize("chan_B").real_revenue, 250)

    def test_same_key_same_channel_different_amount_conflicts(self):
        tracker = SalesTracker()
        tracker.record_event(
            channel_id="chan_A", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="dup"
        )
        with self.assertRaises(IdempotencyConflict):
            tracker.record_event(
                channel_id="chan_A", event_type="sale", value_type=ValueType.REAL, amount=999, idempotency_key="dup"
            )

    def test_same_key_same_channel_different_event_type_conflicts(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="chan_A", event_type="click", idempotency_key="dup")
        with self.assertRaises(IdempotencyConflict):
            tracker.record_event(channel_id="chan_A", event_type="view", idempotency_key="dup")


class FinanceLedgerScopedIdempotencyTests(unittest.TestCase):
    def test_same_key_different_channels_never_cross_leaks(self):
        ledger = FinanceLedger()
        e1 = ledger.record(entry_type="revenue", amount=100, channel_id="chan_A", idempotency_key="dup")
        e2 = ledger.record(entry_type="revenue", amount=500, channel_id="chan_B", idempotency_key="dup")
        self.assertNotEqual(e1.entry_id, e2.entry_id)
        self.assertEqual(ledger.snapshot("chan_A").revenue, 100)
        self.assertEqual(ledger.snapshot("chan_B").revenue, 500)

    def test_channel_scope_and_global_scope_are_distinct(self):
        ledger = FinanceLedger()
        e_channel = ledger.record(entry_type="expense", amount=10, channel_id="chan_A", idempotency_key="dup")
        e_global = ledger.record(entry_type="expense", amount=999, channel_id=None, idempotency_key="dup")
        self.assertNotEqual(e_channel.entry_id, e_global.entry_id)
        self.assertEqual(ledger.snapshot("chan_A").expenses, 10)
        self.assertEqual(ledger.snapshot(None).expenses, 1009)  # 10 + 999 no agregado global

    def test_same_key_same_scope_different_amount_conflicts(self):
        ledger = FinanceLedger()
        ledger.record(entry_type="commission", amount=30, channel_id="chan_A", idempotency_key="dup")
        with self.assertRaises(IdempotencyConflict):
            ledger.record(entry_type="commission", amount=99, channel_id="chan_A", idempotency_key="dup")


class JobQueueScopedIdempotencyTests(unittest.TestCase):
    def test_same_key_different_channels_never_cross_leaks(self):
        queue = JobQueue()
        job_a = queue.enqueue(job_type="produce_video", idempotency_key="dup", channel_id="chan_A")
        job_b = queue.enqueue(job_type="produce_video", idempotency_key="dup", channel_id="chan_B")
        self.assertNotEqual(job_a.job_id, job_b.job_id)
        self.assertEqual(job_a.channel_id, "chan_A")
        self.assertEqual(job_b.channel_id, "chan_B")

    def test_same_key_different_job_types_never_cross_leaks(self):
        queue = JobQueue()
        job_a = queue.enqueue(job_type="produce_video", idempotency_key="dup", channel_id="chan_A")
        job_b = queue.enqueue(job_type="publish_content", idempotency_key="dup", channel_id="chan_A")
        self.assertNotEqual(job_a.job_id, job_b.job_id)

    def test_same_key_same_scope_same_payload_is_idempotent(self):
        queue = JobQueue()
        job1 = queue.enqueue(
            job_type="produce_video", idempotency_key="dup", channel_id="chan_A", payload={"script": "v1"}
        )
        job2 = queue.enqueue(
            job_type="produce_video", idempotency_key="dup", channel_id="chan_A", payload={"script": "v1"}
        )
        self.assertEqual(job1.job_id, job2.job_id)

    def test_same_key_same_scope_different_payload_conflicts(self):
        queue = JobQueue()
        queue.enqueue(job_type="produce_video", idempotency_key="dup", channel_id="chan_A", payload={"script": "v1"})
        with self.assertRaises(IdempotencyConflict):
            queue.enqueue(
                job_type="produce_video", idempotency_key="dup", channel_id="chan_A", payload={"script": "v2"}
            )

    def test_by_idempotency_key_respects_scope(self):
        queue = JobQueue()
        job_a = queue.enqueue(job_type="produce_video", idempotency_key="dup", channel_id="chan_A")
        found = queue.by_idempotency_key("dup", job_type="produce_video", channel_id="chan_A")
        not_found = queue.by_idempotency_key("dup", job_type="produce_video", channel_id="chan_B")
        self.assertEqual(found.job_id, job_a.job_id)
        self.assertIsNone(not_found)


if __name__ == "__main__":
    unittest.main()
