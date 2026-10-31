import os
import tempfile
import time
import unittest

from core.capabilities.registry import CapabilityHealth
from core.evidence.authority_registry import (
    AuthorityDenied, AuthorityRegistry, SCOPE_REGISTER_SOURCE, SCOPE_REGISTER_VERIFIER,
)
from core.evidence.claim_registry import ClaimRecord
from core.evidence.claims import ClaimType
from core.evidence.governance_context import ApplicationGovernanceContext, GovernanceContextError
from core.evidence.metric_evidence import MetricEvidence
from core.evidence.owner_principal import issue_owner_principal
from core.evidence.provenance import ProvenanceRecord
from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
from core.evidence.source_registry import SourceRegistration, SourceType, TrustClass, SourceRegistry
from core.evidence.verifier_registry import TrustLevel, VerifierRegistration, VerifierRegistry
from core.models import db as db_module
from core.multimodal.evidence import Modality
from core.security.auth import OwnerAuthService, create_canonical_auth_service, hash_password, is_canonical_auth_service
from radar_web.adoption import AdoptionPipeline, AdoptionStatus
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy
from radar_web.quality_gate import RadarWebEvidenceGate

"""
Testes CORRECAO V6.1.1 ROUND 9 (GATE ZERO) - OwnerAuthService
canonico, bloqueio de auth_service/principal/context/registry
fabricados, persistencia entre restarts, propagacao de revogacao,
isolamento de tenant/app, ataque completo bloqueado, cenario valido
via GrowthStudioApp real.
"""


class CanonicalAuthServiceTests(unittest.TestCase):
    """Itens 1/2/3/18 — OwnerAuthService canônico não fabricável."""

    def test_canonical_marker_present_only_via_factory(self):
        canonical = create_canonical_auth_service(owner_email="x@x.com", stored_password_hash=hash_password("Senha1234"))
        manual = OwnerAuthService(owner_email="x@x.com", stored_password_hash=hash_password("Senha1234"))
        self.assertTrue(is_canonical_auth_service(canonical))
        self.assertFalse(is_canonical_auth_service(manual))

    def test_identical_implementation_never_passes_as_canonical(self):
        """Item 3: mesmo uma implementação idêntica criada externamente NÃO pode ser aceita."""
        identical_looking = OwnerAuthService(
            owner_email="x@x.com", stored_password_hash=hash_password("Senha1234"),
            application_id="default", tenant_id="default",
        )
        self.assertFalse(is_canonical_auth_service(identical_looking))

    def test_18_fake_auth_service_bootstrap_denied(self):
        """Item 18 exato: attacker cria fake_auth = OwnerAuthService(...),
        faz login, tenta bootstrap -> UNTRUSTED_AUTH_SERVICE."""
        fake_auth = OwnerAuthService(owner_email="attacker@evil.com", stored_password_hash=hash_password("SenhaAtacante123"))
        fake_token = fake_auth.login("attacker@evil.com", "SenhaAtacante123")
        with self.assertRaises(GovernanceContextError) as ctx:
            ApplicationGovernanceContext.bootstrap_for_app(auth_service=fake_auth, session_token=fake_token, bootstrap_reason="attacker")
        self.assertEqual(ctx.exception.error_code, "UNTRUSTED_AUTH_SERVICE")

    def test_direct_registry_bootstrap_also_rejects_non_canonical(self):
        """A checagem não vive só em ApplicationGovernanceContext —
        AuthorityRegistry.bootstrap_root_authority() em si também
        recusa, mesmo chamado diretamente."""
        fake_auth = OwnerAuthService(owner_email="attacker@evil.com", stored_password_hash=hash_password("SenhaAtacante123"))
        fake_token = fake_auth.login("attacker@evil.com", "SenhaAtacante123")
        principal = issue_owner_principal(auth_service=fake_auth, session_token=fake_token)
        ar = AuthorityRegistry()
        with self.assertRaises(AuthorityDenied):
            ar.bootstrap_root_authority(owner_principal=principal, auth_service=fake_auth, bootstrap_reason="x")
        self.assertFalse(ar._root_bootstrapped)
        self.assertEqual(len(ar.all()), 0)


class FakePrincipalAndContextAttackTests(unittest.TestCase):
    """Itens 4/19/6/20 — fake OwnerPrincipal / fake governance context."""

    def test_19_fake_principal_attack(self):
        from core.evidence.owner_principal import OwnerPrincipal

        fake = OwnerPrincipal(
            principal_id="fake", owner_id="attacker", authenticated=True,
            verification_status=VerificationStatus.VERIFIED, session_id="token_fabricado",
        )
        auth = create_canonical_auth_service(owner_email="real@x.com", stored_password_hash=hash_password("SenhaReal123"))
        ar = AuthorityRegistry()
        with self.assertRaises(AuthorityDenied):
            ar.bootstrap_root_authority(owner_principal=fake, auth_service=auth, bootstrap_reason="x")

    def test_20_fake_governance_context_rejected(self):
        """Item 20 exato: attacker cria governance context próprio
        (manualmente, sem bootstrap_for_app) -> UNTRUSTED_GOVERNANCE_CONTEXT."""
        fake_context = ApplicationGovernanceContext(
            context_id="fake_ctx", application_id="default", tenant_id="default",
            authority_registry=AuthorityRegistry(), verifier_registry=VerifierRegistry(),
            source_registry=SourceRegistry(), claim_registry=__import__(
                "core.evidence.claim_registry", fromlist=["ClaimRegistry"],
            ).ClaimRegistry(),
            owner_id="attacker", auth_service_id="fake",
        )
        self.assertFalse(fake_context.is_canonical())
        with self.assertRaises(GovernanceContextError) as ctx:
            fake_context.require_canonical_registry(fake_context.authority_registry)
        self.assertEqual(ctx.exception.error_code, "UNTRUSTED_GOVERNANCE_CONTEXT")


class FakeAuthorityRegistryInjectionTests(unittest.TestCase):
    """Itens 7/8/21 — fake AuthorityRegistry rejeitada."""

    def test_21_attacker_registry_rejected(self):
        """Item 21 exato."""
        auth, token = create_canonical_auth_service(
            owner_email="real@x.com", stored_password_hash=hash_password("SenhaReal123"),
        ), None
        token = auth.login("real@x.com", "SenhaReal123")
        real_ctx = ApplicationGovernanceContext.bootstrap_for_app(auth_service=auth, session_token=token, bootstrap_reason="x")
        attacker_registry = AuthorityRegistry()
        with self.assertRaises(GovernanceContextError) as ctx:
            real_ctx.require_canonical_registry(attacker_registry)
        self.assertEqual(ctx.exception.error_code, "AUTHORITY_REGISTRY_REJECTED")


class RootAuthorityPersistenceTests(unittest.TestCase):
    """Item 10 — persistência entre restarts."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def test_owner_a_bootstrap_restart_owner_b_denied(self):
        """Cenário exato do item 10."""
        auth_a = create_canonical_auth_service(
            owner_email="ownerA@x.com", stored_password_hash=hash_password("SenhaA1234"), db_path=self.db_path,
        )
        token_a = auth_a.login("ownerA@x.com", "SenhaA1234")
        principal_a = issue_owner_principal(auth_service=auth_a, session_token=token_a)
        ar1 = AuthorityRegistry(db_path=self.db_path)
        ar1.bootstrap_root_authority(owner_principal=principal_a, auth_service=auth_a, bootstrap_reason="owner A")
        self.assertTrue(ar1._root_bootstrapped)

        # SIMULA RESTART: nova instância, mesmo db_path
        ar2 = AuthorityRegistry(db_path=self.db_path)
        self.assertTrue(ar2._root_bootstrapped)  # já reconhece antes de qualquer chamada

        auth_b = create_canonical_auth_service(
            owner_email="ownerB@x.com", stored_password_hash=hash_password("SenhaB1234"), db_path=self.db_path,
        )
        token_b = auth_b.login("ownerB@x.com", "SenhaB1234")
        principal_b = issue_owner_principal(auth_service=auth_b, session_token=token_b)
        with self.assertRaises(AuthorityDenied):
            ar2.bootstrap_root_authority(owner_principal=principal_b, auth_service=auth_b, bootstrap_reason="owner B")

    def test_fresh_db_allows_bootstrap(self):
        ar = AuthorityRegistry(db_path=self.db_path)
        self.assertFalse(ar._root_bootstrapped)


class GrantRevocationPropagationTests(unittest.TestCase):
    """Itens 14/15/16/17 — revalidação contínua de grant."""

    def _bootstrap(self):
        auth = create_canonical_auth_service(owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"))
        token = auth.login("owner@x.com", "SenhaForte123")
        principal = issue_owner_principal(auth_service=auth, session_token=token)
        ar = AuthorityRegistry()
        root = ar.bootstrap_root_authority(owner_principal=principal, auth_service=auth, bootstrap_reason="x")
        return ar, root

    def test_17_source_revoked_grant_stops_sustaining_fact(self):
        ar, root = self._bootstrap()
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="owner@x.com", authority_level="OWNER",
            scopes=[SCOPE_REGISTER_SOURCE], issuance_reason="x",
        )
        sr = SourceRegistry(authority_registry=ar)
        sr.register(
            SourceRegistration(
                source_id="source_a", source_type=SourceType.OWN_OPERATION_DATA, source_reference="x",
                requested_trust_class=TrustClass.TRUSTED_FIRST_PARTY, health=CapabilityHealth.HEALTHY,
                requested_verification_status=VerificationStatus.VERIFIED,
            ),
            authority_grant_id=grant.grant_id, registration_reason="x",
        )
        self.assertTrue(sr.can_sustain_fact("source_a"))
        ar.revoke_grant(grant.grant_id, revoked_by="owner@x.com", reason="comprometido")
        self.assertFalse(sr.can_sustain_fact("source_a"))

    def test_17_verifier_revoked_grant_stops_authorizing(self):
        ar, root = self._bootstrap()
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="owner@x.com", authority_level="OWNER",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",
        )
        vr = VerifierRegistry(authority_registry=ar)
        vr.register(
            VerifierRegistration(verifier_id="v1", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD),
            authority_grant_id=grant.grant_id, registration_reason="x",
        )
        self.assertTrue(vr.is_authorized("v1"))
        ar.revoke_grant(grant.grant_id, revoked_by="owner@x.com", reason="comprometido")
        self.assertFalse(vr.is_authorized("v1"))


class TenantAppIsolationTests(unittest.TestCase):
    """Itens 24/25 — isolamento de tenant/app."""

    def _ctx(self, *, app_id, tenant_id, email):
        auth = create_canonical_auth_service(
            owner_email=email, stored_password_hash=hash_password("SenhaForte1234"),
            application_id=app_id, tenant_id=tenant_id,
        )
        token = auth.login(email, "SenhaForte1234")
        return ApplicationGovernanceContext.bootstrap_for_app(
            auth_service=auth, session_token=token, bootstrap_reason="x", application_id=app_id, tenant_id=tenant_id,
        )

    def test_24_tenant_isolation(self):
        ctx_a = self._ctx(app_id="app1", tenant_id="tenantA", email="ownerA@x.com")
        ctx_b = self._ctx(app_id="app1", tenant_id="tenantB", email="ownerB@x.com")
        with self.assertRaises(GovernanceContextError) as ctx:
            ctx_a.require_same_tenant_app(ctx_b.verifier_registry)
        self.assertEqual(ctx.exception.error_code, "REGISTRY_CONTEXT_MISMATCH")

    def test_25_app_isolation(self):
        ctx_a = self._ctx(app_id="app1", tenant_id="tenantA", email="ownerC@x.com")
        ctx_c = self._ctx(app_id="app2", tenant_id="tenantA", email="ownerD@x.com")
        with self.assertRaises(GovernanceContextError) as ctx:
            ctx_a.require_same_tenant_app(ctx_c.verifier_registry)
        self.assertEqual(ctx.exception.error_code, "REGISTRY_CONTEXT_MISMATCH")

    def test_same_context_accepted(self):
        ctx = self._ctx(app_id="app1", tenant_id="tenantA", email="ownerE@x.com")
        ctx.require_same_tenant_app(ctx.verifier_registry)  # nunca levanta


class CompleteAttackAndValidScenarioViaAppTests(unittest.TestCase):
    """Item 22 — ataque completo; item 23 — cenário válido via GrowthStudioApp real."""

    def test_22_complete_attack_never_reaches_real_app_governance(self):
        from api.bootstrap import build_app

        app = build_app(
            owner_email="owner_real@empresa.com", owner_password_hash=hash_password("SenhaOwnerReal123"),
            persistent=False,
        )
        self.assertIsNone(app.governance_context)

        # 1-3: attacker cria proprio OwnerAuthService, credenciais proprias, autentica
        fake_auth = OwnerAuthService(owner_email="attacker@evil.com", stored_password_hash=hash_password("SenhaAtacante123"))
        fake_token = fake_auth.login("attacker@evil.com", "SenhaAtacante123")

        # 4: tenta bootstrap
        with self.assertRaises(GovernanceContextError):
            ApplicationGovernanceContext.bootstrap_for_app(auth_service=fake_auth, session_token=fake_token, bootstrap_reason="attacker")

        # 5-7: fake governance context / fake AuthorityRegistry / tentar virar Owner -- mesmo na propria
        # AuthorityRegistry isolada do atacante, o bootstrap eh recusado (auth_service nao-canonico)
        fake_principal = issue_owner_principal(auth_service=fake_auth, session_token=fake_token)
        fake_ar = AuthorityRegistry()
        with self.assertRaises(AuthorityDenied):
            fake_ar.bootstrap_root_authority(owner_principal=fake_principal, auth_service=fake_auth, bootstrap_reason="attacker registry")

        # a app REAL nunca foi contaminada
        self.assertIsNone(app.governance_context)

    def test_23_valid_scenario_via_real_app_reaches_adopted(self):
        from api.bootstrap import build_app

        app = build_app(
            owner_email="owner_real@empresa.com", owner_password_hash=hash_password("SenhaOwnerReal123"),
            persistent=False,
        )
        ctx = app.bootstrap_governance_context(
            email="owner_real@empresa.com", password="SenhaOwnerReal123", bootstrap_reason="setup real",
        )
        self.assertIs(app.governance_context, ctx)

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

    def test_governance_bootstrap_only_once_per_app(self):
        from api.bootstrap import build_app

        app = build_app(
            owner_email="owner_real@empresa.com", owner_password_hash=hash_password("SenhaOwnerReal123"),
            persistent=False,
        )
        app.bootstrap_governance_context(email="owner_real@empresa.com", password="SenhaOwnerReal123", bootstrap_reason="primeiro")
        with self.assertRaises(GovernanceContextError):
            app.bootstrap_governance_context(email="owner_real@empresa.com", password="SenhaOwnerReal123", bootstrap_reason="segunda tentativa")


if __name__ == "__main__":
    unittest.main()


class OwnerApprovalIntegrationTests(unittest.TestCase):
    """Item 12/13 — Owner Approval real integrado (nunca paralelo)."""

    def _setup(self):
        from atlas.escalation import EscalationDesk
        from core.security.audit import AuditTrail

        auth = create_canonical_auth_service(owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"))
        token = auth.login("owner@x.com", "SenhaForte123")
        principal = issue_owner_principal(auth_service=auth, session_token=token)
        ar = AuthorityRegistry()
        root = ar.bootstrap_root_authority(owner_principal=principal, auth_service=auth, bootstrap_reason="x")
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="owner@x.com", authority_level="OWNER",
            scopes=[SCOPE_REGISTER_SOURCE], issuance_reason="x",
        )
        desk = EscalationDesk(__import__("core.security.audit", fromlist=["AuditTrail"]).AuditTrail())
        sr = SourceRegistry(authority_registry=ar, escalation_desk=desk)
        return ar, grant, desk, sr

    def test_trusted_official_without_approval_denied(self):
        from core.evidence.source_registry import SourceRegistrationDenied

        _ar, grant, _desk, sr = self._setup()
        with self.assertRaises(SourceRegistrationDenied):
            sr.register(
                SourceRegistration(
                    source_id="s1", source_type=SourceType.OFFICIAL, source_reference="x",
                    requested_trust_class=TrustClass.TRUSTED_OFFICIAL, health=CapabilityHealth.HEALTHY,
                    requested_verification_status=VerificationStatus.VERIFIED,
                ),
                authority_grant_id=grant.grant_id, registration_reason="x",
            )

    def test_13_only_approved_status_authorizes_never_rejected_expired_cancelled(self):
        """Item 13: approval REJECTED/EXPIRED/CANCELLED nunca autoriza."""
        from core.evidence.source_registry import SourceRegistrationDenied
        from core.security.rbac import Principal, Role

        _ar, grant, desk, sr = self._setup()
        owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})

        item = desk.escalate(reason="x", context={}, actor="atlas@system", operation="REGISTER_SOURCE_TRUSTED_OFFICIAL", risk="HIGH")
        desk.reject(item.escalation_id, decision="REJECTED", resolved_by=owner)
        with self.assertRaises(SourceRegistrationDenied):
            sr.register(
                SourceRegistration(
                    source_id="s2", source_type=SourceType.OFFICIAL, source_reference="x",
                    requested_trust_class=TrustClass.TRUSTED_OFFICIAL, health=CapabilityHealth.HEALTHY,
                    requested_verification_status=VerificationStatus.VERIFIED,
                ),
                authority_grant_id=grant.grant_id, registration_reason="x", approval_id=item.escalation_id,
            )

    def test_approved_approval_authorizes_trusted_official(self):
        from core.security.rbac import Principal, Role

        _ar, grant, desk, sr = self._setup()
        owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
        item = desk.escalate(reason="x", context={}, actor="atlas@system", operation="REGISTER_SOURCE_TRUSTED_OFFICIAL", risk="HIGH")
        desk.approve(item.escalation_id, decision="APPROVED", resolved_by=owner)
        result = sr.register(
            SourceRegistration(
                source_id="s3", source_type=SourceType.OFFICIAL, source_reference="x",
                requested_trust_class=TrustClass.TRUSTED_OFFICIAL, health=CapabilityHealth.HEALTHY,
                requested_verification_status=VerificationStatus.VERIFIED,
            ),
            authority_grant_id=grant.grant_id, registration_reason="x", approval_id=item.escalation_id,
        )
        self.assertTrue(sr.can_sustain_fact("s3"))
