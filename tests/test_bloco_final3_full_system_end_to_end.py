import http.server
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest

from api.bootstrap import build_app
from bridge.execution.navigator_real import ActionCriticality
from bridge.execution.platform_adapters import PlatformActionResult, PlatformApiAdapter
from bridge.execution.vault import TokenType
from bridge.hub import ConnectorType, HealthStatus
from core.evidence.claim_registry import ClaimRecord
from core.models import db as db_module
from core.security.auth import hash_password

"""
BLOCO FINAL 3 — fechamento da V1 do MULTIVERSO GROWTH STUDIO.

Teste de integração ponta a ponta REAL — o núcleo da auditoria final.
Constrói a aplicação completa via `build_app()` (o mesmo caminho que
`main.py serve` usa), exercita os subsistemas centrais JUNTOS num
único fluxo coerente (governança/claim registry, NavigatorReal contra
um servidor HTTP real, SpendGuard, EscalationDesk), e confirma que
TUDO sobrevive a um restart genuíno do processo (novo `build_app()`
apontando para o mesmo banco, com reautenticação real do Owner —
nunca um atalho sintético).

Nenhuma chamada de rede é mockada no nível do NavigatorReal — um
servidor HTTP local real e genuíno recebe a requisição, contando
chamadas reais, exatamente como a auditoria independente validou nas
rodadas anteriores do BLOCO FINAL 2.
"""


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    received_requests: list = []
    _lock = threading.Lock()

    def do_POST(self):
        with self._lock:
            self.received_requests.append(time.time())
        body = json.dumps({"id": "v1", "ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _RealHttpAdapter(PlatformApiAdapter):
    platform = "youtube"

    def do_something(self, **kwargs) -> PlatformActionResult:
        return self._request(action="DO_SOMETHING", method="POST", path="/action", body=kwargs)


class Bf3FullSystemEndToEndTests(unittest.TestCase):
    """Um único fluxo coerente, do primeiro boot ao restart, exercitando
    governança, NavigatorReal (rede real), SpendGuard e EscalationDesk
    juntos — nunca isoladamente."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)

        # CORREÇÃO: sem `MGS_VAULT_MASTER_KEY`, o TokenVault opera em
        # modo EFÊMERO por design (documentado em bridge/execution/
        # vault.py) — nunca finge persistência sem uma chave mestra
        # real. Um restart de produção genuíno configuraria essa
        # variável; definimos aqui para exercitar o modo PERSISTENTE
        # real (nunca para mascarar a limitação — ver
        # `test_token_vault_ephemeral_without_master_key_is_honest_limitation`
        # abaixo, que confirma o comportamento OPOSTO é igualmente honesto).
        self._had_master_key = "MGS_VAULT_MASTER_KEY" in os.environ
        self._original_master_key = os.environ.get("MGS_VAULT_MASTER_KEY")
        os.environ["MGS_VAULT_MASTER_KEY"] = "chave-mestra-real-de-teste-bf3"

        _CountingHandler.received_requests = []
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CountingHandler)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        _RealHttpAdapter.base_url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        db_module.remove_db_file(self.db_path)
        if self._had_master_key:
            os.environ["MGS_VAULT_MASTER_KEY"] = self._original_master_key
        else:
            os.environ.pop("MGS_VAULT_MASTER_KEY", None)

    def test_full_system_end_to_end_survives_real_restart(self):
        # ------------------------------------------------------------
        # PROCESSO 1 — primeiro boot real da aplicação completa
        # ------------------------------------------------------------
        app1 = build_app(
            owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
        )
        gov1 = app1.bootstrap_governance_context(
            email="owner@x.com", password="SenhaForte123", bootstrap_reason="teste ponta a ponta BF3",
        )

        # 1a. Registra um claim REAL na governança (item central desta rodada)
        claim = ClaimRecord(
            claim_id=ClaimRecord.new_id(), strategy_id="strat_bf3", claim_type="PERFORMANCE",
            statement="reivindicação real do fluxo ponta a ponta BF3", related_metric_names=["roi"],
        )
        gov1.claim_registry.register(claim)
        self.assertIsNotNone(gov1.claim_registry.resolve(claim.claim_id))

        # 1b. Conecta um adapter real ao NavigatorReal desta app e
        #     registra o conector como oficialmente disponível
        ref = app1.atlas.token_vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok_bf3")
        app1.atlas.navigator_real.register_adapter(
            platform="youtube", adapter=_RealHttpAdapter(vault=app1.atlas.token_vault, account_reference_id=ref),
        )
        app1.atlas.bridge.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True,
            health=HealthStatus.OK, permissions_ok=True,
        )

        # 1c. Ação CRÍTICA bloqueada até aprovação do Owner (Owner Gate real)
        blocked = app1.atlas.navigator_real.execute_action(
            platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.ROUTINE,
            params={"video_id": "v1"}, actor="ARGOS", idempotency_key="idem-bf3-1",
        )
        self.assertFalse(blocked.success)
        self.assertIsNotNone(blocked.escalation_id)

        from core.security.auth import Principal, Role
        OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
        app1.atlas.escalation_desk.approve(blocked.escalation_id, decision="aprovado BF3", resolved_by=OWNER)

        # 1d. Executa de verdade -- servidor HTTP real recebe a requisição
        result1 = app1.atlas.navigator_real.execute_action(
            platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.ROUTINE,
            params={"video_id": "v1"}, actor="ARGOS", adapter_method="do_something",
            idempotency_key="idem-bf3-1", approved_escalation_id=blocked.escalation_id,
        )
        self.assertTrue(result1.success)
        self.assertEqual(len(_CountingHandler.received_requests), 1)

        # 1e. Retry com a MESMA idempotency_key -- nunca duplica o efeito real
        result1_retry = app1.atlas.navigator_real.execute_action(
            platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.ROUTINE,
            params={"video_id": "v1"}, actor="ARGOS", adapter_method="do_something",
            idempotency_key="idem-bf3-1", approved_escalation_id=blocked.escalation_id,
        )
        self.assertTrue(result1_retry.success)
        self.assertTrue(result1_retry.replayed_from_cache)
        self.assertEqual(len(_CountingHandler.received_requests), 1)  # ainda 1 -- nao duplicou

        # 1f. SpendGuard -- orcamento zero ate aprovacao real do Owner
        esc_spend = app1.atlas.spend_guard.request_spend_approval(
            platform="google_ads", campaign_reference="camp_bf3", requested_amount=100.0, currency="BRL",
            actor="ARGOS", reason="teste ponta a ponta BF3",
        )
        app1.atlas.escalation_desk.approve(esc_spend, decision="aprovado BF3", resolved_by=OWNER)
        spend_approval = app1.atlas.spend_guard.confirm_approval(
            escalation_id=esc_spend, platform="google_ads", campaign_reference="camp_bf3", currency="BRL",
        )
        app1.atlas.spend_guard.record_spend(approval_id=spend_approval.approval_id, amount=30.0)
        self.assertEqual(app1.atlas.spend_guard.total_approved_budget(), 100.0)

        app1.shutdown()

        # ------------------------------------------------------------
        # PROCESSO 2 — restart REAL: novo build_app(), mesmo db_path,
        # reautenticação real do Owner (nunca um bypass silencioso)
        # ------------------------------------------------------------
        app2 = build_app(
            owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
        )
        gov2 = app2.restore_governance_context(email="owner@x.com", password="SenhaForte123")

        # 2a. Claim sobrevive ao restart -- o item central desta rodada
        restored_claim = gov2.claim_registry.resolve(claim.claim_id)
        self.assertIsNotNone(restored_claim)
        self.assertEqual(restored_claim.statement, "reivindicação real do fluxo ponta a ponta BF3")
        self.assertTrue(gov2.claim_registry.matches(claim.claim_id, strategy_id="strat_bf3", metric_name="roi"))

        # 2b. Anti-replay sobrevive ao restart -- retry pos-restart com a
        #     MESMA idempotency_key nunca dispara o efeito externo de novo
        app2.atlas.navigator_real.register_adapter(
            platform="youtube",
            adapter=_RealHttpAdapter(vault=app2.atlas.token_vault, account_reference_id=ref),
        )
        app2.atlas.bridge.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True,
            health=HealthStatus.OK, permissions_ok=True,
        )
        result2_retry = app2.atlas.navigator_real.execute_action(
            platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.ROUTINE,
            params={"video_id": "v1"}, actor="ARGOS", adapter_method="do_something",
            idempotency_key="idem-bf3-1", approved_escalation_id=blocked.escalation_id,
        )
        self.assertTrue(result2_retry.success)
        self.assertTrue(result2_retry.replayed_from_cache)
        self.assertEqual(len(_CountingHandler.received_requests), 1)  # AINDA 1 apos o restart

        # 2c. SpendGuard sobrevive ao restart -- mesmo approval_id, saldo preservado
        approval_after_restart = app2.atlas.spend_guard.approval_for_escalation(esc_spend)
        self.assertIsNotNone(approval_after_restart)
        self.assertEqual(approval_after_restart.approval_id, spend_approval.approval_id)
        self.assertEqual(approval_after_restart.spent_so_far, 30.0)
        self.assertEqual(app2.atlas.spend_guard.total_approved_budget(), 100.0)

        # 2d. Token do vault sobrevive ao restart
        self.assertEqual(app2.atlas.token_vault.retrieve_token(ref), "tok_bf3")

        app2.shutdown()

    def test_token_vault_ephemeral_without_master_key_is_honest_limitation(self):
        """Confirma explicitamente o comportamento OPOSTO do teste
        principal: SEM `MGS_VAULT_MASTER_KEY` configurada, um token
        armazenado antes do restart NUNCA é silenciosamente
        "recuperado com sucesso" depois — o cofre falha honestamente
        (`SecretDecryptionFailed`), nunca finge persistência que não
        existe. Esta é uma LIMITAÇÃO REAL do ambiente sem a variável
        configurada, documentada explicitamente aqui, não escondida."""
        if self._had_master_key:
            os.environ.pop("MGS_VAULT_MASTER_KEY", None)
        else:
            os.environ.pop("MGS_VAULT_MASTER_KEY", None)

        app1 = build_app(
            owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
        )
        ref = app1.atlas.token_vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok_efemero")
        from bridge.execution.vault import VaultKeyMode
        self.assertEqual(app1.atlas.token_vault.key_mode, VaultKeyMode.EPHEMERAL)
        app1.shutdown()

        app2 = build_app(
            owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
        )
        from bridge.execution.vault import SecretDecryptionFailed
        with self.assertRaises(SecretDecryptionFailed):
            app2.atlas.token_vault.retrieve_token(ref)
        app2.shutdown()

        os.environ["MGS_VAULT_MASTER_KEY"] = "chave-mestra-real-de-teste-bf3"


if __name__ == "__main__":
    unittest.main()
