import unittest

from commercial.finance import FinanceLedger, InvalidFinanceEntryType
from commercial.sales_tracking import InvalidEventType, SalesTracker, UnverifiedAmountRejected
from core.models.entities import ValueType


class SalesTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = SalesTracker()

    def test_record_event_without_amount(self):
        event = self.tracker.record_event(channel_id="c1", event_type="click")
        self.assertEqual(event.event_type, "click")

    def test_invalid_event_type_rejected(self):
        with self.assertRaises(InvalidEventType):
            self.tracker.record_event(channel_id="c1", event_type="bogus")

    def test_amount_without_value_type_rejected(self):
        with self.assertRaises(UnverifiedAmountRejected):
            self.tracker.record_event(channel_id="c1", event_type="sale", amount=100.0)

    def test_real_sale_counted_in_summary(self):
        self.tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=150.0
        )
        summary = self.tracker.summarize("c1")
        self.assertEqual(summary.sales, 1)
        self.assertEqual(summary.real_revenue, 150.0)

    def test_estimated_and_real_kept_separate(self):
        self.tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100.0)
        self.tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.ESTIMATED, amount=50.0)
        summary = self.tracker.summarize("c1")
        self.assertEqual(summary.real_revenue, 100.0)
        self.assertEqual(summary.estimated_revenue, 50.0)

    def test_caller_cannot_fake_sale_without_declaring_type(self):
        # tentativa de "inventar" venda sem tipo declarado deve falhar sempre
        with self.assertRaises(UnverifiedAmountRejected):
            self.tracker.record_event(channel_id="c1", event_type="sale", amount=99999.0, value_type=ValueType.UNKNOWN)


class FinanceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = FinanceLedger()

    def test_margin_calculation(self):
        self.ledger.record(entry_type="revenue", amount=1000, channel_id="c1")
        self.ledger.record(entry_type="realized_cost", amount=300, channel_id="c1")
        self.ledger.record(entry_type="expense", amount=100, channel_id="c1")
        snap = self.ledger.snapshot("c1")
        self.assertEqual(snap.margin, 600)

    def test_invalid_entry_type_rejected(self):
        with self.assertRaises(InvalidFinanceEntryType):
            self.ledger.record(entry_type="bogus", amount=10)

    def test_cac_unknown_with_zero_customers(self):
        self.assertEqual(self.ledger.cac(marketing_spend=500, new_customers=0), "UNKNOWN")

    def test_roi_unknown_with_zero_cost(self):
        self.assertEqual(self.ledger.roi(gain=100, cost=0), "UNKNOWN")

    def test_roi_calculated_normally(self):
        roi = self.ledger.roi(gain=200, cost=100)
        self.assertEqual(roi, 1.0)


if __name__ == "__main__":
    unittest.main()
