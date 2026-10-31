import time
import unittest

from core.capabilities.registry import CapabilityHealth
from core.evidence.claims import ClaimType
from core.evidence.metric_evidence import MetricEvidence
from core.evidence.metric_evidence_validator import MetricEvidenceValidator
from core.evidence.provenance import ProvenanceRecord
from core.evidence.registry import CanonicalMetricRegistry, EvidenceRecord, EvidenceRegistry, VerificationStatus
from core.evidence.authority_registry import SCOPE_REGISTER_SOURCE, SCOPE_REGISTER_VERIFIER
from core.evidence.verifier_registry import TrustLevel, VerifierRegistration, VerifierRegistry
from core.multimodal.evidence import Modality
from core.quality.gates import Verdict
from radar_web.adoption import AdoptionPipeline, AdoptionStatus, AdoptionValidationFailed, MetricValidationFailed, QualityGateRejection
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy
from radar_web.quality_gate import RadarWebEvidenceGate
from tests._evidence_test_helpers import make_test_verifier_registry, make_test_source_registry, make_authority_grant

"""
Testes CORRECAO V6.1.1 ROUND 4 (FINAL) - Verifier Registry, Source
validation, ProvenanceRecord nao-declarativa, MetricEvidenceValidator
centralizado, binding triplo, e os 10 testes adversariais A-J.
"""


def make_verified_record(evidence_id, *, strategy_id=None, metric_name=None, confidence=0.9, source_type="OFFICIAL"):
    return EvidenceRecord(
        evidence_id=evidence_id, type="x", source="fonte_real", provenance="coleta",
        observed_at=time.time(), modality=Modality.DOCUMENT, confidence=confidence,
        related_strategy_id=strategy_id, related_metric_name=metric_name, source_type=source_type,
        requested_verification_status=VerificationStatus.VERIFIED,
        verifier="teste_automatizado", verification_method="validacao_manual",
    )


def make_valid_provenance():
    return ProvenanceRecord(
        source_type="OWN_OPERATION_DATA", source_reference="planilha", collection_method="export", collector="agent",
        verifier="teste_automatizado", verification_method="validacao_manual",
        integrity_reference="sha256:teste", related_evidence_ids=["ev-roi"],
    )


class VerifierRegistryTests(unittest.TestCase):
    """Item 2 — Verifier Registry / Trust Policy."""

    def test_unknown_verifier_never_authorized(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        self.assertFalse(vr.is_authorized("self"))
        self.assertFalse(vr.is_authorized("qualquer_coisa"))
        self.assertFalse(vr.is_authorized(None))
        self.assertFalse(vr.is_authorized(""))

    def test_registered_authorized_verifier_works(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(
            verifier_id="v1", verifier_type="human_reviewer", authorized=True,
            health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.HIGH,
        ), authority_grant_id=_grant_id, registration_reason="teste")
        self.assertTrue(vr.is_authorized("v1"))

    def test_unauthorized_flag_blocks_even_if_registered(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(verifier_id="v2", verifier_type="x", authorized=False), authority_grant_id=_grant_id, registration_reason="teste")
        self.assertFalse(vr.is_authorized("v2"))

    def test_unhealthy_verifier_blocked(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(
            verifier_id="v3", verifier_type="x", authorized=True, health=CapabilityHealth.UNHEALTHY,
        ), authority_grant_id=_grant_id, registration_reason="teste")
        self.assertFalse(vr.is_authorized("v3"))

    def test_verification_method_restriction_enforced(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(
            verifier_id="v4", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY,
            trust_level=TrustLevel.STANDARD, verification_methods=["metodo_a"],
        ), authority_grant_id=_grant_id, registration_reason="teste")
        self.assertTrue(vr.is_authorized("v4", verification_method="metodo_a"))
        self.assertFalse(vr.is_authorized("v4", verification_method="metodo_b"))


class EvidenceRecordPolicyTests(unittest.TestCase):
    """Item 1 — EvidenceRecord VERIFIED nunca autodeclarado."""

    def test_A_arbitrary_nonempty_strings_never_grant_verified(self):
        registry = EvidenceRegistry()  # verifier_registry vazio por padrão
        record = EvidenceRecord(
            evidence_id="ev-fake", type="x", source="totally-fake-source", provenance="trust me",
            observed_at=time.time(), modality=Modality.TEXT, confidence=0.9,
            verifier="self", verification_method="because I say so",
            requested_verification_status=VerificationStatus.VERIFIED,
        )
        registered = registry.register(record)
        self.assertNotEqual(registered.verification_status, VerificationStatus.VERIFIED)

    def test_G_unknown_verifier_yields_unverified(self):
        registry = EvidenceRegistry(verifier_registry=VerifierRegistry())
        record = registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte", provenance="prov", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="verificador_desconhecido", verification_method="m",
            source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED,
        ))
        self.assertEqual(record.verification_status, VerificationStatus.UNVERIFIED)

    def test_H_mock_source_type_never_sustains_fact(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(verifier_id="v1", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD), authority_grant_id=_grant_id, registration_reason="teste")
        registry = EvidenceRegistry(verifier_registry=vr)
        record = registry.register(EvidenceRecord(
            evidence_id="ev-mock", type="x", source="fonte", provenance="prov", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="v1", verification_method="m", source_type="MOCK",
            requested_verification_status=VerificationStatus.VERIFIED,
        ))
        self.assertNotEqual(record.verification_status, VerificationStatus.VERIFIED)

    def test_unknown_source_type_also_never_sustains_fact(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(verifier_id="v1", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD), authority_grant_id=_grant_id, registration_reason="teste")
        registry = EvidenceRegistry(verifier_registry=vr)
        record = registry.register(EvidenceRecord(
            evidence_id="ev-unk", type="x", source="fonte", provenance="prov", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="v1", verification_method="m",
            requested_verification_status=VerificationStatus.VERIFIED,  # source_type nunca informado -> None -> tratado como UNKNOWN
        ))
        self.assertNotEqual(record.verification_status, VerificationStatus.VERIFIED)

    def test_positive_path_genuinely_works(self):
        from core.evidence.source_registry import SourceRegistry, SourceRegistration, SourceType, TrustClass

        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)

        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(verifier_id="v1", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD), authority_grant_id=_grant_id, registration_reason="teste")
        _ar_source, _grant_id_source = make_authority_grant(scope=SCOPE_REGISTER_SOURCE)
        sr = SourceRegistry(authority_registry=_ar_source)
        sr.register(
            SourceRegistration(
                source_id="fonte", source_type=SourceType.FIRST_PARTY, source_reference="test://fonte",
                requested_trust_class=TrustClass.TRUSTED_FIRST_PARTY, health=CapabilityHealth.HEALTHY,
                requested_verification_status=VerificationStatus.VERIFIED,
            ),
            authority_grant_id=_grant_id_source, registration_reason="teste",
        )
        registry = EvidenceRegistry(verifier_registry=vr, source_registry=sr)
        record = registry.register(EvidenceRecord(
            evidence_id="ev-good", type="x", source="fonte", provenance="prov", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="v1", verification_method="m", source_type="OFFICIAL",
            requested_verification_status=VerificationStatus.VERIFIED,
        ))
        self.assertEqual(record.verification_status, VerificationStatus.VERIFIED)


class ProvenanceRecordPolicyTests(unittest.TestCase):
    """Itens 5/6 — ProvenanceRecord validação completa e não-declarativa."""

    def test_B_invalid_status_never_valid(self):
        pr = ProvenanceRecord(
            source_type="x", source_reference="x", collection_method="x", collector="x",
            requested_verification_status=VerificationStatus.INVALID,
        )
        self.assertEqual(pr.verification_status, VerificationStatus.INVALID)
        self.assertFalse(pr.is_valid)

    def test_declared_verified_never_bypasses_grant(self):
        pr = ProvenanceRecord(
            source_type="x", source_reference="x", collection_method="x", collector="x",
            requested_verification_status=VerificationStatus.VERIFIED,
        )
        self.assertNotEqual(pr.verification_status, VerificationStatus.VERIFIED)
        self.assertFalse(pr.is_valid)

    def test_grant_verification_with_authorized_verifier_works(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(verifier_id="v1", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD), authority_grant_id=_grant_id, registration_reason="teste")
        pr = ProvenanceRecord(
            source_type="x", source_reference="x", collection_method="x", collector="x",
            verifier="v1", verification_method="m", related_evidence_ids=["ev-x"],
        )
        status = pr.grant_verification(verifier_registry=vr)
        self.assertEqual(status, VerificationStatus.VERIFIED)
        self.assertTrue(pr.is_valid)

    def test_grant_verification_with_unknown_verifier_fails(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        pr = ProvenanceRecord(
            source_type="x", source_reference="x", collection_method="x", collector="x",
            verifier="self", verification_method="because I say so",
        )
        status = pr.grant_verification(verifier_registry=vr)
        self.assertNotEqual(status, VerificationStatus.VERIFIED)


class TripleBindingAndCanonicalMetricTests(unittest.TestCase):
    """Itens 7/8 — binding triplo + Canonical Metric Registry."""

    def test_aliases_normalize_together(self):
        self.assertEqual(CanonicalMetricRegistry.canonicalize("roi"), CanonicalMetricRegistry.canonicalize("ROI"))
        self.assertEqual(CanonicalMetricRegistry.canonicalize("roi"), CanonicalMetricRegistry.canonicalize("return_on_investment"))
        self.assertTrue(CanonicalMetricRegistry.matches("roi", "ROI"))

    def test_different_metrics_never_merge(self):
        self.assertFalse(CanonicalMetricRegistry.matches("roi", "ctr"))
        self.assertFalse(CanonicalMetricRegistry.matches("ctr", "cac"))
        self.assertFalse(CanonicalMetricRegistry.matches("roi", "roas"))

    def test_cpm_margin_profit_registered(self):
        for name in ("cpm", "margin", "profit"):
            self.assertEqual(CanonicalMetricRegistry.canonicalize(name), name)

    def test_D_key_roi_metricevidence_ctr_evidence_ctr_binding_error(self):
        """TESTE D exato: key='roi', MetricEvidence.metric_name='ctr',
        EvidenceRecord.related_metric_name='ctr' -> METRIC_BINDING_ERROR."""
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record("ev-ctr", metric_name="ctr"))
        validator = MetricEvidenceValidator(evidence_registry=registry)
        ctr_evidence = MetricEvidence(
            metric_name="ctr", value=0.5, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-ctr"],
        )
        outcome = validator.validate(requested_key="roi", metric_evidence=ctr_evidence)
        self.assertFalse(outcome.is_valid)
        self.assertTrue(any("METRIC_BINDING_ERROR" in i for i in outcome.issues))

    def test_E_key_roi_metricevidence_roi_evidence_ctr_binding_error(self):
        """TESTE E exato: key='roi', MetricEvidence.metric_name='roi',
        EvidenceRecord.related_metric_name='ctr' -> METRIC_BINDING_ERROR
        (o terceiro vértice do binding também precisa bater)."""
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record("ev-x", metric_name="ctr"))
        validator = MetricEvidenceValidator(evidence_registry=registry)
        me = MetricEvidence(
            metric_name="roi", value=1.5, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-x"],
        )
        outcome = validator.validate(requested_key="roi", metric_evidence=me)
        self.assertFalse(outcome.is_valid)
        self.assertTrue(any("METRIC_BINDING_ERROR" in i for i in outcome.issues))

    def test_F_strategy_A_evidence_never_proves_strategy_B(self):
        """TESTE F exato."""
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record("ev-a", strategy_id="A"))
        status, binding_error = registry.resolve_status_for("ev-a", strategy_id="B")
        self.assertIsNotNone(binding_error)
        self.assertIn("STRATEGY_BINDING_ERROR", binding_error)


class MandatoryProvenanceForFactTests(unittest.TestCase):
    """Item 4 — ProvenanceRecord obrigatória para FACT."""

    def test_C_metric_evidence_without_provenance_record_unverified(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record("ev-roi", metric_name="roi"))
        validator = MetricEvidenceValidator(evidence_registry=registry)
        me = MetricEvidence(
            metric_name="roi", value=1.5, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-roi"],
        )
        outcome = validator.validate(requested_key="roi", metric_evidence=me)
        self.assertFalse(outcome.is_valid)
        self.assertTrue(any("ProvenanceRecord ausente" in i for i in outcome.issues))

    def test_metric_evidence_with_valid_provenance_record_passes(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record("ev-roi", metric_name="roi"))
        validator = MetricEvidenceValidator(evidence_registry=registry)
        me = MetricEvidence(
            metric_name="roi", value=1.5, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-roi"],
            provenance_record=make_valid_provenance(),
        )
        outcome = validator.validate(requested_key="roi", metric_evidence=me)
        self.assertTrue(outcome.is_valid)


class CentralizedValidatorUsedEverywhereTests(unittest.TestCase):
    """Item 11 — MetricEvidenceValidator usado por Quality Gate E
    Adoption Pipeline (nunca lógicas divergentes)."""

    def test_quality_gate_uses_centralized_validator_instance(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        self.assertIsInstance(gate._metric_validator, MetricEvidenceValidator)
        self.assertIs(gate._metric_validator.evidence_registry, registry)

    def test_gate_and_pipeline_agree_on_same_metric(self):
        """A mesma MetricEvidence inválida é recusada IGUALMENTE pelo
        Quality Gate e pelo `record_metrics()` do pipeline — nunca uma
        passa em um e falha em outro."""
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record("ev1", strategy_id="s1"))
        registry.register(make_verified_record("ev-roi", strategy_id="s1", metric_name="roi"))
        from tests._evidence_test_helpers import register_test_claim
        register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        pipeline = AdoptionPipeline(quality_gate=gate)

        bad_roi = MetricEvidence(
            metric_name="roi", value=999999, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-roi"],
        )  # sem provenance_record -- invalida em ambos

        gate_result = gate.evaluate({
            "strategy": FunnelStrategy(
                strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
                mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
                confidence=0.9, source="fonte", evidence_ids=["ev1"], observed_at=time.time(),
                primary_claim_id="claim-s1",
            ),
            "metrics": {"roi": bad_roi},
        })
        self.assertNotEqual(gate_result.verdict, Verdict.APPROVED)

        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.85, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
            primary_claim_id="claim-s1",
        )
        pipeline.run_quality_gate(record.record_id, strategy=strategy)
        with self.assertRaises(MetricValidationFailed):
            pipeline.record_metrics(record.record_id, metrics={"roi": bad_roi}, provenance="teste")


class CompleteAttackScenarioTests(unittest.TestCase):
    """TESTE I — ataque completo nunca chega a ADOPTED."""

    def test_I_complete_attack_always_blocked(self):
        registry = EvidenceRegistry()  # verifier_registry vazio -- cenário realista de ataque
        fake_evidence = EvidenceRecord(
            evidence_id="ev-fake", type="x", source="totally-fake-source", provenance="trust me",
            observed_at=time.time(), modality=Modality.TEXT, confidence=0.9,
            verifier="self", verification_method="because I say so", source_type="OFFICIAL",
            requested_verification_status=VerificationStatus.VERIFIED,
        )
        registry.register(fake_evidence)
        self.assertNotEqual(registry.resolve_status("ev-fake"), VerificationStatus.VERIFIED)

        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=registry))
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="totally-fake-source", evidence_ids=["ev-fake"], observed_at=time.time(),
        )
        with self.assertRaises(QualityGateRejection):
            pipeline.run_quality_gate(record.record_id, strategy=strategy)

        final = pipeline.get(record.record_id)
        self.assertNotEqual(final.status, AdoptionStatus.ADOPTED)
        self.assertEqual(final.status, AdoptionStatus.REJECTED)


class ValidScenarioStillWorksTests(unittest.TestCase):
    """TESTE J — cenário genuinamente válido segue normalmente."""

    def test_J_fully_valid_scenario_reaches_adopted(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record("ev1", strategy_id="sJ"))
        registry.register(make_verified_record("ev-roi", strategy_id="sJ", metric_name="roi"))
        from tests._evidence_test_helpers import register_test_claim
        register_test_claim(registry, claim_id="claim-sJ", strategy_id="sJ")
        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=registry))
        record = pipeline.start("sJ")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        strategy = FunnelStrategy(
            strategy_id="sJ", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.85, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
            primary_claim_id="claim-sJ",
        )
        pipeline.run_quality_gate(record.record_id, strategy=strategy)
        roi_evidence = MetricEvidence(
            metric_name="roi", value=1.8, source="planilha", provenance="dados",
            observed_at=time.time(), context="campanha", measurement_type="calculado", confidence=0.9,
            evidence_ids=["ev-roi"], provenance_record=make_valid_provenance(),
        )
        pipeline.record_metrics(record.record_id, metrics={"roi": roi_evidence}, provenance="teste controlado")
        final = pipeline.validate(record.record_id, decision="ADOPTED")
        self.assertEqual(final.status, AdoptionStatus.ADOPTED)


if __name__ == "__main__":
    unittest.main()
