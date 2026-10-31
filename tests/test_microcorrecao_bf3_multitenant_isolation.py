import os
import sqlite3
import tempfile
import unittest

from core.evidence.authority_registry import AuthorityRegistry
from core.evidence.claim_registry import ClaimRecord, ClaimRegistry
from core.evidence.governance_context import ApplicationGovernanceContext, GovernanceContextError
from core.models import db as db_module
from core.security.auth import create_canonical_auth_service, hash_password

"""
MICROCORREÇÃO BF3 — ISOLAMENTO MULTI-TENANT REAL.

Falha real reproduzida por auditoria independente: usando o MESMO
`db_path`, appA/tenantA registra um claim; appB/tenantB consegue
executar `restore_for_app()` e resolver o claim de A.

Causa raiz: `restore_for_app()` aceitava o `application_id`/
`tenant_id` do CHAMADOR sem validar contra o que foi genuinamente
persistido em `authority_root_bootstrap`; e `ClaimRegistry` carregava
TODOS os claims de `evidence_claim_registry` sem filtro algum de
escopo.

Corrigido em duas camadas (fail-closed):
1. `restore_for_app()` agora SEMPRE valida `application_id`/
   `tenant_id` do chamador contra `authority_root_bootstrap`
   persistido — `REGISTRY_CONTEXT_MISMATCH` em qualquer divergência.
2. `evidence_claim_registry` agora é escopado por `application_id`/
   `tenant_id`/`governance_context_id` — `ClaimRegistry` recebe esse
   escopo NO CONSTRUTOR, antes de carregar qualquer coisa, e todo
   `SELECT`/`INSERT` é filtrado por ele (defesa em profundidade,
   mesmo se algo construir um `ClaimRegistry` diretamente, sem passar
   por `governance_context.py`).
"""


class RestoreForAppMultiTenantValidationTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _bootstrap_as(self, application_id: str, tenant_id: str):
        auth_service = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
            application_id=application_id, tenant_id=tenant_id,
        )
        token = auth_service.login(email="owner@x.com", password="SenhaForte123")
        ctx = ApplicationGovernanceContext.bootstrap_for_app(
            auth_service=auth_service, session_token=token, bootstrap_reason="teste",
            application_id=application_id, tenant_id=tenant_id, db_path=self.db_path,
        )
        return auth_service, token, ctx

    def test_restore_with_different_application_id_rejected(self):
        self._bootstrap_as("appA", "tenantA")
        auth_b = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
            application_id="appB", tenant_id="tenantA",
        )
        token_b = auth_b.login(email="owner@x.com", password="SenhaForte123")
        with self.assertRaises(GovernanceContextError) as ctx:
            ApplicationGovernanceContext.restore_for_app(
                auth_service=auth_b, session_token=token_b, application_id="appB", tenant_id="tenantA",
                db_path=self.db_path,
            )
        self.assertEqual(ctx.exception.error_code, "REGISTRY_CONTEXT_MISMATCH")

    def test_restore_with_different_tenant_id_rejected(self):
        self._bootstrap_as("appA", "tenantA")
        auth_b = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
            application_id="appA", tenant_id="tenantB",
        )
        token_b = auth_b.login(email="owner@x.com", password="SenhaForte123")
        with self.assertRaises(GovernanceContextError) as ctx:
            ApplicationGovernanceContext.restore_for_app(
                auth_service=auth_b, session_token=token_b, application_id="appA", tenant_id="tenantB",
                db_path=self.db_path,
            )
        self.assertEqual(ctx.exception.error_code, "REGISTRY_CONTEXT_MISMATCH")

    def test_restore_with_matching_scope_still_succeeds(self):
        """Confirma que a correção não quebrou o caso legítimo — o
        MESMO application_id/tenant_id que bootstrapou consegue
        restaurar normalmente."""
        self._bootstrap_as("appA", "tenantA")
        auth_a2 = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
            application_id="appA", tenant_id="tenantA",
        )
        token_a2 = auth_a2.login(email="owner@x.com", password="SenhaForte123")
        ctx2 = ApplicationGovernanceContext.restore_for_app(
            auth_service=auth_a2, session_token=token_a2, application_id="appA", tenant_id="tenantA",
            db_path=self.db_path,
        )
        self.assertIsNotNone(ctx2)

    def test_restore_without_any_bootstrap_still_reports_not_bootstrapped(self):
        """A checagem NOT_BOOTSTRAPPED (já existente) continua vindo
        ANTES da checagem de mismatch — nunca substituída por ela."""
        with self.assertRaises(GovernanceContextError) as ctx:
            auth_service = create_canonical_auth_service(
                owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"),
                db_path=self.db_path, application_id="appA", tenant_id="tenantA",
            )
            token = auth_service.login(email="owner@x.com", password="SenhaForte123")
            ApplicationGovernanceContext.restore_for_app(
                auth_service=auth_service, session_token=token, application_id="appA", tenant_id="tenantA",
                db_path=self.db_path,
            )
        self.assertEqual(ctx.exception.error_code, "NOT_BOOTSTRAPPED")


class ClaimRegistryScopedIsolationTests(unittest.TestCase):
    """Cenário negativo EXATO reportado: usando o mesmo db_path,
    appA/tenantA registra um claim; appB/tenantB NUNCA restaura nem lê
    esse claim — testado via os dois caminhos (restore_for_app() E
    construção direta de ClaimRegistry, defesa em profundidade)."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_b_never_restores_nor_reads_claims_of_a(self):
        auth_a = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
            application_id="appA", tenant_id="tenantA",
        )
        token_a = auth_a.login(email="owner@x.com", password="SenhaForte123")
        ctx_a = ApplicationGovernanceContext.bootstrap_for_app(
            auth_service=auth_a, session_token=token_a, bootstrap_reason="teste", application_id="appA",
            tenant_id="tenantA", db_path=self.db_path,
        )
        claim_a = ClaimRecord(
            claim_id=ClaimRecord.new_id(), strategy_id="strat_a", claim_type="X", statement="segredo real de A",
        )
        ctx_a.claim_registry.register(claim_a)
        self.assertIsNotNone(ctx_a.claim_registry.resolve(claim_a.claim_id))

        # CAMINHO 1 -- appB tenta restore_for_app() sobre o MESMO db_path
        auth_b = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
            application_id="appB", tenant_id="tenantB",
        )
        token_b = auth_b.login(email="owner@x.com", password="SenhaForte123")
        with self.assertRaises(GovernanceContextError):
            ApplicationGovernanceContext.restore_for_app(
                auth_service=auth_b, session_token=token_b, application_id="appB", tenant_id="tenantB",
                db_path=self.db_path,
            )

        # CAMINHO 2 (defesa em profundidade) -- mesmo um ClaimRegistry
        # construído DIRETAMENTE, fora de governance_context.py, com o
        # escopo de B, nunca resolve o claim de A.
        registry_b = ClaimRegistry(
            db_path=self.db_path, application_id="appB", tenant_id="tenantB", governance_context_id="qualquer",
        )
        self.assertIsNone(registry_b.resolve(claim_a.claim_id))
        self.assertEqual(len(registry_b.all()), 0)
        self.assertFalse(registry_b.matches(claim_a.claim_id, strategy_id="strat_a"))

        # controle positivo -- appA com o MESMO escopo ainda ve seu proprio claim
        registry_a_again = ClaimRegistry(
            db_path=self.db_path, application_id="appA", tenant_id="tenantA",
            governance_context_id=ctx_a.context_id,
        )
        self.assertIsNotNone(registry_a_again.resolve(claim_a.claim_id))

    def test_claims_isolated_even_with_same_governance_context_id_but_different_tenant(self):
        """Defesa em profundidade adicional: mesmo que alguém (por
        engano ou má-fé) reutilize o MESMO `governance_context_id` mas
        com `application_id`/`tenant_id` diferente, o claim ainda não
        é visível — os TRÊS campos precisam bater."""
        registry_a = ClaimRegistry(
            db_path=self.db_path, application_id="appA", tenant_id="tenantA", governance_context_id="ctx_compartilhado",
        )
        claim = ClaimRecord(claim_id=ClaimRecord.new_id(), strategy_id="s", claim_type="X", statement="x")
        registry_a.register(claim)

        registry_b_mesmo_context_id = ClaimRegistry(
            db_path=self.db_path, application_id="appB", tenant_id="tenantB",
            governance_context_id="ctx_compartilhado",
        )
        self.assertIsNone(registry_b_mesmo_context_id.resolve(claim.claim_id))

    def test_new_claim_registered_by_a_after_b_exists_still_isolated(self):
        """Ordem inversa: B já existe (escopo diferente) quando A
        registra um NOVO claim depois — ainda assim B nunca vê."""
        registry_b = ClaimRegistry(db_path=self.db_path, application_id="appB", tenant_id="tenantB", governance_context_id="ctxB")
        registry_a = ClaimRegistry(db_path=self.db_path, application_id="appA", tenant_id="tenantA", governance_context_id="ctxA")
        claim = ClaimRecord(claim_id=ClaimRecord.new_id(), strategy_id="s", claim_type="X", statement="novo claim de A")
        registry_a.register(claim)
        self.assertIsNone(registry_b.resolve(claim.claim_id))
        # nem mesmo reconstruindo B do zero (nova carga do banco)
        registry_b_fresh = ClaimRegistry(db_path=self.db_path, application_id="appB", tenant_id="tenantB", governance_context_id="ctxB")
        self.assertIsNone(registry_b_fresh.resolve(claim.claim_id))


class ClaimRegistryMigrationBackfillTests(unittest.TestCase):
    """Migração aditiva + backfill idempotente para bancos criados
    ANTES desta correção (tabela sem as colunas de escopo)."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _create_legacy_schema_with_claim_and_bootstrap(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE evidence_claim_registry (claim_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, "
            "claim_type TEXT NOT NULL, statement TEXT NOT NULL, related_metric_names TEXT NOT NULL, "
            "status TEXT NOT NULL, evidence_requirements TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO evidence_claim_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("claim_legado", "strat_legado", "X", "claim pre-migracao", "[]", "OPEN", "[]", 1000.0),
        )
        conn.execute(
            "CREATE TABLE authority_root_bootstrap (singleton_guard TEXT PRIMARY KEY DEFAULT 'ROOT', "
            "owner_id TEXT NOT NULL, root_grant_id TEXT NOT NULL, auth_service_id TEXT NOT NULL, "
            "application_id TEXT NOT NULL DEFAULT 'default', tenant_id TEXT NOT NULL DEFAULT 'default', "
            "bootstrapped_at REAL NOT NULL, bootstrap_reason TEXT NOT NULL, "
            "canonical_registry_id TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO authority_root_bootstrap VALUES ('ROOT', ?, ?, ?, ?, ?, ?, ?, ?)",
            ("owner1", "grant1", "auth1", "appLegado", "tenantLegado", 999.0, "bootstrap legado", "authreg_legado"),
        )
        conn.commit()
        conn.close()

    def test_migration_backfills_existing_claim_with_real_bootstrap_scope(self):
        self._create_legacy_schema_with_claim_and_bootstrap()
        db_module.init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM evidence_claim_registry WHERE claim_id='claim_legado'").fetchone()
        conn.close()

        self.assertEqual(row["application_id"], "appLegado")
        self.assertEqual(row["tenant_id"], "tenantLegado")
        self.assertEqual(row["governance_context_id"], "authreg_legado")

    def test_migration_idempotent_running_twice_never_corrupts(self):
        self._create_legacy_schema_with_claim_and_bootstrap()
        db_module.init_db(self.db_path)
        db_module.init_db(self.db_path)
        db_module.init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM evidence_claim_registry WHERE claim_id='claim_legado'").fetchone()
        count = conn.execute("SELECT COUNT(*) AS c FROM evidence_claim_registry").fetchone()
        conn.close()

        self.assertEqual(row["application_id"], "appLegado")
        self.assertEqual(count["c"], 1)  # nunca duplicou a linha

    def test_backfilled_claim_resolves_correctly_through_scoped_registry(self):
        self._create_legacy_schema_with_claim_and_bootstrap()
        db_module.init_db(self.db_path)

        registry = ClaimRegistry(
            db_path=self.db_path, application_id="appLegado", tenant_id="tenantLegado",
            governance_context_id="authreg_legado",
        )
        restored = registry.resolve("claim_legado")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.statement, "claim pre-migracao")

    def test_fresh_database_has_scope_columns_from_start(self):
        db_module.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(evidence_claim_registry)").fetchall()]
        conn.close()
        for col in ("application_id", "tenant_id", "governance_context_id"):
            self.assertIn(col, columns)


class AuthorityRegistryPersistedScopeTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_persisted_bootstrap_scope_none_before_bootstrap(self):
        registry = AuthorityRegistry(db_path=self.db_path)
        self.assertIsNone(registry.persisted_bootstrap_scope())

    def test_persisted_bootstrap_scope_matches_after_bootstrap(self):
        auth_service = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
            application_id="appX", tenant_id="tenantY",
        )
        token = auth_service.login(email="owner@x.com", password="SenhaForte123")
        ApplicationGovernanceContext.bootstrap_for_app(
            auth_service=auth_service, session_token=token, bootstrap_reason="teste", application_id="appX",
            tenant_id="tenantY", db_path=self.db_path,
        )
        fresh_registry = AuthorityRegistry(db_path=self.db_path)
        self.assertEqual(fresh_registry.persisted_bootstrap_scope(), ("appX", "tenantY"))


if __name__ == "__main__":
    unittest.main()
