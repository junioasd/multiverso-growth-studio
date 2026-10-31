import http.server
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest

from atlas.escalation import EscalationDesk
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.execution.spend_guard import SpendContextMismatch, SpendGuard
from bridge.execution.vault import TokenType, TokenVault
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

"""
MICROCORREÇÃO — ATOMIC ANTI-REPLAY + STRICT SPEND CONTEXT.

Testes obrigatórios usando um SERVIDOR HTTP LOCAL REAL (nunca
`mock.patch` em `urllib.request.urlopen`) para a verificação de
atomicidade — a mesma disciplina que a auditoria independente usou
para encontrar a corrida original. `mock.patch` de rede não é
suficiente para expor condições de corrida reais de I/O; um servidor
real, com uma thread própria, é necessário para provar genuinamente
que exatamente UMA requisição chega ao servidor.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    """Handler HTTP real — conta requisições recebidas de verdade,
    com um pequeno atraso real para aumentar a chance de expor
    qualquer janela de corrida remanescente."""

    received_requests: list = []
    _lock = threading.Lock()

    def do_POST(self):
        with self._lock:
            self.received_requests.append(time.time())
        time.sleep(0.03)  # simula trabalho real do lado do servidor
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

    def do_something(self, **kwargs):
        return self._request(action="DO_SOMETHING", method="POST", path="/action", body=kwargs)


class AtomicAntiReplayRealServerTests(unittest.TestCase):
    """Servidor HTTP real, iniciado/parado por teste — nunca mockado."""

    def setUp(self):
        _CountingHandler.received_requests = []
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CountingHandler)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        self.audit = AuditTrail()
        self.hub = BridgeHub()
        self.hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True,
            health=HealthStatus.OK, permissions_ok=True,
        )
        self.desk = EscalationDesk(self.audit)
        self.vault = TokenVault()
        ref = self.vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
        _RealHttpAdapter.base_url = f"http://127.0.0.1:{self.port}"
        self.adapter = _RealHttpAdapter(vault=self.vault, account_reference_id=ref)
        self.nav = NavigatorReal(
            bridge_hub=self.hub, escalation_desk=self.desk, adapters_by_platform={"youtube": self.adapter},
            audit=self.audit,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _approve(self, idempotency_key="idem-1"):
        blocked = self.nav.execute_action(
            platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
            params={"video_id": "v1"}, actor="teste", idempotency_key=idempotency_key,
        )
        self.desk.approve(blocked.escalation_id, decision="ok", resolved_by=OWNER)
        return blocked.escalation_id

    def _run_concurrent(self, *, n_threads: int, idempotency_key: str, escalation_id: str) -> list:
        results = []

        def worker():
            r = self.nav.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                idempotency_key=idempotency_key, approved_escalation_id=escalation_id,
            )
            results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_two_concurrent_same_idempotency_key_single_http_effect(self):
        esc_id = self._approve()
        results = self._run_concurrent(n_threads=2, idempotency_key="idem-1", escalation_id=esc_id)
        self.assertEqual(len(_CountingHandler.received_requests), 1)
        self.assertTrue(all(r.success for r in results))

    def test_ten_concurrent_same_idempotency_key_single_http_effect(self):
        esc_id = self._approve()
        results = self._run_concurrent(n_threads=10, idempotency_key="idem-1", escalation_id=esc_id)
        self.assertEqual(len(_CountingHandler.received_requests), 1)
        self.assertTrue(all(r.success for r in results))

    def test_fifty_concurrent_same_idempotency_key_single_http_effect(self):
        esc_id = self._approve()
        results = self._run_concurrent(n_threads=50, idempotency_key="idem-1", escalation_id=esc_id)
        self.assertEqual(len(_CountingHandler.received_requests), 1)
        self.assertTrue(all(r.success for r in results))

    def test_mismatched_idempotency_key_rejected_never_duplicates(self):
        esc_id = self._approve(idempotency_key="idem-1")
        results = []

        def worker(key):
            r = self.nav.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                idempotency_key=key, approved_escalation_id=esc_id,
            )
            results.append((key, r))

        threads = [threading.Thread(target=worker, args=("idem-1",)) for _ in range(5)]
        threads += [threading.Thread(target=worker, args=(f"idem-x-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(_CountingHandler.received_requests), 1)
        matching = [r for k, r in results if k == "idem-1"]
        mismatched = [r for k, r in results if k != "idem-1"]
        self.assertTrue(all(r.success for r in matching))
        self.assertTrue(all(not r.success for r in mismatched))

    def test_cross_instance_same_db_single_http_effect(self):
        """Duas instâncias DIFERENTES de NavigatorReal, compartilhando
        o MESMO db_path — simula dois processos diferentes."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            desk = EscalationDesk(self.audit, db_path=db_path)
            vault = TokenVault(db_path=db_path)
            ref = vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")

            nav_a = NavigatorReal(
                bridge_hub=self.hub, escalation_desk=desk,
                adapters_by_platform={"youtube": _RealHttpAdapter(vault=vault, account_reference_id=ref)},
                db_path=db_path, audit=self.audit,
            )
            nav_b = NavigatorReal(
                bridge_hub=self.hub, escalation_desk=desk,
                adapters_by_platform={"youtube": _RealHttpAdapter(vault=vault, account_reference_id=ref)},
                db_path=db_path, audit=self.audit,
            )

            blocked = nav_a.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", idempotency_key="idem-1",
            )
            desk.approve(blocked.escalation_id, decision="ok", resolved_by=OWNER)

            results = []

            def worker(instance):
                r = instance.execute_action(
                    platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                    params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                    idempotency_key="idem-1", approved_escalation_id=blocked.escalation_id,
                )
                results.append(r)

            threads = []
            for _ in range(8):
                threads.append(threading.Thread(target=worker, args=(nav_a,)))
                threads.append(threading.Thread(target=worker, args=(nav_b,)))
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(_CountingHandler.received_requests), 1)
            self.assertTrue(all(r.success for r in results))
        finally:
            db_module.remove_db_file(db_path)

    def test_restart_after_crash_does_not_permanently_block(self):
        """Um claim travado (simulando um processo que morreu no meio
        da execução, sem chance de liberar) é reconciliado com
        segurança, nunca trava a aprovação para sempre."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            desk = EscalationDesk(self.audit, db_path=db_path)
            vault = TokenVault(db_path=db_path)
            ref = vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
            nav = NavigatorReal(
                bridge_hub=self.hub, escalation_desk=desk,
                adapters_by_platform={"youtube": _RealHttpAdapter(vault=vault, account_reference_id=ref)},
                db_path=db_path, audit=self.audit,
            )
            blocked = nav.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", idempotency_key="idem-1",
            )
            desk.approve(blocked.escalation_id, decision="ok", resolved_by=OWNER)

            with db_module.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO navigator_execution_claims (escalation_id, status, idempotency_key, claimed_at) "
                    "VALUES (?, 'CLAIMED', ?, ?)",
                    (blocked.escalation_id, "idem-crashed", time.time() - 200),
                )

            # COMANDO 3/6 (conflito de requisitos resolvido, com prova por
            # crash REAL em `tests/test_comando_3_navigator_forge_execucao.py`):
            # o processo que morreu PODE ter enviado a requisição — reexecutar
            # automaticamente duplicava a ação externa. Agora o claim antigo
            # vira UNKNOWN_OUTCOME: nenhuma requisição sai sem reconciliação.
            before = nav.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                idempotency_key="idem-1", approved_escalation_id=blocked.escalation_id,
            )
            self.assertFalse(before.success)
            self.assertEqual(before.delivery_state, "UNKNOWN")
            self.assertEqual(len(_CountingHandler.received_requests), 0, "sem reconciliação, nada é reenviado")

            # Intenção original preservada: a aprovação NUNCA fica travada
            # para sempre — o Owner reconcilia (aqui: nada foi executado).
            nav.reconcile_unknown_outcome(
                escalation_id=blocked.escalation_id, executed=False,
                evidence="verificado na plataforma: nenhuma alteração aplicada", resolved_by=OWNER,
            )
            start = time.time()
            result = nav.execute_action(
                platform="youtube", action="UPDATE_VIDEO_METADATA", criticality=ActionCriticality.CRITICAL,
                params={"video_id": "v1"}, actor="teste", adapter_method="do_something",
                idempotency_key="idem-1", approved_escalation_id=blocked.escalation_id,
            )
            duration = time.time() - start

            self.assertTrue(result.success)
            self.assertEqual(len(_CountingHandler.received_requests), 1)
            self.assertLess(duration, 5.0)
        finally:
            db_module.remove_db_file(db_path)


class StrictSpendContextRealServerTests(unittest.TestCase):
    """MICROCORREÇÃO — STRICT SPEND CONTEXT: valida em TODA chamada,
    inclusive reconfirmação. Cenário exato reportado pela auditoria."""

    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)
        self.guard = SpendGuard(escalation_desk=self.desk)
        self.esc_id = self.guard.request_spend_approval(
            platform="google_ads", campaign_reference="camp1", requested_amount=100.0, currency="BRL",
            actor="teste", reason="teste",
        )
        self.desk.approve(self.esc_id, decision="ok", resolved_by=OWNER)

    def test_first_confirmation_with_correct_context_succeeds(self):
        approval = self.guard.confirm_approval(
            escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL",
        )
        self.assertEqual(approval.approved_amount, 100.0)

    def test_reconfirmation_with_different_platform_rejected(self):
        self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        with self.assertRaises(SpendContextMismatch):
            self.guard.confirm_approval(escalation_id=self.esc_id, platform="facebook_ads", campaign_reference="camp1", currency="BRL")

    def test_reconfirmation_with_different_campaign_rejected(self):
        self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        with self.assertRaises(SpendContextMismatch):
            self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="outra_campanha", currency="BRL")

    def test_reconfirmation_with_different_currency_rejected(self):
        self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        with self.assertRaises(SpendContextMismatch):
            self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="USD")

    def test_reconfirmation_with_all_different_rejected_exact_auditor_scenario(self):
        """Cenário EXATO reportado: confirma corretamente, depois
        chama de novo com plataforma/campanha/moeda TODAS diferentes."""
        first = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        with self.assertRaises(SpendContextMismatch):
            self.guard.confirm_approval(escalation_id=self.esc_id, platform="tiktok_ads", campaign_reference="campanha_nova", currency="USD")
        # aprovacao original nunca e afetada pela tentativa rejeitada
        self.assertEqual(self.guard.total_approved_budget(), 100.0)
        still_same = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        self.assertEqual(first.approval_id, still_same.approval_id)

    def test_reconfirmation_with_same_context_still_idempotent(self):
        a1 = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        a2 = self.guard.confirm_approval(escalation_id=self.esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        self.assertEqual(a1.approval_id, a2.approval_id)
        self.assertEqual(self.guard.total_approved_budget(), 100.0)


if __name__ == "__main__":
    unittest.main()
