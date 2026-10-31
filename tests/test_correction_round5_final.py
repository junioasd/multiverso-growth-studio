import time
import unittest

from core.capabilities.registry import CapabilityHealth
from core.evidence.claim_registry import ClaimRecord, ClaimRegistry
from core.evidence.claims import ClaimType
from core.evidence.provenance import ProvenanceRecord
from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
from core.evidence.source_registry import SourceRegistration, SourceRegistry, SourceType, TrustClass
from core.evidence.authority_registry import SCOPE_REGISTER_SOURCE, SCOPE_REGISTER_VERIFIER
from core.evidence.verifier_registry import (
    RegistrationDenied, TrustLevel, VerifierRegistration, VerifierRegistry,
)
from core.multimodal.evidence import Modality
from radar_web.adoption import AdoptionPipeline, AdoptionStatus, QualityGateRejection
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy
from radar_web.quality_gate import RadarWebEvidenceGate
from tests._evidence_test_helpers import make_authority_grant

"""
Testes CORRECAO V6.1.1 ROUND 5 (FINAL) - SourceRegistry real,
VerifierRegistrationPolicy, autorizacao reforcada, ClaimRegistry,
ProvenanceRecord com binding real, testes adversariais A-G + valido.
"""


def make_authorized_verifier_registry(verifier_id="v1"):
    ar, grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
    vr = VerifierRegistry(authority_registry=ar)
    vr.register(
        VerifierRegistration(
            verifier_id=verifier_id, verifier_type="x", authorized=True,
            health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD,
        ),
        authority_grant_id=grant_id, registration_reason="fixture de teste",
    )
    return vr


def make_trusted_source_registry(source_id="fonte_legit"):
    ar, grant_id = make_authority_grant(scope=SCOPE_REGISTER_SOURCE, authority_level="OWNER")
    sr = SourceRegistry(authority_registry=ar)
    sr.register(
        SourceRegistration(
            source_id=source_id, source_type=SourceType.FIRST_PARTY, source_reference="x",
            requested_trust_class=TrustClass.TRUSTED_FIRST_PARTY, health=CapabilityHealth.HEALTHY,
            requested_verification_status=VerificationStatus.VERIFIED,
        ),
        authority_grant_id=grant_id, registration_reason="fixture de teste",
    )
    return sr


class SourceRegistryIntegrationTests(unittest.TestCase):
    """Item 1 — SourceRegistry controla evidência real."""

    def test_A_unregistered_source_never_verified_despite_claimed_type(self):
        """TESTE A exato."""
        vr = make_authorized_verifier_registry()
        registry = EvidenceRegistry(verifier_registry=vr)  # source_registry vazio
        record = registry.register(EvidenceRecord(
            evidence_id="ev-fake", type="x", source="fake://not-registered-anywhere", provenance="x",
            observed_at=time.time(), modality=Modality.TEXT, confidence=0.9, verifier="v1",
            verification_method="m", source_type="OFFICIAL",
            requested_verification_status=VerificationStatus.VERIFIED,
        ))
        self.assertNotEqual(record.verification_status, VerificationStatus.VERIFIED)

    def test_effective_source_type_comes_from_registration_not_caller(self):
        vr = make_authorized_verifier_registry()
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_SOURCE)
        sr = SourceRegistry(authority_registry=_ar)
        sr.register(
            SourceRegistration(
                source_id="fonte_mock", source_type=SourceType.MOCK, source_reference="x",
                requested_trust_class=TrustClass.MOCK, health=CapabilityHealth.HEALTHY,
                requested_verification_status=VerificationStatus.VERIFIED,
            ),
            authority_grant_id=_grant_id, registration_reason="fixture",
        )
        registry = EvidenceRegistry(verifier_registry=vr, source_registry=sr)
        record = registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_mock", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="v1", verification_method="m",
            source_type="OFFICIAL",  # chamador alega OFFICIAL -- mas a registration diz MOCK
            requested_verification_status=VerificationStatus.VERIFIED,
        ))
        self.assertNotEqual(record.verification_status, VerificationStatus.VERIFIED)  # nunca confia na alegação

    def test_valid_registered_source_grants_verified(self):
        vr = make_authorized_verifier_registry()
        sr = make_trusted_source_registry()
        registry = EvidenceRegistry(verifier_registry=vr, source_registry=sr)
        record = registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_legit", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="v1", verification_method="m",
            requested_verification_status=VerificationStatus.VERIFIED,
        ))
        self.assertEqual(record.verification_status, VerificationStatus.VERIFIED)


class SourceRegistrationTrustClassTests(unittest.TestCase):
    """Item 2 — SourceRegistration: trust classes."""

    @staticmethod
    def _with_effective(trust_class, verification_status):
        """Helper — testa `can_sustain_fact()` diretamente sobre os
        campos EFETIVOS (setados manualmente aqui, não via `register()`
        — este teste é sobre a lógica de `can_sustain_fact()`, não
        sobre o gate de autoridade, que tem sua própria classe de
        testes)."""
        reg = SourceRegistration(source_id="s1", source_type=SourceType.FIRST_PARTY, source_reference="x", health=CapabilityHealth.HEALTHY)
        reg.trust_class = trust_class
        reg.verification_status = verification_status
        return reg

    def test_never_sustains_fact_classes(self):
        for trust_class in (TrustClass.MOCK, TrustClass.UNKNOWN, TrustClass.UNTRUSTED):
            reg = self._with_effective(trust_class, VerificationStatus.VERIFIED)
            self.assertFalse(reg.can_sustain_fact())

    def test_trusted_classes_with_full_validity_sustain_fact(self):
        reg = self._with_effective(TrustClass.TRUSTED_FIRST_PARTY, VerificationStatus.VERIFIED)
        self.assertTrue(reg.can_sustain_fact())

    def test_unverified_status_never_sustains_fact_even_if_trust_class_good(self):
        reg = self._with_effective(TrustClass.TRUSTED_OFFICIAL, VerificationStatus.UNVERIFIED)
        self.assertFalse(reg.can_sustain_fact())


# CORREÇÃO (round 7): `VerifierRegistrationPolicy` foi removida —
# `VerifierRegistry.register()` agora É a própria porta de governança,
# exigindo `authority_grant_id` real (nunca mais `authority_level`
# como string). Os testes equivalentes de proteção de autoridade
# vivem em `tests/test_correction_round7_final.py`.


class EnhancedVerifierAuthorizationTests(unittest.TestCase):
    """Item 4 — is_authorized() com política completa."""

    def test_C_authorized_but_untrusted_and_unknown_health_never_authorized(self):
        """TESTE C/18 exato."""
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(
            verifier_id="v_fraco", verifier_type="x", authorized=True,
            trust_level=TrustLevel.UNTRUSTED, health=CapabilityHealth.UNKNOWN,
        ), authority_grant_id=_grant_id, registration_reason="teste")
        self.assertFalse(vr.is_authorized("v_fraco"))

    def test_unknown_health_insufficient_for_fact(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(
            verifier_id="v1", verifier_type="x", authorized=True,
            trust_level=TrustLevel.HIGH, health=CapabilityHealth.UNKNOWN,
        ), authority_grant_id=_grant_id, registration_reason="teste")
        self.assertFalse(vr.is_authorized("v1"))

    def test_degraded_health_requires_explicit_allow(self):
        _ar, _grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER)
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(VerifierRegistration(
            verifier_id="v1", verifier_type="x", authorized=True,
            trust_level=TrustLevel.HIGH, health=CapabilityHealth.DEGRADED,
        ), authority_grant_id=_grant_id, registration_reason="teste")
        self.assertFalse(vr.is_authorized("v1"))
        self.assertTrue(vr.is_authorized("v1", allow_degraded=True))

    def test_healthy_with_standard_trust_authorized_by_default(self):
        vr = make_authorized_verifier_registry()
        self.assertTrue(vr.is_authorized("v1"))


class ClaimRegistryTests(unittest.TestCase):
    """Item 10 — Claim Registry / Claim Binding."""

    def test_F_claim_bound_to_wrong_strategy_mismatch(self):
        """TESTE F/21 exato."""
        cr = ClaimRegistry()
        cr.register(ClaimRecord(claim_id="claim-A", strategy_id="A", claim_type="FACT", statement="x", related_metric_names=["roi"]))
        self.assertFalse(cr.matches("claim-A", strategy_id="B"))
        self.assertTrue(cr.matches("claim-A", strategy_id="A"))

    def test_claim_metric_binding(self):
        cr = ClaimRegistry()
        cr.register(ClaimRecord(claim_id="claim-1", strategy_id="s1", claim_type="FACT", statement="x", related_metric_names=["roi"]))
        self.assertTrue(cr.matches("claim-1", strategy_id="s1", metric_name="ROI"))  # canonicalizado
        self.assertFalse(cr.matches("claim-1", strategy_id="s1", metric_name="ctr"))

    def test_unregistered_claim_never_matches(self):
        cr = ClaimRegistry()
        self.assertFalse(cr.matches("claim-inexistente", strategy_id="s1"))

    def test_evidence_record_with_mismatched_claim_binding_invalid(self):
        """Cenário do item 12 — mismatch de binding completo."""
        vr = make_authorized_verifier_registry()
        sr = make_trusted_source_registry()
        cr = ClaimRegistry()
        cr.register(ClaimRecord(claim_id="claim-B", strategy_id="B", claim_type="FACT", statement="x", related_metric_names=["roi"]))
        registry = EvidenceRegistry(verifier_registry=vr, source_registry=sr, claim_registry=cr)
        # EvidenceRecord alega strategy=A mas o claim resolvido pertence a strategy B
        record = registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_legit", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="v1", verification_method="m",
            related_strategy_id="A", related_claim_id="claim-B", related_metric_name="roi",
            requested_verification_status=VerificationStatus.VERIFIED,
        ))
        self.assertNotEqual(record.verification_status, VerificationStatus.VERIFIED)


class ProvenanceRecordRealBindingTests(unittest.TestCase):
    """Itens 6/7/8/9 — ProvenanceRecord com SourceRegistry, evidence
    binding e integrity reais."""

    def test_D_empty_evidence_ids_never_verified(self):
        """TESTE D/19 exato."""
        vr = make_authorized_verifier_registry()
        sr = make_trusted_source_registry()
        pr = ProvenanceRecord(
            source_type="x", source_reference="fonte_legit", collection_method="x", collector="x",
            verifier="v1", verification_method="m", related_evidence_ids=[],
            requested_verification_status=VerificationStatus.VERIFIED,
        )
        status = pr.grant_verification(verifier_registry=vr, source_registry=sr)
        self.assertNotEqual(status, VerificationStatus.VERIFIED)

    def test_E_fake_source_reference_never_verified(self):
        """TESTE E/20 exato."""
        vr = make_authorized_verifier_registry()
        sr = make_trusted_source_registry()
        pr = ProvenanceRecord(
            source_type="x", source_reference="fake://x", collection_method="x", collector="x",
            verifier="v1", verification_method="m", related_evidence_ids=["ev1"],
        )
        status = pr.grant_verification(verifier_registry=vr, source_registry=sr)
        self.assertNotEqual(status, VerificationStatus.VERIFIED)

    def test_integrity_reference_required_for_certain_source_types(self):
        vr = make_authorized_verifier_registry()
        sr = make_trusted_source_registry()
        pr = ProvenanceRecord(
            source_type="OWN_OPERATION_DATA", source_reference="fonte_legit", collection_method="x", collector="x",
            verifier="v1", verification_method="m", related_evidence_ids=["ev1"], integrity_reference=None,
        )
        status = pr.grant_verification(verifier_registry=vr, source_registry=sr)
        self.assertNotEqual(status, VerificationStatus.VERIFIED)

    def test_evidence_ids_must_resolve_verified_when_registry_provided(self):
        vr = make_authorized_verifier_registry()
        sr = make_trusted_source_registry()
        evidence_registry = EvidenceRegistry(verifier_registry=vr, source_registry=sr)
        pr = ProvenanceRecord(
            source_type="x", source_reference="fonte_legit", collection_method="x", collector="x",
            verifier="v1", verification_method="m", related_evidence_ids=["ev-nao-existe"],
        )
        status = pr.grant_verification(verifier_registry=vr, source_registry=sr, evidence_registry=evidence_registry)
        self.assertNotEqual(status, VerificationStatus.VERIFIED)

    def test_fully_valid_provenance_record_grants_verified(self):
        vr = make_authorized_verifier_registry()
        sr = make_trusted_source_registry()
        evidence_registry = EvidenceRegistry(verifier_registry=vr, source_registry=sr)
        evidence_registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_legit", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="v1", verification_method="m",
            requested_verification_status=VerificationStatus.VERIFIED,
        ))
        pr = ProvenanceRecord(
            source_type="x", source_reference="fonte_legit", collection_method="x", collector="x",
            verifier="v1", verification_method="m", related_evidence_ids=["ev1"],
        )
        status = pr.grant_verification(verifier_registry=vr, source_registry=sr, evidence_registry=evidence_registry)
        self.assertEqual(status, VerificationStatus.VERIFIED)


class CompleteAttackAndValidScenarioTests(unittest.TestCase):
    """TESTE G/22 — ataque completo; cenário 23 — válido."""

    def test_G_complete_attack_always_blocked(self):
        registry = EvidenceRegistry()  # tudo vazio
        fake = EvidenceRecord(
            evidence_id="ev-fake", type="x", source="fake://x", provenance="trust me", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="self", verification_method="because I say so",
            source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED,
        )
        registry.register(fake)
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
            confidence=0.9, source="fake://x", evidence_ids=["ev-fake"], observed_at=time.time(),
        )
        with self.assertRaises(QualityGateRejection):
            pipeline.run_quality_gate(record.record_id, strategy=strategy)
        final = pipeline.get(record.record_id)
        self.assertNotEqual(final.status, AdoptionStatus.ADOPTED)
        self.assertEqual(final.status, AdoptionStatus.REJECTED)

    def test_23_fully_valid_scenario_reaches_verified(self):
        vr = make_authorized_verifier_registry()
        sr = make_trusted_source_registry()
        registry = EvidenceRegistry(verifier_registry=vr, source_registry=sr)
        record = registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_legit", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="v1", verification_method="m",
            requested_verification_status=VerificationStatus.VERIFIED,
        ))
        self.assertEqual(record.verification_status, VerificationStatus.VERIFIED)
        self.assertEqual(registry.explain_status("ev1"), "VERIFIED")


if __name__ == "__main__":
    unittest.main()
