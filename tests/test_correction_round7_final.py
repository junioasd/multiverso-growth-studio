import time
import unittest

from core.capabilities.registry import CapabilityHealth
from core.evidence.authority_registry import (
    ALL_SCOPES, AuthorityDenied, AuthorityGrant, AuthorityRegistry, GrantStatus,
    SCOPE_ISSUE_AUTHORITY_GRANT, SCOPE_REGISTER_SOURCE, SCOPE_REGISTER_VERIFIER,
)
from core.evidence.claims import ClaimType
from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
from core.evidence.source_registry import (
    SourceRegistration, SourceRegistrationDenied, SourceRegistry, SourceType, TrustClass,
)
from core.evidence.verifier_registry import RegistrationDenied, TrustLevel, VerifierRegistration, VerifierRegistry
from core.multimodal.evidence import Modality
from radar_web.adoption import AdoptionPipeline, AdoptionStatus, QualityGateRejection
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy
from radar_web.quality_gate import RadarWebEvidenceGate
from core.evidence.owner_principal import issue_owner_principal
from tests._evidence_test_helpers import make_authenticated_test_owner_session

"""
Testes CORRECAO V6.1.1 ROUND 7 (FINAL) - AuthorityGrant/AuthorityRegistry
reais, privilege escalation protection, bootstrap unico, revogacao/
expiracao de grant, testes adversariais 11-22.
"""


class AuthorityGrantIssuanceTests(unittest.TestCase):
    """Itens 2/3 — AuthorityGrant/AuthorityRegistry reais."""

    def test_bootstrap_creates_owner_grant_with_all_scopes(self):
        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        grant = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="setup inicial")
        self.assertEqual(grant.authority_level, "OWNER")
        self.assertEqual(set(grant.scopes), set(ALL_SCOPES))
        self.assertEqual(grant.status, GrantStatus.ACTIVE)

    def test_17_bootstrap_only_once(self):
        """Item 17 exato: bootstrap não pode ser repetido arbitrariamente."""
        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="x")
        with self.assertRaises(AuthorityDenied):
            _auth_service, _session_token = make_authenticated_test_owner_session()
            _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
            ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="segunda tentativa")

    def test_issue_grant_from_valid_issuer_succeeds(self):
        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        root = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="x")
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="governanca", authority_level="GOVERNANCE",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="delegação",
        )
        self.assertTrue(ar.validate_grant(grant.grant_id, required_scope=SCOPE_REGISTER_VERIFIER))

    def test_11_self_grant_attack_denied(self):
        """Item 11 exato: attacker tenta issue_grant(subject_actor_id=
        'attacker', authority_level='OWNER') sem autoridade -> DENIED."""
        ar = AuthorityRegistry()
        with self.assertRaises(AuthorityDenied):
            ar.issue_grant(
                issuer_grant_id="grant_inexistente", subject_actor_id="attacker", authority_level="OWNER",
                scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="me autorizando",
            )

    def test_10_privilege_escalation_blocked(self):
        """Item 10 exato: SENIOR não pode criar OWNER."""
        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        root = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="x")
        senior = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="senior", authority_level="SENIOR",
            scopes=[SCOPE_ISSUE_AUTHORITY_GRANT], issuance_reason="x",
        )
        with self.assertRaises(AuthorityDenied):
            ar.issue_grant(
                issuer_grant_id=senior.grant_id, subject_actor_id="escalado", authority_level="OWNER",
                scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="escalando",
            )

    def test_10_scope_escalation_blocked(self):
        """Um emissor não pode conceder um escopo que ele mesmo não possui."""
        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        root = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="x")
        limited = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="limitado", authority_level="SENIOR",
            scopes=[SCOPE_ISSUE_AUTHORITY_GRANT, SCOPE_REGISTER_SOURCE], issuance_reason="x",
        )
        with self.assertRaises(AuthorityDenied):
            ar.issue_grant(
                issuer_grant_id=limited.grant_id, subject_actor_id="outro", authority_level="SENIOR",
                scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",  # escopo que 'limitado' nao tem
            )

    def test_13_fabricated_grant_object_never_resolves(self):
        """Item 13 exato: AuthorityGrant criado diretamente pelo
        chamador (nunca registrado) -> INVALID/nunca resolve."""
        ar = AuthorityRegistry()
        fake_grant = AuthorityGrant(
            grant_id="fake", subject_actor_id="attacker", authority_level="OWNER",
            issued_by="attacker", scopes=[SCOPE_REGISTER_VERIFIER],
        )
        self.assertIsNone(ar.resolve_grant(fake_grant.grant_id))
        self.assertFalse(ar.validate_grant(fake_grant.grant_id))

    def test_14_revoked_grant_denied(self):
        """Item 14 exato."""
        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        root = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="x")
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="v1", authority_level="GOVERNANCE",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",
        )
        self.assertTrue(ar.validate_grant(grant.grant_id))
        ar.revoke_grant(grant.grant_id, revoked_by="owner", reason="comprometido")
        self.assertFalse(ar.validate_grant(grant.grant_id))

    def test_15_expired_grant_denied(self):
        """Item 15 exato."""
        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        root = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="x")
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="v1", authority_level="GOVERNANCE",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x", expires_at=time.time() - 1,
        )
        self.assertFalse(ar.validate_grant(grant.grant_id))

    def test_16_wrong_scope_denied(self):
        """Item 16 exato: grant com REGISTER_SOURCE tenta registrar verifier."""
        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        root = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="x")
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="v1", authority_level="GOVERNANCE",
            scopes=[SCOPE_REGISTER_SOURCE], issuance_reason="x",
        )
        self.assertFalse(ar.validate_grant(grant.grant_id, required_scope=SCOPE_REGISTER_VERIFIER))

    def test_18_audit_records_every_authority_action(self):
        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        root = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="x")
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="v1", authority_level="GOVERNANCE",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",
        )
        ar.revoke_grant(grant.grant_id, revoked_by="owner", reason="x")
        actions = [e["action"] for e in ar.audit_log]
        self.assertIn("BOOTSTRAP_ROOT", actions)
        self.assertIn("ISSUE_GRANT", actions)
        self.assertIn("REVOKE_GRANT", actions)
        for entry in ar.audit_log:
            for field in ("audit_id", "actor", "grant_id", "outcome", "timestamp"):
                self.assertIn(field, entry)


class VerifierSourceRegistryAuthorityIntegrationTests(unittest.TestCase):
    """Itens 6/7/8 — VerifierRegistry/SourceRegistry integrados ao AuthorityRegistry."""

    def test_12_fake_owner_string_denied_verifier(self):
        """Item 12 exato: VerifierRegistry.register(actor_id='attacker',
        authority_level='OWNER') sem AuthorityGrant -> DENIED."""
        vr = VerifierRegistry()
        with self.assertRaises(RegistrationDenied):
            vr.register(
                VerifierRegistration(verifier_id="v1", verifier_type="x", authorized=True),
                authority_grant_id="", registration_reason="sou o owner",
            )

    def test_12_fake_owner_string_denied_source(self):
        """Item 12 exato — mesmo comportamento para SourceRegistry."""
        sr = SourceRegistry()
        with self.assertRaises(SourceRegistrationDenied):
            sr.register(
                SourceRegistration(
                    source_id="s1", source_type=SourceType.OFFICIAL, source_reference="x",
                    requested_trust_class=TrustClass.TRUSTED_OFFICIAL, health=CapabilityHealth.HEALTHY,
                    requested_verification_status=VerificationStatus.VERIFIED,
                ),
                authority_grant_id="", registration_reason="sou o owner",
            )

    def test_19_revalidation_grant_revoked_after_registration_derrubada_autorizacao(self):
        """Item 19 — revalidação de root-of-trust: grant revogado
        DEPOIS do registro do verifier derruba a autorização
        imediatamente."""
        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        root = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="x")
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="v1", authority_level="GOVERNANCE",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",
        )
        vr = VerifierRegistry(authority_registry=ar)
        vr.register(
            VerifierRegistration(verifier_id="v1", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD),
            authority_grant_id=grant.grant_id, registration_reason="x",
        )
        self.assertTrue(vr.is_authorized("v1"))
        ar.revoke_grant(grant.grant_id, revoked_by="owner", reason="comprometido")
        self.assertFalse(vr.is_authorized("v1"))  # cai imediatamente, sem novo registro


class CompleteAuthorityAttackAndValidChainTests(unittest.TestCase):
    """Testes 21/22 — ataque completo bloqueado na raiz; cenário válido chega a ADOPTED."""

    def test_21_complete_attack_blocked_at_authority_stage(self):
        vr = VerifierRegistry()
        with self.assertRaises(RegistrationDenied):
            vr.register(
                VerifierRegistration(verifier_id="self", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY),
                authority_grant_id="grant_inventado", registration_reason="sou o owner confie em mim",
            )
        sr = SourceRegistry()
        with self.assertRaises(SourceRegistrationDenied):
            sr.register(
                SourceRegistration(
                    source_id="fake_official", source_type=SourceType.OFFICIAL, source_reference="fake://x",
                    requested_trust_class=TrustClass.TRUSTED_OFFICIAL, health=CapabilityHealth.HEALTHY,
                    requested_verification_status=VerificationStatus.VERIFIED,
                ),
                authority_grant_id="grant_inventado", registration_reason="sou o owner",
            )
        registry = EvidenceRegistry(verifier_registry=vr, source_registry=sr)
        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=registry))
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fake_official", evidence_ids=["ev-fake"], observed_at=time.time(),
        )
        with self.assertRaises(QualityGateRejection):
            pipeline.run_quality_gate(record.record_id, strategy=strategy)
        final = pipeline.get(record.record_id)
        self.assertNotEqual(final.status, AdoptionStatus.ADOPTED)

    def test_22_fully_governed_chain_reaches_adopted(self):
        from tests._evidence_test_helpers import register_test_claim
        from core.evidence.metric_evidence import MetricEvidence
        from core.evidence.provenance import ProvenanceRecord

        ar = AuthorityRegistry()
        _auth_service, _session_token = make_authenticated_test_owner_session()
        _principal = issue_owner_principal(auth_service=_auth_service, session_token=_session_token)
        root = ar.bootstrap_root_authority(owner_principal=_principal, auth_service=_auth_service, bootstrap_reason="setup real")
        v_grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="governanca", authority_level="GOVERNANCE",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="delegação de verificação",
        )
        s_grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="governanca", authority_level="GOVERNANCE",
            scopes=[SCOPE_REGISTER_SOURCE], issuance_reason="delegação de fontes",
        )
        vr = VerifierRegistry(authority_registry=ar)
        vr.register(
            VerifierRegistration(verifier_id="v1", verifier_type="human_reviewer", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD),
            authority_grant_id=v_grant.grant_id, registration_reason="verificador aprovado",
        )
        sr = SourceRegistry(authority_registry=ar)
        sr.register(
            SourceRegistration(
                source_id="fonte_governada", source_type=SourceType.OWN_OPERATION_DATA, source_reference="x",
                requested_trust_class=TrustClass.TRUSTED_FIRST_PARTY, health=CapabilityHealth.HEALTHY,
                requested_verification_status=VerificationStatus.VERIFIED,
            ),
            authority_grant_id=s_grant.grant_id, registration_reason="fonte aprovada",
        )
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


class DiagnosticsUtilityTests(unittest.TestCase):
    """Itens 24/25 — dump de recursos e shutdown_all()."""

    def test_dump_resources_reports_clean_state(self):
        from core.diagnostics import dump_resources

        dump = dump_resources()
        self.assertIn("total_threads", dump)
        self.assertIn("threads", dump)
        self.assertIn("non_daemon_non_main_alive", dump)
        self.assertIn("clean", dump)

    def test_shutdown_all_is_idempotent_and_handles_none(self):
        from core.diagnostics import shutdown_all
        from api.bootstrap import build_app
        from core.security.auth import hash_password

        app = build_app(
            owner_email="owner@x.com", owner_password_hash=hash_password("StrongPass123"),
            persistent=False, start_radar_worker=True,
        )
        result = shutdown_all(app, None, app)  # chamado duas vezes no mesmo app -- nunca deve falhar
        self.assertEqual(len(result["shutdown_results"]), 2)  # None e ignorado, nao aparece no resultado
        for entry in result["shutdown_results"]:
            self.assertEqual(entry["outcome"], "SHUTDOWN")
        self.assertFalse(app.atlas.radar_worker.is_running())

    def test_shutdown_all_never_raises_on_error(self):
        from core.diagnostics import shutdown_all

        class Broken:
            def shutdown(self):
                raise RuntimeError("falha simulada")

        result = shutdown_all(Broken())
        self.assertEqual(result["shutdown_results"][0]["outcome"], "ERROR")
