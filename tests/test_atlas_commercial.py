import unittest

from atlas.director import AtlasDirector
from commercial.funnel_engine import FunnelSelectionInput
from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.isolation.isolation import IsolationViolation
from core.jobs.queue import JobQueue
from core.models.entities import ValueType
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role

"""
Testes ATLAS — ISOLAMENTO e DECISÕES COMERCIAIS / integração com
MultiGerenciador Comercial.
"""


def make_atlas(scopes):
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes=scopes)
    return AtlasDirector(facade=facade, principal=principal), facade, audit


class AtlasIsolationTests(unittest.TestCase):
    def test_atlas_scoped_to_one_channel_cannot_analyze_another(self):
        atlas, facade, _ = make_atlas({"channel:chan_A"})
        owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
        facade.record_sales_event(
            owner, channel_id="chan_B", event_type="sale", value_type=ValueType.REAL, amount=999
        )
        with self.assertRaises(IsolationViolation):
            atlas.analyze_performance(channel_id="chan_B")

    def test_atlas_scoped_to_channel_can_analyze_it(self):
        atlas, facade, _ = make_atlas({"channel:chan_A"})
        owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
        facade.record_sales_event(
            owner, channel_id="chan_A", event_type="sale", value_type=ValueType.REAL, amount=150
        )
        report = atlas.analyze_performance(channel_id="chan_A")
        self.assertEqual(report.real_revenue, 150)

    def test_atlas_with_wildcard_scope_can_analyze_any_specific_channel(self):
        atlas, facade, _ = make_atlas({"*"})
        owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
        facade.record_sales_event(
            owner, channel_id="chan_X", event_type="sale", value_type=ValueType.REAL, amount=42
        )
        report = atlas.analyze_performance(channel_id="chan_X")
        self.assertEqual(report.real_revenue, 42)

    def test_isolation_never_bypassed_by_atlas_role(self):
        atlas, facade, _ = make_atlas({"channel:chan_A"})
        with self.assertRaises(IsolationViolation):
            facade.create_lead(atlas.principal, channel_id="chan_other", source="youtube")


class AtlasCommercialIntegrationTests(unittest.TestCase):
    def test_evaluate_opportunities_delegates_to_multigerenciador(self):
        atlas, facade, _ = make_atlas({"*"})
        assessment = atlas.evaluate_opportunities(
            channel_id="chan_1", platform="youtube", affiliate_platform="hotmart",
            niche="saude", funnel_request=FunnelSelectionInput(platform="youtube"),
        )
        self.assertEqual(assessment.channel_id, "chan_1")
        self.assertGreater(len(assessment.opportunities), 0)
        direct = facade.mgc.assess_opportunity(
            channel_id="chan_1", platform="youtube", affiliate_platform="hotmart",
            niche="saude", funnel_request=FunnelSelectionInput(platform="youtube"),
        )
        self.assertEqual(
            {o.ranked_product.catalog_product.external_id for o in assessment.opportunities},
            {rp.catalog_product.external_id for rp in direct.ranked_products},
        )

    def test_opportunity_offer_category_defaults_to_affiliate_for_marketplace_products(self):
        atlas, facade, _ = make_atlas({"*"})
        assessment = atlas.evaluate_opportunities(
            channel_id="chan_1", platform="youtube", affiliate_platform="hotmart",
            niche="saude", funnel_request=FunnelSelectionInput(platform="youtube"),
        )
        for opp in assessment.opportunities:
            self.assertEqual(opp.offer_category, "affiliate")

    def test_opportunity_evaluation_never_invents_recommendation_without_compliance(self):
        atlas, facade, _ = make_atlas({"*"})
        assessment = atlas.evaluate_opportunities(
            channel_id="chan_1", platform="youtube", affiliate_platform="hotmart",
            niche="saude", funnel_request=FunnelSelectionInput(platform="youtube"),
        )
        self.assertTrue(all(not o.ranked_product.recommended for o in assessment.opportunities))

    def test_funnel_recommendation_present_and_consistent(self):
        atlas, facade, _ = make_atlas({"*"})
        assessment = atlas.evaluate_opportunities(
            channel_id="chan_1", platform="tiktok", affiliate_platform="hotmart",
            niche="fitness", funnel_request=FunnelSelectionInput(platform="tiktok", audience_trust_level="LOW"),
        )
        self.assertIsNotNone(assessment.funnel_recommendation)

    def test_evaluate_opportunities_is_audited(self):
        atlas, facade, audit = make_atlas({"*"})
        atlas.evaluate_opportunities(
            channel_id="chan_1", platform="youtube", affiliate_platform="hotmart",
            niche="saude", funnel_request=FunnelSelectionInput(platform="youtube"),
        )
        events = [e for e in audit.all() if e.action == "atlas_evaluate_opportunities"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].outcome, "SUCCESS")

    def test_empty_catalog_handled_without_crashing(self):
        atlas, facade, _ = make_atlas({"*"})
        assessment = atlas.evaluate_opportunities(
            channel_id="chan_1", platform="youtube", affiliate_platform="hotmart",
            niche="nicho-sem-produtos", funnel_request=FunnelSelectionInput(platform="youtube"),
        )
        self.assertIsInstance(assessment.opportunities, list)

    def test_invalid_affiliate_platform_raises_same_error_as_underlying_engine(self):
        atlas, facade, _ = make_atlas({"*"})
        with self.assertRaises(ValueError):
            atlas.evaluate_opportunities(
                channel_id="chan_1", platform="youtube", affiliate_platform="plataforma-inexistente",
                niche="saude", funnel_request=FunnelSelectionInput(platform="youtube"),
            )


if __name__ == "__main__":
    unittest.main()
