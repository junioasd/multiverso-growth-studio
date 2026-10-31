import os
import tempfile
import unittest

from core.capabilities.registry import CapabilityHealth
from core.evidence.authority_registry import (
    AuthorityDenied, AuthorityRegistry, SCOPE_REGISTER_SOURCE, SCOPE_REGISTER_VERIFIER,
)
from core.evidence.governance_context import ApplicationGovernanceContext, GovernanceContextError
from core.evidence.owner_principal import issue_owner_principal
from core.evidence.registry import VerificationStatus
from core.evidence.source_registry import SourceRegistration, SourceRegistry, SourceType, TrustClass
from core.evidence.verifier_registry import TrustLevel, VerifierRegistration, VerifierRegistry
from core.models import db as db_module
from core.security.auth import create_canonical_auth_service, hash_password

"""
Testes CORRECAO FINAL ROUND 9 - continuidade real da governanca/Owner
apos restart: grants, revogacoes e registros de verifier/source
persistem de verdade (nao so o FATO do bootstrap); registry_id
estavel entre restarts; API limpa de restauracao; sem vazamento de
recursos.
"""


class GovernanceContinuityAfterRestartTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def _bootstrap(self):
        auth = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
        )
        token = auth.login("owner@x.com", "SenhaForte123")
        principal = issue_owner_principal(auth_service=auth, session_token=token)
        ar = AuthorityRegistry(db_path=self.db_path)
        root = ar.bootstrap_root_authority(owner_principal=principal, auth_service=auth, bootstrap_reason="setup")
        return auth, ar, root

    def test_grant_survives_restart(self):
        _auth, ar1, root = self._bootstrap()
        grant = ar1.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="owner@x.com", authority_level="OWNER",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",
        )
        self.assertTrue(ar1.validate_grant(grant.grant_id, required_scope=SCOPE_REGISTER_VERIFIER))

        # SIMULA RESTART: nova instância, mesmo db_path
        ar2 = AuthorityRegistry(db_path=self.db_path)
        self.assertTrue(ar2.validate_grant(grant.grant_id, required_scope=SCOPE_REGISTER_VERIFIER))

    def test_registry_id_stable_across_restart(self):
        _auth, ar1, _root = self._bootstrap()
        ar2 = AuthorityRegistry(db_path=self.db_path)
        self.assertEqual(ar1.registry_id, ar2.registry_id)

    def test_revocation_survives_restart(self):
        """A decisão crítica de REVOGAR não pode 'voltar atrás'
        silenciosamente por causa de um restart."""
        _auth, ar1, root = self._bootstrap()
        grant = ar1.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="owner@x.com", authority_level="OWNER",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",
        )
        ar1.revoke_grant(grant.grant_id, revoked_by="owner@x.com", reason="comprometido")
        self.assertFalse(ar1.validate_grant(grant.grant_id))

        ar2 = AuthorityRegistry(db_path=self.db_path)
        self.assertFalse(ar2.validate_grant(grant.grant_id))
        restored = ar2.resolve_grant(grant.grant_id)
        self.assertEqual(restored.status.value, "REVOKED")
        self.assertEqual(restored.revocation_reason, "comprometido")

    def test_verifier_registration_survives_restart(self):
        _auth, ar1, root = self._bootstrap()
        grant = ar1.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="owner@x.com", authority_level="OWNER",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",
        )
        vr1 = VerifierRegistry(authority_registry=ar1, db_path=self.db_path)
        vr1.register(
            VerifierRegistration(verifier_id="v1", verifier_type="x", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD),
            authority_grant_id=grant.grant_id, registration_reason="x",
        )
        self.assertTrue(vr1.is_authorized("v1"))

        ar2 = AuthorityRegistry(db_path=self.db_path)
        vr2 = VerifierRegistry(authority_registry=ar2, db_path=self.db_path)
        self.assertTrue(vr2.is_authorized("v1"))

    def test_source_registration_survives_restart(self):
        _auth, ar1, root = self._bootstrap()
        grant = ar1.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="owner@x.com", authority_level="OWNER",
            scopes=[SCOPE_REGISTER_SOURCE], issuance_reason="x",
        )
        sr1 = SourceRegistry(authority_registry=ar1, db_path=self.db_path)
        sr1.register(
            SourceRegistration(
                source_id="s1", source_type=SourceType.OWN_OPERATION_DATA, source_reference="x",
                requested_trust_class=TrustClass.TRUSTED_FIRST_PARTY, health=CapabilityHealth.HEALTHY,
                requested_verification_status=VerificationStatus.VERIFIED,
            ),
            authority_grant_id=grant.grant_id, registration_reason="x",
        )
        self.assertTrue(sr1.can_sustain_fact("s1"))

        ar2 = AuthorityRegistry(db_path=self.db_path)
        sr2 = SourceRegistry(authority_registry=ar2, db_path=self.db_path)
        self.assertTrue(sr2.can_sustain_fact("s1"))

    def test_source_revoked_grant_after_restart_stops_sustaining_fact(self):
        """Revogação + restart combinados — a fonte precisa parar de
        sustentar FACT mesmo depois de reiniciar."""
        _auth, ar1, root = self._bootstrap()
        grant = ar1.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="owner@x.com", authority_level="OWNER",
            scopes=[SCOPE_REGISTER_SOURCE], issuance_reason="x",
        )
        sr1 = SourceRegistry(authority_registry=ar1, db_path=self.db_path)
        sr1.register(
            SourceRegistration(
                source_id="s1", source_type=SourceType.OWN_OPERATION_DATA, source_reference="x",
                requested_trust_class=TrustClass.TRUSTED_FIRST_PARTY, health=CapabilityHealth.HEALTHY,
                requested_verification_status=VerificationStatus.VERIFIED,
            ),
            authority_grant_id=grant.grant_id, registration_reason="x",
        )
        ar1.revoke_grant(grant.grant_id, revoked_by="owner@x.com", reason="comprometido")

        ar2 = AuthorityRegistry(db_path=self.db_path)
        sr2 = SourceRegistry(authority_registry=ar2, db_path=self.db_path)
        self.assertFalse(sr2.can_sustain_fact("s1"))

    def test_owner_b_denied_after_restart_exact_scenario(self):
        """Cenário exato: Owner A bootstrap -> restart -> Owner B negado."""
        _auth_a, ar1, _root = self._bootstrap()
        ar2 = AuthorityRegistry(db_path=self.db_path)
        auth_b = create_canonical_auth_service(
            owner_email="ownerB@x.com", stored_password_hash=hash_password("SenhaB1234"), db_path=self.db_path,
        )
        token_b = auth_b.login("ownerB@x.com", "SenhaB1234")
        principal_b = issue_owner_principal(auth_service=auth_b, session_token=token_b)
        with self.assertRaises(AuthorityDenied):
            ar2.bootstrap_root_authority(owner_principal=principal_b, auth_service=auth_b, bootstrap_reason="x")


class CleanRestoreApiTests(unittest.TestCase):
    """API dedicada: ApplicationGovernanceContext.restore_for_app() /
    GrowthStudioApp.restore_governance_context()."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def test_restore_without_prior_bootstrap_denied(self):
        auth = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
        )
        token = auth.login("owner@x.com", "SenhaForte123")
        with self.assertRaises(GovernanceContextError) as ctx:
            ApplicationGovernanceContext.restore_for_app(auth_service=auth, session_token=token, db_path=self.db_path)
        self.assertEqual(ctx.exception.error_code, "NOT_BOOTSTRAPPED")

    def test_full_app_restore_cycle(self):
        from api.bootstrap import build_app

        app1 = build_app(
            owner_email="owner_real@empresa.com", owner_password_hash=hash_password("SenhaOwnerReal123"),
            db_path=self.db_path,
        )
        ctx1 = app1.bootstrap_governance_context(
            email="owner_real@empresa.com", password="SenhaOwnerReal123", bootstrap_reason="setup real",
        )
        ctx1.verifier_registry.register(
            VerifierRegistration(verifier_id="v1", verifier_type="human_reviewer", authorized=True, health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.HIGH),
            authority_grant_id=ctx1._bootstrap_verifier_grant_id, registration_reason="aprovado",
        )
        app1.shutdown()

        app2 = build_app(
            owner_email="owner_real@empresa.com", owner_password_hash=hash_password("SenhaOwnerReal123"),
            db_path=self.db_path,
        )
        ctx2 = app2.restore_governance_context(email="owner_real@empresa.com", password="SenhaOwnerReal123")
        self.assertTrue(ctx2.is_canonical())
        self.assertEqual(ctx2.context_id, ctx1.context_id)
        self.assertTrue(ctx2.verifier_registry.is_authorized("v1"))
        app2.shutdown()

    def test_restore_denied_with_wrong_credentials(self):
        from api.bootstrap import build_app
        from core.security.auth import AuthError

        app1 = build_app(
            owner_email="owner_real@empresa.com", owner_password_hash=hash_password("SenhaOwnerReal123"),
            db_path=self.db_path,
        )
        app1.bootstrap_governance_context(
            email="owner_real@empresa.com", password="SenhaOwnerReal123", bootstrap_reason="setup",
        )
        app1.shutdown()

        app2 = build_app(
            owner_email="owner_real@empresa.com", owner_password_hash=hash_password("SenhaOwnerReal123"),
            db_path=self.db_path,
        )
        with self.assertRaises(AuthError):
            app2.restore_governance_context(email="owner_real@empresa.com", password="SenhaErrada999")
        app2.shutdown()


class NoResourceLeaksFromPersistenceTests(unittest.TestCase):
    """Sem vazamento de conexões SQLite nas novas rotinas de persistência."""

    def test_repeated_bootstrap_restore_cycles_no_leak(self):
        import gc
        import warnings

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(db_path)
        db_module.init_db(db_path)
        try:
            captured = []
            warnings.simplefilter("always", ResourceWarning)
            original = warnings.showwarning

            def capture(message, category, filename, lineno, file=None, line=None):
                if category is ResourceWarning:
                    captured.append(str(message))

            warnings.showwarning = capture
            try:
                auth = create_canonical_auth_service(
                    owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=db_path,
                )
                token = auth.login("owner@x.com", "SenhaForte123")
                principal = issue_owner_principal(auth_service=auth, session_token=token)
                ar = AuthorityRegistry(db_path=db_path)
                root = ar.bootstrap_root_authority(owner_principal=principal, auth_service=auth, bootstrap_reason="x")
                for _ in range(5):
                    grant = ar.issue_grant(
                        issuer_grant_id=root.grant_id, subject_actor_id="owner@x.com", authority_level="OWNER",
                        scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",
                    )
                    ar.revoke_grant(grant.grant_id, revoked_by="owner@x.com", reason="teste")
                for _ in range(3):
                    AuthorityRegistry(db_path=db_path)  # simula múltiplos restarts
                gc.collect()
                gc.collect()
            finally:
                warnings.showwarning = original
            self.assertEqual(len(captured), 0, f"ResourceWarnings inesperados: {captured}")
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)


if __name__ == "__main__":
    unittest.main()
