import unittest

from commercial.sales_tracking import SalesTracker
from core.models.entities import ValueType

"""
Testes de regressão — MICROCORREÇÃO 2, item 4 (INTEGRIDADE COMERCIAL).

Falha original: um evento `refund` com `value_type=REAL` era somado ao
MESMO acumulador que `sale`, inflando a receita em vez de abatê-la.
Uma venda REAL de 100 + reembolso REAL de 100 resultava em 200 (soma),
não em 0 (líquido). Estes testes travam o comportamento correto.
"""


class RevenueIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tracker = SalesTracker()

    def test_full_refund_zeroes_out_net_revenue(self):
        self.tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="sale-1"
        )
        self.tracker.record_event(
            channel_id="c1", event_type="refund", value_type=ValueType.REAL, amount=100, idempotency_key="refund-1"
        )
        summary = self.tracker.summarize("c1")
        self.assertEqual(summary.gross_revenue_real, 100)
        self.assertEqual(summary.refunds_amount_real, 100)
        self.assertEqual(summary.real_revenue, 0)  # NUNCA 200

    def test_partial_refund_reduces_net_revenue_correctly(self):
        self.tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="sale-1"
        )
        self.tracker.record_event(
            channel_id="c1", event_type="refund", value_type=ValueType.REAL, amount=30, idempotency_key="refund-1"
        )
        summary = self.tracker.summarize("c1")
        self.assertEqual(summary.real_revenue, 70)

    def test_no_refund_net_equals_gross(self):
        self.tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=250, idempotency_key="sale-1"
        )
        summary = self.tracker.summarize("c1")
        self.assertEqual(summary.real_revenue, 250)
        self.assertEqual(summary.gross_revenue_real, summary.real_revenue)

    def test_multiple_sales_and_refunds_net_correctly(self):
        self.tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="s1"
        )
        self.tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=200, idempotency_key="s2"
        )
        self.tracker.record_event(
            channel_id="c1", event_type="refund", value_type=ValueType.REAL, amount=50, idempotency_key="r1"
        )
        summary = self.tracker.summarize("c1")
        self.assertEqual(summary.gross_revenue_real, 300)
        self.assertEqual(summary.refunds_amount_real, 50)
        self.assertEqual(summary.real_revenue, 250)

    def test_estimated_and_forecast_refunds_kept_separate_from_real(self):
        self.tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="s1"
        )
        self.tracker.record_event(
            channel_id="c1", event_type="refund", value_type=ValueType.ESTIMATED, amount=999, idempotency_key="r1"
        )
        summary = self.tracker.summarize("c1")
        # reembolso ESTIMATED nunca deve tocar a receita REAL
        self.assertEqual(summary.real_revenue, 100)
        self.assertEqual(summary.estimated_revenue, -999)

    def test_refund_exceeding_sale_reflects_negative_net_not_clamped(self):
        # cenário legítimo (ex: correção de cobrança indevida) — o
        # indicador deve refletir a realidade, não esconder um negativo.
        self.tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=50, idempotency_key="s1"
        )
        self.tracker.record_event(
            channel_id="c1", event_type="refund", value_type=ValueType.REAL, amount=80, idempotency_key="r1"
        )
        summary = self.tracker.summarize("c1")
        self.assertEqual(summary.real_revenue, -30)

    def test_refund_never_inflates_gross_sale_accumulator(self):
        self.tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="s1"
        )
        self.tracker.record_event(
            channel_id="c1", event_type="refund", value_type=ValueType.REAL, amount=100, idempotency_key="r1"
        )
        summary = self.tracker.summarize("c1")
        # a falha original inflava gross_revenue_real para 200 ao somar
        # o refund no mesmo acumulador do sale
        self.assertEqual(summary.gross_revenue_real, 100)

    def test_channel_isolation_of_revenue_unaffected_by_fix(self):
        self.tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="s1"
        )
        self.tracker.record_event(
            channel_id="c2", event_type="sale", value_type=ValueType.REAL, amount=500, idempotency_key="s2"
        )
        self.assertEqual(self.tracker.summarize("c1").real_revenue, 100)
        self.assertEqual(self.tracker.summarize("c2").real_revenue, 500)


if __name__ == "__main__":
    unittest.main()
