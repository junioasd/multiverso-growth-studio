import os
import sqlite3
import tempfile
import unittest

from core.evidence.claim_registry import ClaimRecord, ClaimRegistry
from core.models import db as db_module

"""
MICROCORREÇÃO BF3 MULTITENANT — IDENTIDADE PERSISTENTE REALMENTE
ESCOPADA.

Falha real reproduzida por auditoria independente:
`evidence_claim_registry` ainda usava `claim_id` como PRIMARY KEY
GLOBAL, e `_persist_claim()` usava `INSERT OR REPLACE` — assim,
appB/tenantB podia registrar o MESMO `claim_id` de appA/tenantA e
SUBSTITUIR o registro de A (mesmo com as colunas de escopo já
presentes e o SELECT já filtrado por elas da correção anterior — a
identidade de ESCRITA continuava sendo só `claim_id`).

Corrigido: PRIMARY KEY composta (application_id, tenant_id,
governance_context_id, claim_id) — o MESMO claim_id pode existir em
escopos diferentes sem colidir, sobrescrever, alterar ou ocultar o
outro. Migração via reconstrução de tabela (SQLite não permite ALTER
de PRIMARY KEY diretamente), atômica (uma transação) e idempotente
(re-executável sem duplicar/perder dado).
"""


class ClaimIdCollisionAcrossTenantsTests(unittest.TestCase):
    """Teste negativo EXATO exigido: A registra `claim_same`; B
    registra também `claim_same`; após restart, A resolve somente o
    conteúdo de A e B somente o conteúdo de B, com DUAS linhas
    persistidas."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_same_claim_id_two_tenants_survives_real_restart_without_collision(self):
        registry_a = ClaimRegistry(
            db_path=self.db_path, application_id="appA", tenant_id="tenantA", governance_context_id="ctxA",
        )
        registry_b = ClaimRegistry(
            db_path=self.db_path, application_id="appB", tenant_id="tenantB", governance_context_id="ctxB",
        )

        claim_a = ClaimRecord(
            claim_id="claim_same", strategy_id="strat_a", claim_type="PERFORMANCE", statement="conteúdo real de A",
            related_metric_names=["roi_a"],
        )
        claim_b = ClaimRecord(
            claim_id="claim_same", strategy_id="strat_b", claim_type="RISK", statement="conteúdo real de B",
            related_metric_names=["roi_b"],
        )
        registry_a.register(claim_a)
        registry_b.register(claim_b)

        # RESTART REAL -- novas instâncias, mesmo db_path
        registry_a_restart = ClaimRegistry(
            db_path=self.db_path, application_id="appA", tenant_id="tenantA", governance_context_id="ctxA",
        )
        registry_b_restart = ClaimRegistry(
            db_path=self.db_path, application_id="appB", tenant_id="tenantB", governance_context_id="ctxB",
        )

        resolved_a = registry_a_restart.resolve("claim_same")
        resolved_b = registry_b_restart.resolve("claim_same")

        self.assertIsNotNone(resolved_a)
        self.assertIsNotNone(resolved_b)
        self.assertEqual(resolved_a.statement, "conteúdo real de A")
        self.assertEqual(resolved_b.statement, "conteúdo real de B")
        self.assertEqual(resolved_a.claim_type, "PERFORMANCE")
        self.assertEqual(resolved_b.claim_type, "RISK")
        self.assertNotEqual(resolved_a.strategy_id, resolved_b.strategy_id)

        # DUAS linhas persistidas -- nunca uma sobrescreveu a outra
        conn = sqlite3.connect(self.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM evidence_claim_registry WHERE claim_id = 'claim_same'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

        # A nunca vê o claim de B, e vice-versa
        self.assertFalse(registry_a_restart.matches("claim_same", strategy_id="strat_b"))
        self.assertTrue(registry_a_restart.matches("claim_same", strategy_id="strat_a"))
        self.assertTrue(registry_b_restart.matches("claim_same", strategy_id="strat_b"))
        self.assertFalse(registry_b_restart.matches("claim_same", strategy_id="strat_a"))

    def test_a_registering_after_b_never_overwrites_b(self):
        """Ordem inversa — B registra primeiro, A registra depois com
        o mesmo claim_id — B continua intacto."""
        registry_b = ClaimRegistry(db_path=self.db_path, application_id="appB", tenant_id="tenantB", governance_context_id="ctxB")
        registry_b.register(ClaimRecord(claim_id="claim_x", strategy_id="s_b", claim_type="X", statement="B primeiro"))

        registry_a = ClaimRegistry(db_path=self.db_path, application_id="appA", tenant_id="tenantA", governance_context_id="ctxA")
        registry_a.register(ClaimRecord(claim_id="claim_x", strategy_id="s_a", claim_type="Y", statement="A depois"))

        registry_b_fresh = ClaimRegistry(db_path=self.db_path, application_id="appB", tenant_id="tenantB", governance_context_id="ctxB")
        self.assertEqual(registry_b_fresh.resolve("claim_x").statement, "B primeiro")

    def test_reregistering_own_claim_id_still_updates_in_place(self):
        """Confirma que `INSERT OR REPLACE` ainda funciona CORRETAMENTE
        dentro do MESMO escopo — reregistrar o próprio claim_id
        atualiza (não duplica) a própria linha."""
        registry_a = ClaimRegistry(db_path=self.db_path, application_id="appA", tenant_id="tenantA", governance_context_id="ctxA")
        registry_a.register(ClaimRecord(claim_id="claim_y", strategy_id="s", claim_type="X", statement="versão 1"))
        registry_a.register(ClaimRecord(claim_id="claim_y", strategy_id="s", claim_type="X", statement="versão 2"))

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM evidence_claim_registry WHERE claim_id='claim_y'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

        registry_fresh = ClaimRegistry(db_path=self.db_path, application_id="appA", tenant_id="tenantA", governance_context_id="ctxA")
        self.assertEqual(registry_fresh.resolve("claim_y").statement, "versão 2")

    def test_three_tenants_same_claim_id_all_isolated(self):
        registries = {
            name: ClaimRegistry(db_path=self.db_path, application_id=name, tenant_id=f"t_{name}", governance_context_id=f"ctx_{name}")
            for name in ("appA", "appB", "appC")
        }
        for name, registry in registries.items():
            registry.register(ClaimRecord(claim_id="claim_compartilhado", strategy_id=f"s_{name}", claim_type="X", statement=f"conteúdo de {name}"))

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM evidence_claim_registry WHERE claim_id='claim_compartilhado'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 3)

        for name in ("appA", "appB", "appC"):
            fresh = ClaimRegistry(db_path=self.db_path, application_id=name, tenant_id=f"t_{name}", governance_context_id=f"ctx_{name}")
            self.assertEqual(fresh.resolve("claim_compartilhado").statement, f"conteúdo de {name}")


class CompositePrimaryKeyMigrationTests(unittest.TestCase):
    """Migração de reconstrução de tabela — atômica e idempotente."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _create_previous_version_schema(self):
        """Schema exatamente como no ZIP imediatamente anterior:
        colunas de escopo já existem, mas claim_id ainda é PK
        sozinho."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE evidence_claim_registry (claim_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, "
            "claim_type TEXT NOT NULL, statement TEXT NOT NULL, related_metric_names TEXT NOT NULL, "
            "status TEXT NOT NULL, evidence_requirements TEXT NOT NULL, created_at REAL NOT NULL, "
            "application_id TEXT NOT NULL DEFAULT '', tenant_id TEXT NOT NULL DEFAULT '', "
            "governance_context_id TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO evidence_claim_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("claim_pre_pk_fix", "strat_x", "X", "claim antes da correção de PK", "[]", "OPEN", "[]", 1000.0,
             "appPrevio", "tenantPrevio", "ctxPrevio"),
        )
        conn.commit()
        conn.close()

    def test_migration_establishes_composite_primary_key(self):
        self._create_previous_version_schema()
        db_module.init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        pk_columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(evidence_claim_registry)").fetchall() if r["pk"] > 0
        }
        conn.close()
        self.assertEqual(pk_columns, {"application_id", "tenant_id", "governance_context_id", "claim_id"})

    def test_migration_preserves_existing_data(self):
        self._create_previous_version_schema()
        db_module.init_db(self.db_path)

        registry = ClaimRegistry(
            db_path=self.db_path, application_id="appPrevio", tenant_id="tenantPrevio",
            governance_context_id="ctxPrevio",
        )
        restored = registry.resolve("claim_pre_pk_fix")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.statement, "claim antes da correção de PK")

    def test_migration_idempotent_multiple_runs(self):
        self._create_previous_version_schema()
        for _ in range(4):
            db_module.init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM evidence_claim_registry").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_migration_from_fully_legacy_schema_without_scope_columns(self):
        """Cadeia completa de migração: banco SEM nenhuma coluna de
        escopo (anterior a duas correções atrás) + PK antiga — tudo
        resolvido numa única passada de `init_db()`."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE evidence_claim_registry (claim_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, "
            "claim_type TEXT NOT NULL, statement TEXT NOT NULL, related_metric_names TEXT NOT NULL, "
            "status TEXT NOT NULL, evidence_requirements TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO evidence_claim_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("claim_bem_antigo", "strat_antigo", "X", "claim bem antigo", "[]", "OPEN", "[]", 500.0),
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
            ("o1", "g1", "a1", "appBemAntigo", "tenantBemAntigo", 400.0, "bootstrap antigo", "authreg_antigo"),
        )
        conn.commit()
        conn.close()

        db_module.init_db(self.db_path)

        conn2 = sqlite3.connect(self.db_path)
        conn2.row_factory = sqlite3.Row
        pk_columns = {
            r["name"] for r in conn2.execute("PRAGMA table_info(evidence_claim_registry)").fetchall() if r["pk"] > 0
        }
        row = conn2.execute("SELECT * FROM evidence_claim_registry WHERE claim_id='claim_bem_antigo'").fetchone()
        conn2.close()

        self.assertEqual(pk_columns, {"application_id", "tenant_id", "governance_context_id", "claim_id"})
        self.assertEqual(row["application_id"], "appBemAntigo")
        self.assertEqual(row["governance_context_id"], "authreg_antigo")

        registry = ClaimRegistry(
            db_path=self.db_path, application_id="appBemAntigo", tenant_id="tenantBemAntigo",
            governance_context_id="authreg_antigo",
        )
        self.assertIsNotNone(registry.resolve("claim_bem_antigo"))

    def test_fresh_database_already_has_composite_primary_key(self):
        db_module.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        pk_columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(evidence_claim_registry)").fetchall() if r["pk"] > 0
        }
        conn.close()
        self.assertEqual(pk_columns, {"application_id", "tenant_id", "governance_context_id", "claim_id"})


if __name__ == "__main__":
    unittest.main()
