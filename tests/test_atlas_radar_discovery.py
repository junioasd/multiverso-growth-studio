import unittest

from atlas.radar.engine import CommercialOpportunityRadar
from atlas.radar.models import ChannelType, EvidenceLevel, OfferType, OpportunitySignal, CommercialOpportunity
from atlas.radar.sources import AffiliateMarketplaceOpportunitySource, GenericMockOpportunitySource

"""
Testes ATLAS+RADAR - DESCOBERTA DE NOVOS CANAIS, CANAIS ALEM DE
AFILIADOS, CLASSIFICACAO REAL/ESTIMADO/INFERIDO/DESCONHECIDO, RANKING
COM EVIDENCIAS.
"""


class ChannelCoverageTests(unittest.TestCase):
    def setUp(self):
        self.radar = CommercialOpportunityRadar()

    def test_covers_own_products_channel(self):
        types = {s.channel_type for s in self.radar.sources}
        self.assertIn(ChannelType.OWN_STORE, types)
        self.assertIn(ChannelType.SITE, types)

    def test_covers_services_channel(self):
        types = {s.channel_type for s in self.radar.sources}
        self.assertIn(ChannelType.SERVICE_PLATFORM, types)

    def test_covers_marketplaces(self):
        types = {s.channel_type for s in self.radar.sources}
        self.assertIn(ChannelType.MARKETPLACE, types)

    def test_covers_sites_and_blogs(self):
        types = {s.channel_type for s in self.radar.sources}
        self.assertIn(ChannelType.SITE, types)
        self.assertIn(ChannelType.BLOG, types)

    def test_covers_social_networks(self):
        types = {s.channel_type for s in self.radar.sources}
        for expected in (ChannelType.YOUTUBE, ChannelType.INSTAGRAM, ChannelType.FACEBOOK, ChannelType.TIKTOK):
            self.assertIn(expected, types)

    def test_covers_email_and_newsletter(self):
        types = {s.channel_type for s in self.radar.sources}
        self.assertIn(ChannelType.EMAIL, types)
        self.assertIn(ChannelType.NEWSLETTER, types)

    def test_covers_communities_and_forums(self):
        types = {s.channel_type for s in self.radar.sources}
        self.assertIn(ChannelType.COMMUNITY, types)
        self.assertIn(ChannelType.FORUM, types)

    def test_radar_not_limited_to_affiliate_platforms(self):
        non_affiliate_sources = [s for s in self.radar.sources if s.channel_type != ChannelType.AFFILIATE_PLATFORM]
        self.assertGreater(len(non_affiliate_sources), 15)

    def test_affiliate_sources_still_included_among_others(self):
        affiliate_sources = [s for s in self.radar.sources if s.channel_type == ChannelType.AFFILIATE_PLATFORM]
        self.assertGreaterEqual(len(affiliate_sources), 5)

    def test_unknown_future_channel_falls_back_to_other(self):
        self.assertIn(ChannelType.OTHER, list(ChannelType))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.radar = CommercialOpportunityRadar()

    def test_scan_returns_opportunities_from_all_sources(self):
        opportunities = self.radar.scan(niche="saude")
        self.assertGreaterEqual(len(opportunities), len(self.radar.sources))

    def test_scan_filtered_by_channel_type(self):
        opportunities = self.radar.scan(channel_types={ChannelType.YOUTUBE}, niche="fitness")
        self.assertTrue(all(o.channel_type == ChannelType.YOUTUBE for o in opportunities))
        self.assertGreater(len(opportunities), 0)

    def test_scan_own_product_channel_specifically(self):
        opportunities = self.radar.scan(channel_types={ChannelType.OWN_STORE})
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0].offer_type, OfferType.OWN_PRODUCT)

    def test_scan_service_channel_specifically(self):
        opportunities = self.radar.scan(channel_types={ChannelType.SERVICE_PLATFORM})
        self.assertEqual(opportunities[0].offer_type, OfferType.SERVICE)

    def test_scan_never_returns_fixed_cached_list_across_calls(self):
        first = self.radar.scan(channel_types={ChannelType.YOUTUBE})
        second = self.radar.scan(channel_types={ChannelType.YOUTUBE})
        self.assertNotEqual(first[0].opportunity_id, second[0].opportunity_id)

    def test_tracking_accumulates_scans_over_time(self):
        self.radar.scan(channel_types={ChannelType.YOUTUBE})
        self.radar.scan(channel_types={ChannelType.YOUTUBE})
        tracked = self.radar.tracked_opportunities()
        self.assertGreater(len(tracked), 0)

    def test_unknown_niche_still_returns_opportunities_without_crashing(self):
        opportunities = self.radar.scan(niche="nicho-nunca-visto-antes")
        self.assertGreater(len(opportunities), 0)


class EvidenceClassificationTests(unittest.TestCase):
    def setUp(self):
        self.radar = CommercialOpportunityRadar()

    def test_mock_sources_never_produce_real_evidence(self):
        opportunities = self.radar.scan(niche="saude")
        for opp in opportunities:
            for signal in opp.signals.values():
                self.assertEqual(signal.evidence, EvidenceLevel.UNKNOWN)

    def test_generic_mock_source_never_fabricates_numeric_value(self):
        source = GenericMockOpportunitySource(
            channel_type=ChannelType.YOUTUBE, source_name="youtube_test", default_offer_type=OfferType.INFOPRODUCT,
        )
        opportunities = source.discover(niche="saude")
        for opp in opportunities:
            for signal in opp.signals.values():
                self.assertEqual(signal.value, "UNKNOWN")

    def test_affiliate_source_mock_products_also_classified_unknown(self):
        source = AffiliateMarketplaceOpportunitySource("hotmart")
        opportunities = source.discover(niche="saude")
        self.assertGreater(len(opportunities), 0)
        for opp in opportunities:
            for signal in opp.signals.values():
                self.assertEqual(signal.evidence, EvidenceLevel.UNKNOWN)

    def test_evidence_never_inferred_from_plausible_looking_mock_numbers(self):
        source = AffiliateMarketplaceOpportunitySource("hotmart")
        opportunities = source.discover(niche="saude")
        commission_signal = opportunities[0].signal("commission")
        self.assertEqual(commission_signal.evidence, EvidenceLevel.UNKNOWN)
        self.assertEqual(commission_signal.value, "UNKNOWN")

    def test_opportunity_notes_disclose_mock_status(self):
        source = GenericMockOpportunitySource(channel_type=ChannelType.BLOG, source_name="blog_test")
        opportunities = source.discover()
        self.assertTrue(any("MOCK" in note for note in opportunities[0].notes))


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.radar = CommercialOpportunityRadar()

    def test_ranking_with_no_real_evidence_all_unknown_score(self):
        opportunities = self.radar.scan(channel_types={ChannelType.YOUTUBE})
        ranked = self.radar.rank(opportunities)
        self.assertTrue(all(r.score == "UNKNOWN" for r in ranked))
        self.assertTrue(all(r.confidence == "LOW" for r in ranked))

    def test_ranking_has_justification_for_every_item(self):
        opportunities = self.radar.scan(channel_types={ChannelType.YOUTUBE})
        ranked = self.radar.rank(opportunities)
        for r in ranked:
            self.assertIsInstance(r.justification, list)

    def test_ranking_scores_when_real_evidence_supplied(self):
        opp = CommercialOpportunity(
            opportunity_id="opp_test1", channel_type=ChannelType.YOUTUBE, channel_name="youtube",
            offer_type=OfferType.INFOPRODUCT, title="teste",
            signals={
                "margin_potential": OpportunitySignal(value=0.4, evidence=EvidenceLevel.REAL, source="owner"),
                "roi": OpportunitySignal(value=1.2, evidence=EvidenceLevel.REAL, source="owner"),
                "cost": OpportunitySignal(value=100, evidence=EvidenceLevel.REAL, source="owner"),
                "compliance": OpportunitySignal(value=True, evidence=EvidenceLevel.REAL, source="owner"),
            },
        )
        ranked = self.radar.rank([opp], policy_compatible_by_id={"opp_test1": True}, risk_level_by_id={"opp_test1": "LOW"})
        self.assertIsInstance(ranked[0].score, float)
        self.assertIn(ranked[0].confidence, ("LOW", "MEDIUM", "HIGH"))

    def test_ranking_never_scores_when_gate_blocks(self):
        opp = CommercialOpportunity(
            opportunity_id="opp_test2", channel_type=ChannelType.YOUTUBE, channel_name="youtube",
            offer_type=OfferType.INFOPRODUCT, title="teste",
            signals={"margin_potential": OpportunitySignal(value=0.9, evidence=EvidenceLevel.REAL)},
        )
        ranked = self.radar.rank([opp], policy_compatible_by_id={"opp_test2": False})
        self.assertEqual(ranked[0].score, "UNKNOWN")

    def test_ranking_sorted_with_unknown_at_end(self):
        real_opp = CommercialOpportunity(
            opportunity_id="opp_real", channel_type=ChannelType.YOUTUBE, channel_name="youtube",
            offer_type=OfferType.INFOPRODUCT, title="real",
            signals={
                "margin_potential": OpportunitySignal(value=0.5, evidence=EvidenceLevel.REAL),
                "cost": OpportunitySignal(value=10, evidence=EvidenceLevel.REAL),
                "compliance": OpportunitySignal(value=True, evidence=EvidenceLevel.REAL),
            },
        )
        unknown_opp = CommercialOpportunity(
            opportunity_id="opp_unk", channel_type=ChannelType.BLOG, channel_name="blog",
            offer_type=OfferType.OTHER, title="desconhecida",
        )
        ranked = self.radar.rank(
            [unknown_opp, real_opp],
            policy_compatible_by_id={"opp_real": True, "opp_unk": True},
            risk_level_by_id={"opp_real": "LOW", "opp_unk": "LOW"},
        )
        self.assertEqual(ranked[0].opportunity.opportunity_id, "opp_real")
        self.assertEqual(ranked[-1].score, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
