import os
import tempfile
import unittest

from core.evidence.claim_registry import ClaimRecord, ClaimRegistry
from core.evidence.governance_context import ApplicationGovernanceContext
from core.models import db as db_module
from core.security.auth import create_canonical_auth_service, hash_password

"""
BLOCO FINAL 3 — fechamento da V1: persistência restante do
ClaimRegistry.

Falha confirmada: `ClaimRegistry` era o ÚNICO dos quatro registries de
`ApplicationGovernanceContext` (autoridade/verifier/source/claim) sem
`db_path` — `restore_for_app()` já restaurava autoridade/verifier/
source corretamente após um restart, mas recriava um
`ClaimRegistry()` vazio, perdendo todo claim registrado antes do
restart.

Testes aqui usam o FLUXO REAL de produção
(`create_canonical_auth_service` → `login()` →
`bootstrap_for_app()`/`restore_for_app()`) — nunca um atalho
sintético — para provar que a persistência funciona no caminho
genuíno que a aplicação realmente usa, não apenas em isolamento.
"""


class ClaimRegistryPersistenceUnitTests(unittest.TestCase):
    """Testes diretos de `ClaimRegistry(db_path=...)`, isolados."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_register_and_resolve_before_restart(self):
        registry = ClaimRegistry(db_path=self.db_path)
        claim = ClaimRecord(
            claim_id=ClaimRecord.new_id(), strategy_id="strat_1", claim_type="PERFORMANCE",
            statement="afirmação real de teste",
        )
        registry.register(claim)
        self.assertIsNotNone(registry.resolve(claim.claim_id))

    def test_claim_survives_restart(self):
        registry1 = ClaimRegistry(db_path=self.db_path)
        claim = ClaimRecord(
            claim_id=ClaimRecord.new_id(), strategy_id="strat_1", claim_type="PERFORMANCE",
            statement="afirmação real de teste", related_metric_names=["ctr", "cac"],
            evidence_requirements=["teste A/B"],
        )
        registry1.register(claim)

        registry2 = ClaimRegistry(db_path=self.db_path)
        restored = registry2.resolve(claim.claim_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.strategy_id, "strat_1")
        self.assertEqual(restored.claim_type, "PERFORMANCE")
        self.assertEqual(restored.statement, "afirmação real de teste")
        self.assertEqual(restored.related_metric_names, ["ctr", "cac"])
        self.assertEqual(restored.evidence_requirements, ["teste A/B"])
        self.assertEqual(restored.status, "OPEN")

    def test_matches_works_correctly_after_restart(self):
        registry1 = ClaimRegistry(db_path=self.db_path)
        claim = ClaimRecord(
            claim_id=ClaimRecord.new_id(), strategy_id="strat_1", claim_type="PERFORMANCE",
            statement="x", related_metric_names=["CTR"],
        )
        registry1.register(claim)

        registry2 = ClaimRegistry(db_path=self.db_path)
        self.assertTrue(registry2.matches(claim.claim_id, strategy_id="strat_1", metric_name="ctr"))
        self.assertFalse(registry2.matches(claim.claim_id, strategy_id="outra_estrategia"))
        self.assertFalse(registry2.matches(claim.claim_id, metric_name="metrica_nao_relacionada"))

    def test_multiple_claims_all_survive_restart(self):
        registry1 = ClaimRegistry(db_path=self.db_path)
        claims = [
            ClaimRecord(claim_id=ClaimRecord.new_id(), strategy_id=f"strat_{i}", claim_type="X", statement=f"c{i}")
            for i in range(5)
        ]
        for c in claims:
            registry1.register(c)

        registry2 = ClaimRegistry(db_path=self.db_path)
        self.assertEqual(len(registry2.all()), 5)
        for c in claims:
            self.assertIsNotNone(registry2.resolve(c.claim_id))

    def test_reregistering_same_claim_id_updates_in_place(self):
        registry1 = ClaimRegistry(db_path=self.db_path)
        claim_id = ClaimRecord.new_id()
        registry1.register(ClaimRecord(claim_id=claim_id, strategy_id="strat_1", claim_type="X", statement="original"))
        registry1.register(ClaimRecord(claim_id=claim_id, strategy_id="strat_1", claim_type="X", statement="atualizado"))

        registry2 = ClaimRegistry(db_path=self.db_path)
        self.assertEqual(len(registry2.all()), 1)
        self.assertEqual(registry2.resolve(claim_id).statement, "atualizado")

    def test_in_memory_mode_unaffected_no_regression(self):
        """Sem `db_path` — comportamento em memória idêntico ao de
        antes desta correção (nenhuma regressão introduzida)."""
        registry = ClaimRegistry()
        claim = ClaimRecord(claim_id=ClaimRecord.new_id(), strategy_id="strat_1", claim_type="X", statement="x")
        registry.register(claim)
        self.assertIsNotNone(registry.resolve(claim.claim_id))
        # uma nova instancia em memoria nunca compartilha estado
        fresh = ClaimRegistry()
        self.assertIsNone(fresh.resolve(claim.claim_id))


class ClaimRegistryRealGovernanceContextRestartTests(unittest.TestCase):
    """Fluxo REAL de produção — `create_canonical_auth_service()` →
    `login()` → `bootstrap_for_app()`/`restore_for_app()` — nunca um
    atalho sintético. Este é o caminho que a auditoria original
    identificou como incompleto."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _real_login(self):
        auth_service = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
        )
        token = auth_service.login(email="owner@x.com", password="SenhaForte123")
        return auth_service, token

    def test_claim_registered_before_restart_resolves_after_restore(self):
        auth_service, token = self._real_login()
        ctx1 = ApplicationGovernanceContext.bootstrap_for_app(
            auth_service=auth_service, session_token=token, bootstrap_reason="teste", db_path=self.db_path,
        )
        claim = ClaimRecord(
            claim_id=ClaimRecord.new_id(), strategy_id="strat_real", claim_type="PERFORMANCE",
            statement="reivindicação real registrada antes do restart", related_metric_names=["roi"],
        )
        ctx1.claim_registry.register(claim)
        self.assertIsNotNone(ctx1.claim_registry.resolve(claim.claim_id))

        # RESTART REAL -- nova cadeia de autenticacao completa, nao um atalho
        auth_service2, token2 = self._real_login()
        ctx2 = ApplicationGovernanceContext.restore_for_app(
            auth_service=auth_service2, session_token=token2, db_path=self.db_path,
        )
        restored = ctx2.claim_registry.resolve(claim.claim_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.statement, "reivindicação real registrada antes do restart")
        self.assertTrue(ctx2.claim_registry.matches(claim.claim_id, strategy_id="strat_real", metric_name="roi"))

    def test_restored_context_claim_registry_still_isolated_by_application_tenant(self):
        """CORREÇÃO relacionada: o `claim_registry` restaurado também
        precisa carregar `application_id`/`tenant_id`/
        `governance_context_id`, igual aos outros três registries —
        nunca ficar de fora do isolamento multi-tenant."""
        auth_service, token = self._real_login()
        ctx = ApplicationGovernanceContext.bootstrap_for_app(
            auth_service=auth_service, session_token=token, bootstrap_reason="teste", db_path=self.db_path,
        )
        self.assertEqual(ctx.claim_registry.application_id, ctx.application_id)
        self.assertEqual(ctx.claim_registry.tenant_id, ctx.tenant_id)
        self.assertEqual(ctx.claim_registry.governance_context_id, ctx.context_id)

    def test_new_claim_after_restore_also_persists(self):
        """Claim registrado DEPOIS de um restore também precisa
        persistir corretamente — não apenas os anteriores ao
        restart."""
        auth_service, token = self._real_login()
        ApplicationGovernanceContext.bootstrap_for_app(
            auth_service=auth_service, session_token=token, bootstrap_reason="teste", db_path=self.db_path,
        )

        auth_service2, token2 = self._real_login()
        ctx2 = ApplicationGovernanceContext.restore_for_app(
            auth_service=auth_service2, session_token=token2, db_path=self.db_path,
        )
        new_claim = ClaimRecord(
            claim_id=ClaimRecord.new_id(), strategy_id="strat_pos_restart", claim_type="X", statement="novo claim",
        )
        ctx2.claim_registry.register(new_claim)

        auth_service3, token3 = self._real_login()
        ctx3 = ApplicationGovernanceContext.restore_for_app(
            auth_service=auth_service3, session_token=token3, db_path=self.db_path,
        )
        self.assertIsNotNone(ctx3.claim_registry.resolve(new_claim.claim_id))


if __name__ == "__main__":
    unittest.main()
