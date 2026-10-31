import unittest
from tests._evidence_test_helpers import make_test_verifier_registry, make_test_source_registry

from core.evidence.claims import ClaimType
from core.quality.gates import Verdict
from radar_web.adoption import AdoptionPipeline, AdoptionStatus, InvalidAdoptionTransition, QualityGateRejection
from radar_web.engine import MockStrategySource, RadarWebComercial
from radar_web.evolution import AnnualReviewEngine, DuplicateAnnualReviewError, StrategyHistoryStore
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy, compute_strategy_fingerprint
from radar_web.quality_gate import RadarWebEvidenceGate

"""
Testes CORRECAO V6.1 - itens 4 (identidade estavel), 5 (Quality Gate),
6 (pipeline de adocao corrigido), 11 (Biblioteca Mestre).
"""


class StableStrategyIdentityTests(unittest.TestCase):
    def test_same_structural_attributes_same_id(self):
        id1 = compute_strategy_fingerprint(mechanism=FunnelMechanism.VSL, niche="saude", channel="youtube")
        id2 = compute_strategy_fingerprint(mechanism=FunnelMechanism.VSL, niche="saude", channel="youtube")
        self.assertEqual(id1, id2)

    def test_different_niche_different_id(self):
        id1 = compute_strategy_fingerprint(mechanism=FunnelMechanism.VSL, niche="saude", channel="youtube")
        id2 = compute_strategy_fingerprint(mechanism=FunnelMechanism.VSL, niche="fitness", channel="youtube")
        self.assertNotEqual(id1, id2)

    def test_case_and_whitespace_normalized(self):
        id1 = compute_strategy_fingerprint(mechanism=FunnelMechanism.VSL, niche="Saude", channel="YouTube")
        id2 = compute_strategy_fingerprint(mechanism=FunnelMechanism.VSL, niche=" saude ", channel="youtube")
        self.assertEqual(id1, id2)

    def test_id_never_random_across_calls(self):
        source = MockStrategySource(mechanism=FunnelMechanism.VSL, source_name="mock")
        radar = RadarWebComercial(sources=[source])
        ids = {radar.discover(niche="saude")[0].strategy_id for _ in range(5)}
        self.assertEqual(len(ids), 1)

    def test_same_strategy_three_years_same_id_increasing_version(self):
        source = MockStrategySource(mechanism=FunnelMechanism.VSL, source_name="mock")
        radar = RadarWebComercial(sources=[source])
        history = StrategyHistoryStore()
        engine = AnnualReviewEngine(radar=radar, history=history)

        r1 = engine.run_annual_review(year=2026, niche="saude")
        r2 = engine.run_annual_review(year=2027, niche="saude")
        r3 = engine.run_annual_review(year=2028, niche="saude")

        ids = {r1["entries"][0].strategy_id, r2["entries"][0].strategy_id, r3["entries"][0].strategy_id}
        self.assertEqual(len(ids), 1)
        versions = [e.strategy_version for e in history.history_for(r1["entries"][0].strategy_id)]
        self.assertEqual(versions, [1, 2, 3])

    def test_repeated_same_year_review_requires_explicit_reason(self):
        source = MockStrategySource(mechanism=FunnelMechanism.VSL, source_name="mock")
        radar = RadarWebComercial(sources=[source])
        history = StrategyHistoryStore()
        engine = AnnualReviewEngine(radar=radar, history=history)
        engine.run_annual_review(year=2026, niche="saude")
        with self.assertRaises(DuplicateAnnualReviewError):
            engine.run_annual_review(year=2026, niche="saude")

    def test_repeated_same_year_with_reason_gets_new_snapshot_version(self):
        source = MockStrategySource(mechanism=FunnelMechanism.VSL, source_name="mock")
        radar = RadarWebComercial(sources=[source])
        history = StrategyHistoryStore()
        engine = AnnualReviewEngine(radar=radar, history=history)
        engine.run_annual_review(year=2026, niche="saude")
        r2 = engine.run_annual_review(year=2026, niche="saude", revision_reason="correção de classificação")
        entry = r2["entries"][0]
        self.assertEqual(entry.annual_snapshot_version, 2)
        self.assertEqual(entry.revision_number, 2)
        self.assertEqual(entry.strategy_version, 2)  # global tambem avanca

    def test_never_1_1_silently(self):
        source = MockStrategySource(mechanism=FunnelMechanism.VSL, source_name="mock")
        radar = RadarWebComercial(sources=[source])
        history = StrategyHistoryStore()
        engine = AnnualReviewEngine(radar=radar, history=history)
        engine.run_annual_review(year=2026, niche="saude")
        r2 = engine.run_annual_review(year=2026, niche="saude", revision_reason="motivo")
        strategy_id = r2["entries"][0].strategy_id
        same_year = [e for e in history.by_year(2026) if e.strategy_id == strategy_id]
        annual_versions = sorted(e.annual_snapshot_version for e in same_year)
        self.assertEqual(annual_versions, [1, 2])

    def test_history_preserved_across_variant(self):
        from radar_web.models import compute_strategy_variant_id

        base_id = compute_strategy_fingerprint(mechanism=FunnelMechanism.VSL, niche="saude", channel="youtube")
        variant_id = compute_strategy_variant_id(strategy_id=base_id, variant_label="copy_b")
        self.assertNotEqual(base_id, variant_id)
        self.assertTrue(variant_id.startswith(base_id))


class RadarWebQualityGateTests(unittest.TestCase):
    def test_fabricated_roi_without_source_rejected(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="UNKNOWN", observed_at=0,
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"roi": 999999}})
        self.assertIn(result.verdict, (Verdict.REJECTED, Verdict.BLOCKED))
        self.assertNotEqual(result.verdict, Verdict.APPROVED)

    def test_estimate_with_partial_evidence_stays_estimate(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s2", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.EMERGING, claim_type=ClaimType.ESTIMATE,
            confidence=0.6, source="fonte", evidence_ids=["ev1"], observed_at=0,
        )
        gate.evaluate({"strategy": strategy, "metrics": {}})
        self.assertEqual(strategy.claim_type, ClaimType.ESTIMATE)

    def test_well_evidenced_strategy_approved(self):
        import time

        from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
        from core.multimodal.evidence import Modality
        from tests._evidence_test_helpers import register_test_claim

        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        for eid in ("ev1", "ev2"):
            registry.register(EvidenceRecord(
                evidence_id=eid, type="source_document", source="fonte_real", provenance="coleta verificada",
                observed_at=time.time(), modality=Modality.DOCUMENT, confidence=0.9,
                source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
            ))
        register_test_claim(registry, claim_id="claim-s3", strategy_id="s3")
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="s3", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.85, source="fonte_real", evidence_ids=["ev1", "ev2"], observed_at=time.time(),
            primary_claim_id="claim-s3",
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {}})
        self.assertEqual(result.verdict, Verdict.APPROVED)

    def test_stale_evidence_flagged(self):
        import time

        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s4", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.85, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time() - (400 * 86400),
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {}})
        self.assertNotEqual(result.verdict, Verdict.APPROVED)
        self.assertEqual(result.criteria["freshness"], "STALE")

    def test_missing_source_rejected(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s5", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.FACT,
            confidence=0.9, source="", evidence_ids=["ev1"], observed_at=0,
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {}})
        self.assertEqual(result.verdict, Verdict.REJECTED)


class CorrectedAdoptionPipelineTests(unittest.TestCase):
    @staticmethod
    def _good_strategy():
        import time

        return FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.85, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
            primary_claim_id="claim-s1",
        )

    @staticmethod
    def _pipeline_with_verified_evidence():
        import time

        from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
        from core.multimodal.evidence import Modality
        from tests._evidence_test_helpers import register_test_claim

        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="source_document", source="fonte_real", provenance="coleta verificada",
            observed_at=time.time(), modality=Modality.DOCUMENT, confidence=0.9,
            source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")
        return AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=registry))

    def test_full_pipeline_stages_in_order(self):
        pipeline = self._pipeline_with_verified_evidence()
        record = pipeline.start("s1")
        self.assertEqual(record.status, AdoptionStatus.DISCOVERY)
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        pipeline.run_quality_gate(record.record_id, strategy=self._good_strategy())
        current = pipeline.get(record.record_id)
        self.assertEqual(current.status, AdoptionStatus.CONTROLLED_TEST)
        pipeline.record_metrics(record.record_id, metrics={"engagement_score": 0.05}, provenance="teste controlado")
        final = pipeline.validate(record.record_id, decision="ADOPTED")
        self.assertEqual(final.status, AdoptionStatus.ADOPTED)

    def test_evidence_collection_requires_real_evidence(self):
        pipeline = AdoptionPipeline()
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        with self.assertRaises(ValueError):
            pipeline.collect_evidence(record.record_id, evidence={})

    def test_quality_gate_stage_mandatory_before_controlled_test(self):
        pipeline = AdoptionPipeline()
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        with self.assertRaises(InvalidAdoptionTransition):
            pipeline._advance(record.record_id, to=AdoptionStatus.CONTROLLED_TEST)

    def test_quality_gate_rejection_prevents_controlled_test(self):
        pipeline = AdoptionPipeline()
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        bad_strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="UNKNOWN", observed_at=0,
        )
        with self.assertRaises(QualityGateRejection):
            pipeline.run_quality_gate(record.record_id, strategy=bad_strategy)
        self.assertEqual(pipeline.get(record.record_id).status, AdoptionStatus.REJECTED)

    def test_metrics_require_provenance(self):
        pipeline = self._pipeline_with_verified_evidence()
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        pipeline.run_quality_gate(record.record_id, strategy=self._good_strategy())
        with self.assertRaises(ValueError):
            pipeline.record_metrics(record.record_id, metrics={"engagement_score": 0.05}, provenance="")

    def test_own_data_prioritized_over_market_inference(self):
        result = AdoptionPipeline.prefer_own_data(own_metrics={"ctr": 0.09}, market_inference={"ctr": 0.02})
        self.assertEqual(result["ctr"], 0.09)


class RejectedKnowledgePreservedTests(unittest.TestCase):
    """Item 11 — conhecimento rejeitado permanece como
    candidato/rejeitado, nunca some."""

    def test_rejected_record_still_retrievable(self):
        pipeline = AdoptionPipeline()
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        bad_strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="UNKNOWN", observed_at=0,
        )
        try:
            pipeline.run_quality_gate(record.record_id, strategy=bad_strategy)
        except QualityGateRejection:
            pass
        retrieved = pipeline.get(record.record_id)
        self.assertEqual(retrieved.status, AdoptionStatus.REJECTED)
        self.assertIsNotNone(retrieved.quality_gate_result)


if __name__ == "__main__":
    unittest.main()
