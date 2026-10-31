import unittest

from atlas.director import AtlasDirector
from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.models.entities import ValueType
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role

"""
Testes ATLAS - EVIDENCIA E CLASSIFICACAO DE DADOS, e comportamento
diante de dados AUSENTES ou CONFLITANTES.
"""


def make_atlas(scopes=None):
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes=scopes or {"*"})
    return AtlasDirector(facade=facade, principal=principal), facade, audit


OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


class EvidenceSeparationTests(unittest.TestCase):
    def test_real_revenue_never_mixed_with_forecast(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100
        )
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=99999
        )
        report = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report.real_revenue, 100)
        self.assertEqual(report.forecast_revenue, 99999)

    def test_estimated_kept_separate_from_real(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.ESTIMATED, amount=500
        )
        report = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report.real_revenue, 0)
        self.assertEqual(report.estimated_revenue, 500)

    def test_sale_counts_separated_by_evidence_type(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=10)
        facade.record_sales_event(OWNER, channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=20)
        report = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report.sales_real, 1)
        self.assertEqual(report.sales_forecast, 1)


class MissingDataTests(unittest.TestCase):
    def test_no_data_at_all_yields_unknown_not_invented_numbers(self):
        atlas, facade, _ = make_atlas()
        report = atlas.analyze_performance(channel_id="empty_channel")
        self.assertEqual(report.real_revenue, 0)
        self.assertEqual(report.cac, "UNKNOWN")
        self.assertEqual(report.ltv, "UNKNOWN")
        self.assertEqual(report.roi, "UNKNOWN")
        # margem soma zero lançamentos financeiros -> 0.0 (zero
        # efetivamente computado, consistente com FinanceSnapshot em todo
        # o resto do sistema desde a Microcorreção 3) — não é "UNKNOWN"
        # porque a consulta em si teve sucesso, só não havia lançamentos.
        self.assertEqual(report.margin, 0.0)

    def test_conversion_rate_unknown_without_clicks_never_zero(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=10
        )
        report = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report.conversion_rate, "UNKNOWN")

    def test_conversion_rate_computed_when_clicks_present(self):
        atlas, facade, _ = make_atlas()
        for _ in range(10):
            facade.record_sales_event(OWNER, channel_id="c1", event_type="click")
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=10
        )
        report = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report.conversion_rate, 0.1)

    def test_cac_computed_only_when_inputs_supplied(self):
        atlas, facade, _ = make_atlas()
        report_without = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report_without.cac, "UNKNOWN")

        report_with = atlas.analyze_performance(channel_id="c1", marketing_spend=500, new_customers=10)
        self.assertEqual(report_with.cac, 50.0)

    def test_ltv_computed_only_when_inputs_supplied(self):
        atlas, facade, _ = make_atlas()
        report = atlas.analyze_performance(
            channel_id="c1", avg_revenue_per_customer=100, avg_customer_lifespan_periods=3
        )
        self.assertEqual(report.ltv, 300.0)

    def test_roi_unknown_without_realized_cost(self):
        atlas, facade, _ = make_atlas()
        facade.record_finance_entry(OWNER, entry_type="revenue", amount=500, channel_id="c1")
        report = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report.roi, "UNKNOWN")

    def test_roi_computed_when_realized_cost_present(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=200
        )
        facade.record_finance_entry(OWNER, entry_type="realized_cost", amount=100, channel_id="c1")
        report = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report.roi, 1.0)

    def test_evidence_notes_explain_why_fields_are_unknown(self):
        atlas, facade, _ = make_atlas()
        report = atlas.analyze_performance(channel_id="c1")
        self.assertTrue(len(report.evidence_notes) > 0)
        self.assertTrue(any("CAC" in n for n in report.evidence_notes))


class ConflictingOrAmbiguousDataTests(unittest.TestCase):
    def test_real_sale_and_full_refund_net_to_zero_never_double_counted(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100
        )
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="refund", value_type=ValueType.REAL, amount=100
        )
        report = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report.real_revenue, 0)

    def test_mixed_evidence_scenario_all_four_types_present(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=10)
        facade.record_sales_event(OWNER, channel_id="c1", event_type="sale", value_type=ValueType.ESTIMATED, amount=20)
        facade.record_sales_event(OWNER, channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=30)
        facade.record_sales_event(OWNER, channel_id="c1", event_type="sale", value_type=ValueType.UNKNOWN)
        report = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report.real_revenue, 10)
        self.assertEqual(report.estimated_revenue, 20)
        self.assertEqual(report.forecast_revenue, 30)
        self.assertEqual(report.sales_real, 1)

    def test_channel_scope_label_never_hidden(self):
        atlas, facade, _ = make_atlas()
        report = atlas.analyze_performance(channel_id="c1")
        self.assertEqual(report.channel_scope, "c1")


if __name__ == "__main__":
    unittest.main()
