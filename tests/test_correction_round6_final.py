import time
import unittest

from core.capabilities.registry import CapabilityHealth
from core.evidence.authority_registry import (
    AuthorityDenied, SCOPE_REGISTER_SOURCE, SCOPE_REGISTER_VERIFIER, AuthorityRegistry,
)
from core.evidence.claim_registry import ClaimRecord, ClaimRegistry
from core.evidence.claims import ClaimType
from core.evidence.metric_evidence import MetricEvidence
from core.evidence.metric_evidence_validator import MetricEvidenceValidator
from core.evidence.provenance import ProvenanceRecord
from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
from core.evidence.source_registry import (
    SourceRegistrationDenied, SourceRegistration, SourceRegistry, SourceType, TrustClass,
)
from core.evidence.verifier_registry import (
    RegistrationDenied, TrustLevel, VerifierRegistration, VerifierRegistry,
)
from core.multimodal.evidence import Modality
from core.quality.gates import Verdict
from radar_web.adoption import AdoptionPipeline, AdoptionStatus, QualityGateRejection
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy
from radar_web.quality_gate import RadarWebEvidenceGate
from tests._evidence_test_helpers import (
    make_test_verifier_registry, make_test_source_registry, register_test_claim, make_authority_grant,
)

"""
Testes CORRECAO V6.1.1 ROUND 6 (FINAL) - register() como gate de
autoridade unico (sem bypass), claim binding obrigatorio para FACT,
ProvenanceRecord strategy/claim mismatch, strategy.source resolvendo
no SourceRegistry, testes adversariais 18-23.

CORREÇÃO (round 7): as classes `VerifierRegistryNoBypassTests`/
`SourceRegistryNoBypassTests` foram atualizadas para o novo contrato
`authority_grant_id` (não aceita mais `authority_level` como string) —
o MESMO comportamento de segurança (registro nunca aceito sem
autoridade genuína), agora testado através do mecanismo real de
`AuthorityGrant`/`AuthorityRegistry`.
"""


class VerifierRegistryNoBypassTests(unittest.TestCase):
    """Itens 1/2/3 — register() é o único caminho, sempre exige um AuthorityGrant real."""

    def test_direct_register_without_grant_denied(self):
        """Sem `authority_grant_id`, `register()` sempre recusa —
        mesmo chamado diretamente (não há outro caminho)."""
        vr = VerifierRegistry()
        with self.assertRaises(RegistrationDenied):
            vr.register(
                VerifierRegistration(
                    verifier_id="self", verifier_type="x", authorized=True,
                    health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.HIGH,
                ),
                authority_grant_id="", registration_reason="self-registro",
            )
        self.assertFalse(vr.is_authorized("self"))

    def test_grant_without_required_scope_denied(self):
        """Um grant válido, mas SEM o escopo `REGISTER_VERIFIER`,
        nunca basta — `authority_level` alto sozinho não é suficiente."""
        from core.evidence.owner_principal import issue_owner_principal
        from tests._evidence_test_helpers import make_authenticated_test_owner_session

        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        root = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="x")
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="componente", authority_level="OWNER",
            scopes=[SCOPE_REGISTER_SOURCE],  # escopo ERRADO de propósito
            issuance_reason="x",
        )
        vr = VerifierRegistry(authority_registry=ar)
        with self.assertRaises(RegistrationDenied):
            vr.register(
                VerifierRegistration(verifier_id="v1", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.HIGH),
                authority_grant_id=grant.grant_id, registration_reason="x",
            )

    def test_valid_governance_registration_succeeds(self):
        _ar, grant_id = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER, authority_level="OWNER")
        vr = VerifierRegistry(authority_registry=_ar)
        vr.register(
            VerifierRegistration(verifier_id="v1", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD),
            authority_grant_id=grant_id, registration_reason="aprovado",
        )
        self.assertTrue(vr.is_authorized("v1"))

    def test_audit_trail_always_recorded(self):
        vr = VerifierRegistry()
        try:
            vr.register(
                VerifierRegistration(verifier_id="self", verifier_type="x", authorized=True),
                authority_grant_id="grant_inexistente", registration_reason="x",
            )
        except RegistrationDenied:
            pass
        self.assertEqual(len(vr.audit_log), 1)
        self.assertEqual(vr.audit_log[0]["outcome"], "DENIED")
        self.assertIn("audit_id", vr.audit_log[0])


class SourceRegistryNoBypassTests(unittest.TestCase):
    """Itens 4/5/6 — mesmo padrão para SourceRegistry."""

    def test_18_direct_fake_official_source_registration_denied(self):
        """Item 6, Teste 18: registrar fonte falsa como TRUSTED_OFFICIAL
        sem um AuthorityGrant real — sempre recusado."""
        sr = SourceRegistry()
        with self.assertRaises(SourceRegistrationDenied):
            sr.register(
                SourceRegistration(
                    source_id="fake_source", source_type=SourceType.OFFICIAL, source_reference="fake://source",
                    requested_trust_class=TrustClass.TRUSTED_OFFICIAL, health=CapabilityHealth.HEALTHY,
                    requested_verification_status=VerificationStatus.VERIFIED,
                ),
                authority_grant_id="", registration_reason="quero criar minha fonte",
            )
        self.assertFalse(sr.can_sustain_fact("fake_source"))

    def test_trusted_official_requires_owner_or_governance_not_atlas_admin_alone(self):
        _ar, grant_id = make_authority_grant(scope=SCOPE_REGISTER_SOURCE, authority_level="ATLAS_ADMIN")
        sr = SourceRegistry(authority_registry=_ar)
        with self.assertRaises(SourceRegistrationDenied):
            sr.register(
                SourceRegistration(
                    source_id="s1", source_type=SourceType.OFFICIAL, source_reference="x",
                    requested_trust_class=TrustClass.TRUSTED_OFFICIAL, health=CapabilityHealth.HEALTHY,
                    requested_verification_status=VerificationStatus.VERIFIED,
                ),
                authority_grant_id=grant_id, registration_reason="promovendo",
            )

    def test_requested_vs_effective_fields_separated(self):
        _ar, grant_id = make_authority_grant(scope=SCOPE_REGISTER_SOURCE, authority_level="OWNER")
        sr = SourceRegistry(authority_registry=_ar)
        reg = SourceRegistration(
            source_id="s1", source_type=SourceType.FIRST_PARTY, source_reference="x",
            requested_trust_class=TrustClass.TRUSTED_FIRST_PARTY, health=CapabilityHealth.HEALTHY,
            requested_verification_status=VerificationStatus.VERIFIED,
        )
        # antes do register(), campos efetivos nunca ja vem elevados
        self.assertEqual(reg.trust_class, TrustClass.UNKNOWN)
        self.assertEqual(reg.verification_status, VerificationStatus.UNVERIFIED)
        sr.register(reg, authority_grant_id=grant_id, registration_reason="x")
        self.assertEqual(reg.trust_class, TrustClass.TRUSTED_FIRST_PARTY)
        self.assertEqual(reg.verification_status, VerificationStatus.VERIFIED)


class MandatoryClaimBindingForFactTests(unittest.TestCase):
    """Itens 7/8/9/10 — claim binding obrigatório para FACT."""

    def test_19_fact_without_primary_claim_id_blocked(self):
        """Item 19 exato: FACT com ROI, evidência válida, mas sem
        primary_claim_id → BLOCKED/CLAIM_REQUIRED."""
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_real", provenance="x", observed_at=time.time(),
            modality=Modality.DOCUMENT, confidence=0.9, related_strategy_id="s1", source_type="OFFICIAL",
            requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy})
        self.assertNotEqual(result.verdict, Verdict.APPROVED)
        self.assertTrue(any("CLAIM" in r for r in result.reasons) or any("claim" in u.lower() for u in result.criteria.get("unsupported_claims", [])))

    def test_20_claim_bound_to_different_strategy_binding_error(self):
        """Item 20 exato: strategy=S1, claim=claim-S1, mas
        Evidence.related_claim_id=claim-S2 → CLAIM_BINDING_ERROR."""
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.claim_registry.register(ClaimRecord(claim_id="claim-S1", strategy_id="S1", claim_type="FACT", statement="x"))
        registry.claim_registry.register(ClaimRecord(claim_id="claim-S2", strategy_id="S2", claim_type="FACT", statement="x"))
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_real", provenance="x", observed_at=time.time(),
            modality=Modality.DOCUMENT, confidence=0.9, related_strategy_id="S1", related_claim_id="claim-S2",
            source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED,
            verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        self.assertNotEqual(registry.resolve_status("ev1"), VerificationStatus.VERIFIED)

    def test_valid_claim_binding_allows_fact(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_real", provenance="x", observed_at=time.time(),
            modality=Modality.DOCUMENT, confidence=0.9, related_strategy_id="s1", source_type="OFFICIAL",
            requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
            primary_claim_id="claim-s1",
        )
        result = gate.evaluate({"strategy": strategy})
        self.assertEqual(result.verdict, Verdict.APPROVED)


class ProvenanceStrategyClaimMismatchTests(unittest.TestCase):
    """Itens 11/12 — ProvenanceRecord strategy/claim mismatch."""

    def test_21_provenance_from_different_strategy_mismatch(self):
        """Item 21 exato."""
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev-roi", type="x", source="fonte_real", provenance="x", observed_at=time.time(),
            modality=Modality.STRUCTURED_DATA, confidence=0.9, source_type="OFFICIAL",
            requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        validator = MetricEvidenceValidator(evidence_registry=registry)
        me = MetricEvidence(
            metric_name="roi", value=1.5, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-roi"],
            provenance_record=ProvenanceRecord(
                source_type="OWN_OPERATION_DATA", source_reference="fonte_real", collection_method="x", collector="x",
                verifier="teste_automatizado", verification_method="validacao_manual", integrity_reference="sha256:x",
                related_evidence_ids=["ev-roi"], related_strategy_id="S2",
            ),
        )
        outcome = validator.validate(requested_key="roi", metric_evidence=me, strategy_id="S1")
        self.assertFalse(outcome.is_valid)
        self.assertTrue(any("PROVENANCE_STRATEGY_MISMATCH" in i for i in outcome.issues))

    def test_benchmark_scope_provenance_allows_cross_strategy(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev-roi", type="x", source="fonte_real", provenance="x", observed_at=time.time(),
            modality=Modality.STRUCTURED_DATA, confidence=0.9, source_type="OFFICIAL",
            requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        validator = MetricEvidenceValidator(evidence_registry=registry)
        me = MetricEvidence(
            metric_name="roi", value=1.5, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-roi"],
            provenance_record=ProvenanceRecord(
                source_type="OWN_OPERATION_DATA", source_reference="fonte_real", collection_method="x", collector="x",
                verifier="teste_automatizado", verification_method="validacao_manual", integrity_reference="sha256:x",
                related_evidence_ids=["ev-roi"], related_strategy_id="S2", scope="BENCHMARK",
            ),
        )
        outcome = validator.validate(requested_key="roi", metric_evidence=me, strategy_id="S1")
        self.assertTrue(outcome.is_valid)


class StrategySourceResolutionTests(unittest.TestCase):
    """Itens 13/14 — strategy.source deve resolver no SourceRegistry."""

    def test_22_unregistered_strategy_source_blocks_fact(self):
        """Item 22 exato: strategy.source não registrada, mesmo com
        evidence válida de outra fonte."""
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_real", provenance="x", observed_at=time.time(),
            modality=Modality.DOCUMENT, confidence=0.9, related_strategy_id="s1", source_type="OFFICIAL",
            requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fake://x", evidence_ids=["ev1"], observed_at=time.time(),
            primary_claim_id="claim-s1",
        )
        result = gate.evaluate({"strategy": strategy})
        self.assertNotEqual(result.verdict, Verdict.APPROVED)

    def test_registered_strategy_source_allows_fact(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_real", provenance="x", observed_at=time.time(),
            modality=Modality.DOCUMENT, confidence=0.9, related_strategy_id="s1", source_type="OFFICIAL",
            requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
            primary_claim_id="claim-s1",
        )
        result = gate.evaluate({"strategy": strategy})
        self.assertEqual(result.verdict, Verdict.APPROVED)


class FullRootOfTrustChainTests(unittest.TestCase):
    """Itens 15-17 + Testes 18/23 — cadeia completa de confiança."""

    def _governed_registries(self):
        _ar_v, _grant_v = make_authority_grant(scope=SCOPE_REGISTER_VERIFIER, authority_level="OWNER", actor_id="owner@empresa.com")
        vr = VerifierRegistry(authority_registry=_ar_v)
        vr.register(
            VerifierRegistration(verifier_id="v1", verifier_type="human_reviewer", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.HIGH),
            authority_grant_id=_grant_v, registration_reason="verificador aprovado",
        )
        _ar_s, _grant_s = make_authority_grant(scope=SCOPE_REGISTER_SOURCE, authority_level="OWNER", actor_id="owner@empresa.com")
        sr = SourceRegistry(authority_registry=_ar_s)
        sr.register(
            SourceRegistration(
                source_id="fonte_governada", source_type=SourceType.OWN_OPERATION_DATA, source_reference="x",
                requested_trust_class=TrustClass.TRUSTED_FIRST_PARTY, health=CapabilityHealth.HEALTHY,
                requested_verification_status=VerificationStatus.VERIFIED,
            ),
            authority_grant_id=_grant_s, registration_reason="fonte aprovada",
        )
        return vr, sr

    def test_18_complete_self_fabricated_root_of_trust_blocked(self):
        """Teste 18 — ataque completo tentando fabricar a PRÓPRIA raiz
        de confiança (verifier + source), sempre bloqueado na raiz."""
        vr = VerifierRegistry()
        with self.assertRaises(RegistrationDenied):
            vr.register(
                VerifierRegistration(verifier_id="self", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.HIGH),
                authority_grant_id="grant_fabricado_pelo_atacante", registration_reason="self",
            )
        sr = SourceRegistry()
        with self.assertRaises(SourceRegistrationDenied):
            sr.register(
                SourceRegistration(
                    source_id="fake", source_type=SourceType.OFFICIAL, source_reference="fake://x",
                    requested_trust_class=TrustClass.TRUSTED_OFFICIAL, health=CapabilityHealth.HEALTHY,
                    requested_verification_status=VerificationStatus.VERIFIED,
                ),
                authority_grant_id="grant_fabricado_pelo_atacante", registration_reason="fake",
            )
        # a raiz nunca foi criada -- qualquer evidencia subsequente nunca resolve
        registry = EvidenceRegistry(verifier_registry=vr, source_registry=sr)
        record = registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fake", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, verifier="self", verification_method="m",
            requested_verification_status=VerificationStatus.VERIFIED,
        ))
        self.assertNotEqual(record.verification_status, VerificationStatus.VERIFIED)

    def test_23_fully_governed_valid_chain_reaches_adopted(self):
        """Teste 23 — cadeia válida completa (governança real em cada
        elo) chega a ADOPTED."""
        vr, sr = self._governed_registries()
        registry = EvidenceRegistry(verifier_registry=vr, source_registry=sr)
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_governada", provenance="x", observed_at=time.time(),
            modality=Modality.DOCUMENT, confidence=0.9, related_strategy_id="s1", verifier="v1",
            verification_method="m", requested_verification_status=VerificationStatus.VERIFIED,
        ))
        registry.register(EvidenceRecord(
            evidence_id="ev-roi", type="x", source="fonte_governada", provenance="x", observed_at=time.time(),
            modality=Modality.STRUCTURED_DATA, confidence=0.9, related_strategy_id="s1", related_metric_name="roi",
            verifier="v1", verification_method="m", requested_verification_status=VerificationStatus.VERIFIED,
        ))
        register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")

        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=registry))
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte_governada", evidence_ids=["ev1"], observed_at=time.time(),
            primary_claim_id="claim-s1",
        )
        pipeline.run_quality_gate(record.record_id, strategy=strategy)
        roi_evidence = MetricEvidence(
            metric_name="roi", value=1.8, source="x", provenance="x", observed_at=time.time(),
            context="campanha", measurement_type="calculado", confidence=0.9, evidence_ids=["ev-roi"],
            provenance_record=ProvenanceRecord(
                source_type="OWN_OPERATION_DATA", source_reference="fonte_governada", collection_method="export",
                collector="agent", verifier="v1", verification_method="m", integrity_reference="sha256:x",
                related_evidence_ids=["ev-roi"], related_strategy_id="s1",
            ),
        )
        pipeline.record_metrics(record.record_id, metrics={"roi": roi_evidence}, provenance="teste controlado")
        final = pipeline.validate(record.record_id, decision="ADOPTED")
        self.assertEqual(final.status, AdoptionStatus.ADOPTED)


if __name__ == "__main__":
    unittest.main()
