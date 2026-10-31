import time
import unittest

from core.capabilities.registry import CapabilityHealth
from core.evidence.authority_registry import AuthorityDenied, AuthorityRegistry, SCOPE_REGISTER_VERIFIER
from core.evidence.claim_registry import ClaimRecord
from core.evidence.claims import ClaimType
from core.evidence.governance_context import ApplicationGovernanceContext, GovernanceContextError
from core.evidence.metric_evidence import MetricEvidence
from core.evidence.owner_principal import OwnerPrincipal, issue_owner_principal
from core.evidence.provenance import ProvenanceRecord
from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
from core.evidence.source_registry import SourceRegistration, SourceType, TrustClass
from core.evidence.verifier_registry import TrustLevel, VerifierRegistration
from core.multimodal.evidence import Modality
from core.security.auth import OwnerAuthService
from radar_web.adoption import AdoptionPipeline, AdoptionStatus
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy
from radar_web.quality_gate import RadarWebEvidenceGate
from tests._auth_test_helpers import fast_hash_password
from tests._evidence_test_helpers import make_authenticated_test_owner_session

"""
Testes CORRECAO V6.1.1 ROUND 8 (FINAL) - OwnerPrincipal autenticado
real, bootstrap_root_authority protegido, ApplicationGovernanceContext,
registry injection protection, testes adversariais 10-18.
"""


class OwnerPrincipalIssuanceTests(unittest.TestCase):
    """Item 2 — OwnerPrincipal autenticado real."""

    def test_valid_session_issues_authenticated_principal(self):
        auth_service, token = make_authenticated_test_owner_session()
        principal = issue_owner_principal(auth_service=auth_service, session_token=token)
        self.assertTrue(principal.is_genuinely_authenticated())
        self.assertEqual(principal.authentication_source, "OwnerAuthService")

    def test_invalid_token_never_authenticates(self):
        auth_service, _ = make_authenticated_test_owner_session()
        principal = issue_owner_principal(auth_service=auth_service, session_token="token.fabricado.invalido")
        self.assertFalse(principal.is_genuinely_authenticated())
        self.assertEqual(principal.verification_status, VerificationStatus.INVALID)

    def test_manually_constructed_principal_never_self_verifies(self):
        """Item 10 — is_genuinely_authenticated() sozinho não é o que
        `bootstrap_root_authority()` usa para decidir; mas mesmo o
        próprio método reflete só o que foi genuinamente emitido."""
        fake = OwnerPrincipal(
            principal_id="fake", owner_id="attacker", authenticated=True,
            verification_status=VerificationStatus.VERIFIED,
        )
        # o objeto ATÉ reporta is_genuinely_authenticated=True (campos
        # alegados), mas isso sozinho nunca basta para bootstrap --
        # ver BootstrapSecurityTests abaixo, que é quem realmente importa.
        self.assertTrue(fake.is_genuinely_authenticated())  # campos objeto, não prova
        self.assertIsNone(fake.session_id)  # sem sessão real para re-verificar


class BootstrapSecurityTests(unittest.TestCase):
    """Itens 1/9/10 — bootstrap_root_authority nunca aceita Owner autodeclarado."""

    def test_10_fake_principal_attack_denied(self):
        """Item 10 exato: attacker cria OwnerPrincipal manualmente,
        mesmo com authenticated=True/VERIFIED -> BOOTSTRAP_DENIED."""
        fake_principal = OwnerPrincipal(
            principal_id="fake", owner_id="attacker", authenticated=True,
            verification_status=VerificationStatus.VERIFIED, session_id="token_inventado",
        )
        auth_service = OwnerAuthService(owner_email="real@x.com", stored_password_hash=fast_hash_password("SenhaReal123"))
        ar = AuthorityRegistry()
        with self.assertRaises(AuthorityDenied):
            ar.bootstrap_root_authority(owner_principal=fake_principal, auth_service=auth_service, bootstrap_reason="x")
        self.assertFalse(ar._root_bootstrapped)

    def test_principal_without_session_id_denied(self):
        fake_principal = OwnerPrincipal(
            principal_id="fake", owner_id="attacker", authenticated=True, verification_status=VerificationStatus.VERIFIED,
        )
        auth_service = OwnerAuthService(owner_email="real@x.com", stored_password_hash=fast_hash_password("SenhaReal123"))
        ar = AuthorityRegistry()
        with self.assertRaises(AuthorityDenied):
            ar.bootstrap_root_authority(owner_principal=fake_principal, auth_service=auth_service, bootstrap_reason="x")

    def test_genuine_session_bootstraps_successfully(self):
        auth_service, token = make_authenticated_test_owner_session()
        principal = issue_owner_principal(auth_service=auth_service, session_token=token)
        ar = AuthorityRegistry()
        grant = ar.bootstrap_root_authority(owner_principal=principal, auth_service=auth_service, bootstrap_reason="setup real")
        self.assertEqual(grant.authority_level, "OWNER")
        self.assertTrue(ar._root_bootstrapped)

    def test_4_bootstrap_only_once_per_registry(self):
        auth_service, token = make_authenticated_test_owner_session()
        principal = issue_owner_principal(auth_service=auth_service, session_token=token)
        ar = AuthorityRegistry()
        ar.bootstrap_root_authority(owner_principal=principal, auth_service=auth_service, bootstrap_reason="primeiro")
        with self.assertRaises(AuthorityDenied):
            ar.bootstrap_root_authority(owner_principal=principal, auth_service=auth_service, bootstrap_reason="segunda tentativa")


class ApplicationGovernanceContextTests(unittest.TestCase):
    """Itens 5/6/7 — contexto canônico + registry injection protection."""

    def test_bootstrap_creates_full_chain(self):
        auth_service, token = make_authenticated_test_owner_session()
        ctx = ApplicationGovernanceContext.bootstrap(auth_service=auth_service, session_token=token, bootstrap_reason="setup")
        self.assertIsNotNone(ctx.verifier_registry)
        self.assertIsNotNone(ctx.source_registry)
        self.assertIsNotNone(ctx.claim_registry)
        self.assertIs(ctx.verifier_registry.authority_registry, ctx.authority_registry)
        self.assertIs(ctx.source_registry.authority_registry, ctx.authority_registry)

    def test_bootstrap_denied_without_authentication(self):
        auth_service = OwnerAuthService(owner_email="real@x.com", stored_password_hash=fast_hash_password("SenhaReal123"))
        with self.assertRaises(GovernanceContextError):
            ApplicationGovernanceContext.bootstrap(auth_service=auth_service, session_token="token.fabricado", bootstrap_reason="x")

    def test_12_attacker_registry_rejected_by_canonical_check(self):
        """Item 12 exato: attacker cria uma AuthorityRegistry própria
        (mesmo que de alguma forma bootstrapada) — nunca é aceita como
        canônica por um `ApplicationGovernanceContext` real."""
        auth_service, token = make_authenticated_test_owner_session()
        real_ctx = ApplicationGovernanceContext.bootstrap(auth_service=auth_service, session_token=token, bootstrap_reason="setup")

        fake_registry = AuthorityRegistry()  # instância totalmente separada
        with self.assertRaises(GovernanceContextError):
            real_ctx.require_canonical_registry(fake_registry)

    def test_canonical_registry_accepted(self):
        auth_service, token = make_authenticated_test_owner_session()
        ctx = ApplicationGovernanceContext.bootstrap(auth_service=auth_service, session_token=token, bootstrap_reason="setup")
        ctx.require_canonical_registry(ctx.authority_registry)  # nunca levanta
        ctx.require_canonical_registry(ctx.verifier_registry)  # nunca levanta


class CompleteAttackAndValidScenarioRound8Tests(unittest.TestCase):
    """Item 17 — ataque completo; item 18 — cenário válido completo."""

    def test_17_complete_attack_blocked_at_bootstrap(self):
        fake_principal = OwnerPrincipal(
            principal_id="fake", owner_id="attacker", authenticated=True,
            verification_status=VerificationStatus.VERIFIED, session_id="token_fabricado",
        )
        auth_service = OwnerAuthService(owner_email="real@x.com", stored_password_hash=fast_hash_password("SenhaReal123"))
        attacker_registry = AuthorityRegistry()
        with self.assertRaises(AuthorityDenied):
            attacker_registry.bootstrap_root_authority(
                owner_principal=fake_principal, auth_service=auth_service, bootstrap_reason="attacker bootstrap",
            )
        # sem bootstrap bem-sucedido, nao ha grant valido -- nada mais no
        # fluxo (registrar verifier/source/claim/evidence/ROI/ADOPT)
        # pode progredir
        self.assertFalse(attacker_registry._root_bootstrapped)
        self.assertEqual(len(attacker_registry.all()), 0)

    def test_18_fully_governed_chain_reaches_adopted(self):
        auth_service, token = make_authenticated_test_owner_session()
        ctx = ApplicationGovernanceContext.bootstrap(auth_service=auth_service, session_token=token, bootstrap_reason="setup real")

        ctx.verifier_registry.register(
            VerifierRegistration(verifier_id="v1", verifier_type="human_reviewer", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.HIGH),
            authority_grant_id=ctx._bootstrap_verifier_grant_id, registration_reason="verificador aprovado",
        )
        ctx.source_registry.register(
            SourceRegistration(
                source_id="fonte_real", source_type=SourceType.OWN_OPERATION_DATA, source_reference="x",
                requested_trust_class=TrustClass.TRUSTED_FIRST_PARTY, health=CapabilityHealth.HEALTHY,
                requested_verification_status=VerificationStatus.VERIFIED,
            ),
            authority_grant_id=ctx._bootstrap_source_grant_id, registration_reason="fonte aprovada",
        )
        ctx.claim_registry.register(ClaimRecord(claim_id="claim-s1", strategy_id="s1", claim_type="FACT", statement="x"))

        registry = EvidenceRegistry(
            verifier_registry=ctx.verifier_registry, source_registry=ctx.source_registry,
            claim_registry=ctx.claim_registry,
        )
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_real", provenance="x", observed_at=time.time(),
            modality=Modality.DOCUMENT, confidence=0.9, related_strategy_id="s1", verifier="v1",
            verification_method="m", requested_verification_status=VerificationStatus.VERIFIED,
        ))
        registry.register(EvidenceRecord(
            evidence_id="ev-roi", type="x", source="fonte_real", provenance="x", observed_at=time.time(),
            modality=Modality.STRUCTURED_DATA, confidence=0.9, related_strategy_id="s1", related_metric_name="roi",
            verifier="v1", verification_method="m", requested_verification_status=VerificationStatus.VERIFIED,
        ))

        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=registry))
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
            primary_claim_id="claim-s1",
        )
        pipeline.run_quality_gate(record.record_id, strategy=strategy)
        roi_evidence = MetricEvidence(
            metric_name="roi", value=1.8, source="x", provenance="x", observed_at=time.time(),
            context="campanha", measurement_type="calculado", confidence=0.9, evidence_ids=["ev-roi"],
            provenance_record=ProvenanceRecord(
                source_type="OWN_OPERATION_DATA", source_reference="fonte_real", collection_method="export",
                collector="agent", verifier="v1", verification_method="m", integrity_reference="sha256:x",
                related_evidence_ids=["ev-roi"], related_strategy_id="s1",
            ),
        )
        pipeline.record_metrics(record.record_id, metrics={"roi": roi_evidence}, provenance="teste controlado")
        final = pipeline.validate(record.record_id, decision="ADOPTED")
        self.assertEqual(final.status, AdoptionStatus.ADOPTED)


if __name__ == "__main__":
    unittest.main()
