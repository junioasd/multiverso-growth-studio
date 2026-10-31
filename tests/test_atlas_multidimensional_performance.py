import unittest

from atlas.director import AtlasDirector
from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.models.entities import ValueType
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role

"""
Testes ATLAS+RADAR - DESEMPENHO POR PRODUTO/OFERTA/CAMPANHA/FUNIL,
CRUZAMENTO PRODUTO X CANAL / CAMPANHA X CANAL, METRICAS AUSENTES E
CONFLITANTES.
"""


def make_atlas(scopes=None):
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes=scopes or {"*"})
    return AtlasDirector(facade=facade, principal=principal), facade, audit


OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


class DimensionalPerformanceTests(unittest.TestCase):
    def test_performance_by_product(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, product_id="prod_A",
        )
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=200, product_id="prod_B",
        )
        report_a = atlas.analyze_performance_multidimensional(channel_id="c1", product_id="prod_A")
        self.assertEqual(report_a.real_revenue, 100)

    def test_performance_by_campaign(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=50, campaign_id="camp_X",
        )
        report = atlas.analyze_performance_multidimensional(channel_id="c1", campaign_id="camp_X")
        self.assertEqual(report.real_revenue, 50)

    def test_performance_by_funnel(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=75, funnel_id="funnel_1",
        )
        report = atlas.analyze_performance_multidimensional(channel_id="c1", funnel_id="funnel_1")
        self.assertEqual(report.real_revenue, 75)

    def test_performance_by_platform(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=60, platform="youtube",
        )
        report = atlas.analyze_performance_multidimensional(channel_id="c1", platform="youtube")
        self.assertEqual(report.real_revenue, 60)

    def test_performance_by_traffic_source(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=90, traffic_source="organic",
        )
        report = atlas.analyze_performance_multidimensional(channel_id="c1", traffic_source="organic")
        self.assertEqual(report.real_revenue, 90)

    def test_performance_by_offer_via_offer_id_still_supported_at_summary_level(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=40, offer_id="offer_1",
        )
        report = atlas.analyze_performance_multidimensional(channel_id="c1")
        self.assertEqual(report.real_revenue, 40)


class CrossDimensionalTests(unittest.TestCase):
    def test_product_x_channel_breakdown(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, product_id="prod_A",
        )
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=50, product_id="prod_B",
        )
        breakdown = atlas.breakdown_performance(group_by="product_id", channel_id="c1")
        self.assertEqual(breakdown["prod_A"].real_revenue, 100)
        self.assertEqual(breakdown["prod_B"].real_revenue, 50)

    def test_campaign_x_channel_breakdown(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=300, campaign_id="camp_1",
        )
        breakdown = atlas.breakdown_performance(group_by="campaign_id", channel_id="c1")
        self.assertEqual(breakdown["camp_1"].real_revenue, 300)

    def test_product_x_campaign_via_fixed_filter(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=10,
            product_id="prod_A", campaign_id="camp_1",
        )
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=20,
            product_id="prod_B", campaign_id="camp_1",
        )
        breakdown = atlas.breakdown_performance(group_by="product_id", channel_id="c1", campaign_id="camp_1")
        self.assertEqual(breakdown["prod_A"].real_revenue, 10)
        self.assertEqual(breakdown["prod_B"].real_revenue, 20)

    def test_breakdown_never_invents_categories_not_registered(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=10, product_id="prod_only",
        )
        breakdown = atlas.breakdown_performance(group_by="product_id", channel_id="c1")
        self.assertEqual(set(breakdown.keys()), {"prod_only"})

    def test_platform_x_revenue_breakdown(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, platform="youtube",
        )
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=50, platform="tiktok",
        )
        breakdown = atlas.breakdown_performance(group_by="platform", channel_id="c1")
        self.assertEqual(breakdown["youtube"].real_revenue, 100)
        self.assertEqual(breakdown["tiktok"].real_revenue, 50)

    def test_traffic_source_x_conversion_breakdown(self):
        atlas, facade, _ = make_atlas()
        for _ in range(10):
            facade.record_sales_event(OWNER, channel_id="c1", event_type="click", traffic_source="organic")
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=1, traffic_source="organic",
        )
        breakdown = atlas.breakdown_performance(group_by="traffic_source", channel_id="c1")
        self.assertEqual(breakdown["organic"].conversion_rate, 0.1)

    def test_isolation_preserved_in_breakdown(self):
        atlas_scoped, facade, _ = make_atlas({"channel:c1"})
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=10, product_id="p1",
        )
        facade.record_sales_event(
            OWNER, channel_id="c2", event_type="sale", value_type=ValueType.REAL, amount=999, product_id="p1",
        )
        breakdown = atlas_scoped.breakdown_performance(group_by="product_id", channel_id="c1")
        self.assertEqual(breakdown["p1"].real_revenue, 10)


class MissingAndConflictingMetricsTests(unittest.TestCase):
    def test_no_events_for_dimension_yields_zero_not_unknown(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(OWNER, channel_id="c1", event_type="click")
        report = atlas.analyze_performance_multidimensional(channel_id="c1", product_id="produto-nunca-vendido")
        self.assertEqual(report.real_revenue, 0)

    def test_empty_breakdown_when_dimension_never_recorded(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(OWNER, channel_id="c1", event_type="click")
        breakdown = atlas.breakdown_performance(group_by="funnel_id", channel_id="c1")
        self.assertEqual(breakdown, {})

    def test_conversion_rate_unknown_without_clicks_in_dimensional_report(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=10, product_id="p1",
        )
        report = atlas.analyze_performance_multidimensional(channel_id="c1", product_id="p1")
        self.assertEqual(report.conversion_rate, "UNKNOWN")

    def test_mixed_real_and_forecast_never_combined_in_dimensional_report(self):
        atlas, facade, _ = make_atlas()
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=10, product_id="p1",
        )
        facade.record_sales_event(
            OWNER, channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=9999, product_id="p1",
        )
        report = atlas.analyze_performance_multidimensional(channel_id="c1", product_id="p1")
        self.assertEqual(report.real_revenue, 10)
        self.assertEqual(report.forecast_revenue, 9999)

    def test_invalid_group_by_dimension_rejected(self):
        atlas, facade, _ = make_atlas()
        with self.assertRaises(ValueError):
            atlas.breakdown_performance(group_by="not_a_real_dimension", channel_id="c1")


if __name__ == "__main__":
    unittest.main()
