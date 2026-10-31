import os
import tempfile
import time
import unittest
from tests._evidence_test_helpers import make_test_verifier_registry, make_test_source_registry, register_test_claim

from core.evidence.claims import ClaimType
from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
from core.models import db as db_module
from core.multimodal.evidence import Modality
from radar_web.adoption import AdoptionPipeline, AdoptionStatus, InvalidAdoptionTransition, QualityGateRejection
from radar_web.engine import RadarWebComercial, build_default_strategy_sources
from radar_web.evolution import StrategyHistoryEntry, StrategyHistoryStore
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy
from radar_web.quality_gate import RadarWebEvidenceGate
from radar_web.scoring import FunnelIntelligenceScorer

"""
Testes RADAR WEB COMERCIAL (item 32).
"""


def make_verified_evidence_registry(*, evidence_id="ev1", strategy_id="s1") -> EvidenceRegistry:
    """Helper de teste — CORREÇÃO (item 3): evidência agora precisa
    resolver de verdade contra um `EvidenceRegistry`, nunca mais uma
    string solta."""
    registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
    registry.register(EvidenceRecord(
        evidence_id=evidence_id, type="source_document", source="fonte_real",
        provenance="coleta manual verificada", observed_at=time.time(), modality=Modality.DOCUMENT,
        confidence=0.9, related_strategy_id=strategy_id, source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
    ))
    # CORREÇÃO (itens 7/8/9 desta rodada): claim_type=FACT agora exige
    # `primary_claim_id` resolvendo no ClaimRegistry para a MESMA
    # estratégia — registrado aqui para todo teste que usa este helper.
    register_test_claim(registry, claim_id=f"claim-{strategy_id}", strategy_id=strategy_id)
    return registry


def make_pipeline_with_verified_evidence(*, evidence_id="ev1", strategy_id="s1") -> AdoptionPipeline:
    registry = make_verified_evidence_registry(evidence_id=evidence_id, strategy_id=strategy_id)
    gate = RadarWebEvidenceGate(evidence_registry=registry)
    return AdoptionPipeline(quality_gate=gate)


class DiscoveryTests(unittest.TestCase):
    def test_discover_returns_strategies_from_all_sources(self):
        radar = RadarWebComercial()
        results = radar.discover(niche="saude")
        self.assertGreaterEqual(len(results), len(radar.sources))

    def test_discover_filtered_by_mechanism(self):
        radar = RadarWebComercial()
        results = radar.discover(niche="fitness", mechanisms={FunnelMechanism.VSL})
        self.assertTrue(all(r.mechanism == FunnelMechanism.VSL for r in results))
        self.assertGreater(len(results), 0)

    def test_covers_broad_mechanism_list(self):
        sources = build_default_strategy_sources()
        mechanisms = {s.mechanism for s in sources}
        for expected in (
            FunnelMechanism.VSL, FunnelMechanism.WEBINAR, FunnelMechanism.QUIZ, FunnelMechanism.LEAD_MAGNET,
            FunnelMechanism.WHATSAPP, FunnelMechanism.COMMUNITY, FunnelMechanism.CREATOR_COMMERCE,
        ):
            self.assertIn(expected, mechanisms)

    def test_architecture_not_limited_to_listed_mechanisms(self):
        self.assertIn(FunnelMechanism.OTHER, list(FunnelMechanism))

    def test_tracked_strategies_accumulate(self):
        radar = RadarWebComercial()
        radar.discover(niche="a")
        radar.discover(niche="b")
        self.assertGreater(len(radar.tracked_strategies()), len(radar.sources))


class ClassificationTests(unittest.TestCase):
    def test_mock_sources_never_classify_high_scale_without_evidence(self):
        radar = RadarWebComercial()
        results = radar.discover(niche="saude")
        for strategy in results:
            classified = radar.classify_maturity(strategy)
            self.assertEqual(classified.claim_type, ClaimType.UNKNOWN)
            self.assertNotIn(classified.maturity, (FunnelMaturity.HIGH_SCALE, FunnelMaturity.MATURE))

    def test_high_scale_allowed_only_with_fact_and_high_confidence(self):
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.HIGH_SCALE, claim_type=ClaimType.FACT,
            confidence=0.85, source="teste", observed_at=0,
        )
        enforced = strategy.with_maturity_enforced()
        self.assertEqual(enforced.maturity, FunnelMaturity.HIGH_SCALE)

    def test_high_scale_downgraded_without_sufficient_evidence(self):
        strategy = FunnelStrategy(
            strategy_id="s2", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.HIGH_SCALE, claim_type=ClaimType.INFERENCE,
            confidence=0.4, source="teste", observed_at=0,
        )
        enforced = strategy.with_maturity_enforced()
        self.assertEqual(enforced.maturity, FunnelMaturity.UNKNOWN)

    def test_high_scale_downgraded_low_confidence_even_with_fact(self):
        strategy = FunnelStrategy(
            strategy_id="s3", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.HIGH_SCALE, claim_type=ClaimType.FACT,
            confidence=0.5, source="teste", observed_at=0,
        )
        enforced = strategy.with_maturity_enforced()
        self.assertEqual(enforced.maturity, FunnelMaturity.UNKNOWN)

    def test_emerging_and_declining_never_require_high_evidence(self):
        strategy = FunnelStrategy(
            strategy_id="s4", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.QUIZ, maturity=FunnelMaturity.EMERGING, claim_type=ClaimType.HYPOTHESIS,
            confidence=0.2, source="teste", observed_at=0,
        )
        enforced = strategy.with_maturity_enforced()
        self.assertEqual(enforced.maturity, FunnelMaturity.EMERGING)


class ScoringTests(unittest.TestCase):
    def test_score_requires_all_dimensions(self):
        scorer = FunnelIntelligenceScorer()
        with self.assertRaises(ValueError):
            scorer.score(dimensions={"evidence_strength": 0.5}, confidence=0.5)

    def test_score_confidence_kept_separate_from_overall(self):
        scorer = FunnelIntelligenceScorer()
        dims = {
            "evidence_strength": 0.8, "market_presence": 0.7, "growth_signal": 0.6, "longevity": 0.5,
            "repeatability": 0.6, "conversion_signal": 0.5, "scalability": 0.7, "channel_fit": 0.8,
            "product_fit": 0.7, "saturation_risk": 0.2, "implementation_cost": 0.3, "operational_complexity": 0.2,
        }
        score = scorer.score(dimensions=dims, confidence=0.9)
        self.assertEqual(score.confidence, 0.9)
        self.assertNotEqual(score.overall, score.confidence)

    def test_score_dimension_out_of_range_rejected(self):
        scorer = FunnelIntelligenceScorer()
        dims = {
            "evidence_strength": 1.5, "market_presence": 0.7, "growth_signal": 0.6, "longevity": 0.5,
            "repeatability": 0.6, "conversion_signal": 0.5, "scalability": 0.7, "channel_fit": 0.8,
            "product_fit": 0.7, "saturation_risk": 0.2, "implementation_cost": 0.3, "operational_complexity": 0.2,
        }
        with self.assertRaises(ValueError):
            scorer.score(dimensions=dims, confidence=0.9)

    def test_high_risk_dimensions_reduce_overall(self):
        scorer = FunnelIntelligenceScorer()
        base_dims = {
            "evidence_strength": 0.8, "market_presence": 0.8, "growth_signal": 0.8, "longevity": 0.8,
            "repeatability": 0.8, "conversion_signal": 0.8, "scalability": 0.8, "channel_fit": 0.8,
            "product_fit": 0.8,
        }
        low_risk = scorer.score(dimensions={**base_dims, "saturation_risk": 0.1, "implementation_cost": 0.1, "operational_complexity": 0.1}, confidence=0.9)
        high_risk = scorer.score(dimensions={**base_dims, "saturation_risk": 0.9, "implementation_cost": 0.9, "operational_complexity": 0.9}, confidence=0.9)
        self.assertGreater(low_risk.overall, high_risk.overall)


class NicheChannelAdaptationTests(unittest.TestCase):
    def test_same_mechanism_different_niches_tracked_independently(self):
        radar = RadarWebComercial()
        results_a = radar.discover(niche="fitness", mechanisms={FunnelMechanism.VSL})
        results_b = radar.discover(niche="financas", mechanisms={FunnelMechanism.VSL})
        self.assertEqual(results_a[0].niche, "fitness")
        self.assertEqual(results_b[0].niche, "financas")


class AdoptionPipelineTests(unittest.TestCase):
    """CORREÇÃO (item 6): fluxo completo — DISCOVERY -> CLASSIFICATION
    -> EVIDENCE_COLLECTION -> ANALYSIS -> HYPOTHESIS ->
    SENTINEL_QUALITY_GATE -> CONTROLLED_TEST -> METRICS -> VALIDATION
    -> ADOPTED/RETEST/REJECTED."""

    @staticmethod
    def _good_strategy():
        import time

        return FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.8, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
            primary_claim_id="claim-s1",
        )

    def _advance_to_controlled_test(self, pipeline, record_id, *, strategy=None):
        pipeline.classify(record_id)
        pipeline.collect_evidence(record_id, evidence={"ev1": "dado coletado"})
        pipeline.analyze(record_id)
        pipeline.propose_hypothesis(record_id, hypothesis="x")
        pipeline.run_quality_gate(record_id, strategy=strategy or self._good_strategy())

    def test_full_flow_discover_to_adopted(self):
        pipeline = make_pipeline_with_verified_evidence()
        record = pipeline.start("s1")
        self._advance_to_controlled_test(pipeline, record.record_id)
        pipeline.record_metrics(record.record_id, metrics={"engagement_score": 0.05}, provenance="teste controlado interno")
        final = pipeline.validate(record.record_id, decision="ADOPTED")
        self.assertEqual(final.status, AdoptionStatus.ADOPTED)

    def test_cannot_skip_stages(self):
        pipeline = AdoptionPipeline()
        record = pipeline.start("s1")
        with self.assertRaises(InvalidAdoptionTransition):
            pipeline.run_quality_gate(record.record_id, strategy=self._good_strategy())

    def test_metrics_required_never_empty(self):
        pipeline = make_pipeline_with_verified_evidence()
        record = pipeline.start("s1")
        self._advance_to_controlled_test(pipeline, record.record_id)
        with self.assertRaises(ValueError):
            pipeline.record_metrics(record.record_id, metrics={}, provenance="x")

    def test_metrics_require_provenance(self):
        pipeline = make_pipeline_with_verified_evidence()
        record = pipeline.start("s1")
        self._advance_to_controlled_test(pipeline, record.record_id)
        with self.assertRaises(ValueError):
            pipeline.record_metrics(record.record_id, metrics={"engagement_score": 0.05}, provenance="")

    def test_quality_gate_rejects_bad_evidence(self):
        pipeline = AdoptionPipeline()
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"ev1": "x"})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        bad_strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="UNKNOWN", observed_at=0,
        )
        with self.assertRaises(QualityGateRejection):
            pipeline.run_quality_gate(record.record_id, strategy=bad_strategy, metrics={"roi": 999999})
        final = pipeline.get(record.record_id)
        self.assertEqual(final.status, AdoptionStatus.REJECTED)

    def test_rejected_is_terminal(self):
        pipeline = make_pipeline_with_verified_evidence()
        record = pipeline.start("s1")
        self._advance_to_controlled_test(pipeline, record.record_id)
        pipeline.record_metrics(record.record_id, metrics={"engagement_score": 0.001}, provenance="teste controlado")
        pipeline.validate(record.record_id, decision="REJECTED")
        with self.assertRaises(InvalidAdoptionTransition):
            pipeline.analyze(record.record_id)

    def test_retest_cycle_returns_to_controlled_test(self):
        pipeline = make_pipeline_with_verified_evidence()
        record = pipeline.start("s1")
        self._advance_to_controlled_test(pipeline, record.record_id)
        pipeline.record_metrics(record.record_id, metrics={"engagement_score": 0.01}, provenance="teste controlado")
        pipeline.validate(record.record_id, decision="RETEST")
        retried = pipeline.retest(record.record_id)
        self.assertEqual(retried.status, AdoptionStatus.CONTROLLED_TEST)

    def test_invalid_decision_rejected(self):
        pipeline = make_pipeline_with_verified_evidence()
        record = pipeline.start("s1")
        self._advance_to_controlled_test(pipeline, record.record_id)
        pipeline.record_metrics(record.record_id, metrics={"engagement_score": 0.01}, provenance="teste controlado")
        with self.assertRaises(ValueError):
            pipeline.validate(record.record_id, decision="MAYBE")

    def test_own_data_preferred_over_market_inference(self):
        own = {"ctr": 0.08}
        market = {"ctr": 0.03}
        self.assertEqual(AdoptionPipeline.prefer_own_data(own_metrics=own, market_inference=market), own)
        self.assertEqual(AdoptionPipeline.prefer_own_data(own_metrics=None, market_inference=market), market)


class MasterLibraryIntegrationTests(unittest.TestCase):
    def test_radar_web_result_can_seed_library_entry_content(self):
        from core.library.master_library import MasterLibrary

        radar = RadarWebComercial()
        strategies = radar.discover(niche="saude")
        library = MasterLibrary()
        entry = library.add(
            title=f"Descoberta: {strategies[0].name}", area="commercial_strategy",
            content=f"mecanismo={strategies[0].mechanism.value}, claim_type={strategies[0].claim_type.value}",
            origin="radar_web_comercial", responsible_agent="ATLAS",
        )
        self.assertEqual(entry.origin, "radar_web_comercial")


class HistoryPersistenceTests(unittest.TestCase):
    def test_history_persists_across_restart(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            store1 = StrategyHistoryStore(db_path=db_path)
            store1.add_entry(StrategyHistoryEntry(
                entry_id=StrategyHistoryEntry.new_id(), strategy_id="s1", version=1, year=2026,
                status=FunnelMaturity.EMERGING,
            ))
            store2 = StrategyHistoryStore(db_path=db_path)
            history = store2.history_for("s1")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].status, FunnelMaturity.EMERGING)
        finally:
            if os.path.exists(db_path):
                db_module.remove_db_file(db_path)


if __name__ == "__main__":
    unittest.main()


class KnowledgeFreshnessTests(unittest.TestCase):
    """Item 13 — Knowledge Freshness."""

    def test_freshly_observed_strategy_not_stale(self):
        import time
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.EMERGING, claim_type=ClaimType.FACT,
            confidence=0.8, source="teste", observed_at=time.time(),
        )
        self.assertGreater(strategy.freshness_score, 0.9)
        self.assertFalse(strategy.is_stale)

    def test_old_strategy_marked_stale(self):
        import time
        strategy = FunnelStrategy(
            strategy_id="s2", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.EMERGING, claim_type=ClaimType.FACT,
            confidence=0.8, source="teste", observed_at=time.time() - (400 * 86400),
        )
        self.assertTrue(strategy.is_stale)

    def test_valid_until_expiration_marks_stale_regardless_of_score(self):
        import time
        strategy = FunnelStrategy(
            strategy_id="s3", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.EMERGING, claim_type=ClaimType.FACT,
            confidence=0.8, source="teste", observed_at=time.time(), valid_until=time.time() - 1,
        )
        self.assertTrue(strategy.is_stale)
