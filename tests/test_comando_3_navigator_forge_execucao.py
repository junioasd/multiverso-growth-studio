"""
COMANDO MESTRE 3/6 — execução externa confiável (NAVIGATOR REAL / ACCESS &
EXECUTION HUB). Servidor HTTP LOCAL REAL (nunca mock de rede) e crash REAL
de processo. Defeitos provados antes da correção:
  D1 — timeout após o serviço executar liberava a aprovação: o retry
       publicava de novo (2 publicações reais);
  D2 — HTTP 202 (aceito, não executado) virava sucesso;
  D3 — processo morto após enviar: no restart o claim antigo era apagado e
       a ação reexecutada (2 publicações reais).
"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from atlas.escalation import EscalationDesk
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.execution.platform_adapters import PlatformApiAdapter, _classify_http_error
from bridge.execution.vault import TokenType, TokenVault
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
ATLAS = Principal(subject="atlas", role=Role.ATLAS, scopes={"*"})
SECRET = "SEGREDO-TOKEN-NUNCA-EXPOR-7f3a"


class _Service(http.server.BaseHTTPRequestHandler):
    """Serviço externo real: EXECUTA ao receber, depois responde conforme o modo."""
    executed: list = []
    mode = {"delay": 0.0, "status": 200, "body": None}
    lock = threading.Lock()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(n)
        with self.lock:
            self.executed.append({"payload": json.loads(payload or b"{}"), "auth": self.headers.get("Authorization")})
        time.sleep(self.mode["delay"])
        out = self.mode["body"] if self.mode["body"] is not None else json.dumps({"id": f"ext_{len(self.executed)}"}).encode()
        try:
            self.send_response(self.mode["status"])
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except Exception:
            pass

    def log_message(self, *args):
        pass


class _Adapter(PlatformApiAdapter):
    platform = "youtube"

    def publish(self, **kwargs):
        return self._request(action="PUBLISH", method="POST", path="/publish", body=kwargs)


class _RealServiceBase(unittest.TestCase):
    def setUp(self):
        _Service.executed = []
        _Service.mode = {"delay": 0.0, "status": 200, "body": None}
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Service)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.audit = AuditTrail()
        self.hub = BridgeHub()
        self.hub.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True,
                                    authenticated=True, health=HealthStatus.OK, permissions_ok=True)
        self.desk = EscalationDesk(self.audit)
        self.vault = TokenVault()
        self.ref = self.vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value=SECRET)
        _Adapter.base_url = f"http://127.0.0.1:{self.port}"
        self.adapter = _Adapter(vault=self.vault, account_reference_id=self.ref, timeout_seconds=0.3)
        self.nav = NavigatorReal(bridge_hub=self.hub, escalation_desk=self.desk,
                                 adapters_by_platform={"youtube": self.adapter}, audit=self.audit)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def kw(self, key="pub-1", title="video 1"):
        return dict(platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL,
                    params={"title": title}, actor="navigator", idempotency_key=key)

    def approved(self, **kw):
        esc = self.nav.execute_action(**kw).escalation_id
        self.desk.approve(esc, decision="ok", resolved_by=OWNER)
        return esc

    def run_action(self, esc, **kw):
        return self.nav.execute_action(**kw, adapter_method="publish", approved_escalation_id=esc)


class DeliveryClassificationTests(unittest.TestCase):
    def test_http_error_classification(self):
        for code in (408, 429, 503):
            self.assertEqual(_classify_http_error(code), "TRANSIENT")
        for code in (500, 502, 504):
            self.assertEqual(_classify_http_error(code), "UNKNOWN")
        for code in (400, 401, 403, 404, 409, 422):
            self.assertEqual(_classify_http_error(code), "REJECTED")


class AmbiguousOutcomeTests(_RealServiceBase):
    def test_timeout_after_execution_never_duplicates(self):
        kw = self.kw()
        esc = self.approved(**kw)
        _Service.mode["delay"] = 0.8
        first = self.run_action(esc, **kw)
        self.assertFalse(first.success)
        self.assertEqual(first.delivery_state, "UNKNOWN")
        time.sleep(0.9)
        _Service.mode["delay"] = 0.0
        retry = self.run_action(esc, **kw)
        self.assertFalse(retry.success)
        self.assertIn("DESCONHECIDO", retry.reason)
        self.assertEqual(len(_Service.executed), 1, "retry após resultado ambíguo NUNCA reenvia")

    def test_owner_reconciles_executed_then_no_resend(self):
        kw = self.kw()
        esc = self.approved(**kw)
        _Service.mode["delay"] = 0.8
        self.run_action(esc, **kw)
        time.sleep(0.9)
        with self.assertRaises(PermissionError):
            self.nav.reconcile_unknown_outcome(escalation_id=esc, executed=True, evidence="x", resolved_by=ATLAS)
        self.assertEqual(self.nav.reconcile_unknown_outcome(
            escalation_id=esc, executed=True, evidence="vídeo ext_1 visível no canal", resolved_by=OWNER),
            "CONFIRMED_EXECUTED")
        _Service.mode["delay"] = 0.0
        again = self.run_action(esc, **kw)
        self.assertTrue(again.replayed_from_cache)
        self.assertEqual(len(_Service.executed), 1)

    def test_owner_reconciles_not_executed_allows_exactly_one_retry(self):
        _Service.mode["status"] = 500
        kw = self.kw()
        esc = self.approved(**kw)
        r = self.run_action(esc, **kw)
        self.assertEqual(r.delivery_state, "UNKNOWN", "HTTP 500: o serviço pode ter processado")
        self.nav.reconcile_unknown_outcome(escalation_id=esc, executed=False, evidence="sem registro na plataforma",
                                           resolved_by=OWNER)
        _Service.mode["status"] = 200
        ok = self.run_action(esc, **kw)
        self.assertTrue(ok.success)
        again = self.run_action(esc, **kw)
        self.assertTrue(again.replayed_from_cache)
        self.assertEqual(len(_Service.executed), 2, "1 ambígua + 1 retry autorizado; o replay não reenvia")

    def test_transient_and_rejected_release_safely(self):
        for status, expected in ((503, "TRANSIENT"), (429, "TRANSIENT"), (400, "REJECTED")):
            _Service.executed = []
            _Service.mode["status"] = status
            kw = self.kw(key=f"k-{status}", title=f"t{status}")
            esc = self.approved(**kw)
            r = self.run_action(esc, **kw)
            self.assertEqual((r.success, r.delivery_state), (False, expected))
            _Service.mode["status"] = 200
            executed_before = len(_Service.executed)
            ok = self.run_action(esc, **kw)
            while not ok.success and "rate limit local" in ok.reason:
                # rate limit LOCAL real (token bucket): nada sai, nada executa;
                # o retry após o backoff indicado é seguro.
                self.assertEqual(ok.delivery_state, "NOT_SENT")
                self.assertEqual(len(_Service.executed), executed_before)
                time.sleep(1.1)
                ok = self.run_action(esc, **kw)
            self.assertTrue(ok.success, f"{status}: nada executado, retry permitido")

    def test_accepted_is_not_executed_and_never_resent(self):
        _Service.mode["status"] = 202
        kw = self.kw()
        esc = self.approved(**kw)
        r = self.run_action(esc, **kw)
        self.assertEqual((r.success, r.delivery_state), (False, "ACCEPTED"))
        self.assertIn("ACEITOU", r.reason)
        _Service.mode["status"] = 200
        again = self.run_action(esc, **kw)
        self.assertTrue(again.replayed_from_cache)
        self.assertEqual(len(_Service.executed), 1)

    def test_unreadable_2xx_is_unknown_not_success(self):
        _Service.mode["body"] = b"<html>nao json</html>"
        kw = self.kw()
        esc = self.approved(**kw)
        r = self.run_action(esc, **kw)
        self.assertEqual((r.success, r.delivery_state), (False, "UNKNOWN"))

    def test_connection_refused_is_not_sent_and_retry_is_safe(self):
        dead = _Adapter(vault=self.vault, account_reference_id=self.ref, timeout_seconds=0.3)
        dead.base_url = "http://127.0.0.1:9"
        nav = NavigatorReal(bridge_hub=self.hub, escalation_desk=self.desk, adapters_by_platform={"youtube": dead},
                            audit=self.audit)
        kw = self.kw()
        esc = nav.execute_action(**kw).escalation_id
        self.desk.approve(esc, decision="ok", resolved_by=OWNER)
        r = nav.execute_action(**kw, adapter_method="publish", approved_escalation_id=esc)
        self.assertEqual((r.success, r.delivery_state), (False, "NOT_SENT"))
        ok = self.run_action(esc, **kw)
        self.assertTrue(ok.success)


class AdversarialTests(_RealServiceBase):
    def test_critical_action_never_runs_without_approval(self):
        r = self.nav.execute_action(**self.kw(), adapter_method="publish")
        self.assertFalse(r.success)
        self.assertIsNotNone(r.escalation_id)
        self.assertEqual(len(_Service.executed), 0)

    def test_old_approval_never_authorizes_modified_payload(self):
        kw = self.kw(title="aprovado")
        esc = self.approved(**kw)
        r = self.run_action(esc, **{**kw, "params": {"title": "ALTERADO depois da aprovação"}})
        self.assertFalse(r.success)
        self.assertIn("DIFERENTE", r.reason)
        self.assertEqual(len(_Service.executed), 0)

    def test_caller_cannot_downgrade_critical_action(self):
        r = self.nav.execute_action(**{**self.kw(), "criticality": ActionCriticality.ROUTINE}, adapter_method="publish")
        self.assertFalse(r.success)
        self.assertEqual(len(_Service.executed), 0)

    def test_secret_reaches_only_the_service_never_audit_or_results(self):
        kw = self.kw()
        esc = self.approved(**kw)
        r = self.run_action(esc, **kw)
        self.assertTrue(r.success)
        self.assertEqual(_Service.executed[0]["auth"], f"Bearer {SECRET}", "o token real chega ao serviço")
        blob = json.dumps([e.__dict__ for e in self.audit.all()], default=str) + json.dumps(r.__dict__, default=str)
        self.assertNotIn(SECRET, blob)

    def test_unavailable_official_path_never_falls_back_to_unsafe_path(self):
        hub = BridgeHub()
        hub.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True,
                               authenticated=True, health=HealthStatus.OK, permissions_ok=True)
        nav = NavigatorReal(bridge_hub=hub, escalation_desk=self.desk, adapters_by_platform={}, audit=self.audit)
        kw = self.kw()
        esc = nav.execute_action(**kw).escalation_id
        self.desk.approve(esc, decision="ok", resolved_by=OWNER)
        r = nav.execute_action(**kw, adapter_method="publish", approved_escalation_id=esc, url="http://x")
        self.assertFalse(r.success)
        self.assertEqual(r.mechanism_used, ConnectorType.OFFICIAL_API.value)
        self.assertEqual(len(_Service.executed), 0)

    def test_browser_never_chosen_when_official_api_available(self):
        hub = BridgeHub()
        for ct in (ConnectorType.BROWSER_AUTOMATION, ConnectorType.OFFICIAL_API):
            hub.register_connector(platform="youtube", connector_type=ct, is_configured=True, authenticated=True,
                                   health=HealthStatus.OK, permissions_ok=True)
        self.assertEqual(hub.best_available_execution_path(platform="youtube").connector_type,
                         ConnectorType.OFFICIAL_API)


_CRASH_WORKER = r'''
import os, sys, threading
sys.path.insert(0, sys.argv[5])
from atlas.escalation import EscalationDesk
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.execution.vault import TokenVault
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.security.audit import AuditTrail
db, port, esc, ref = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
class A(PlatformApiAdapter):
    platform = "youtube"; base_url = "http://127.0.0.1:" + port
    def publish(self, **kw):
        threading.Timer(0.2, lambda: os._exit(9)).start()   # morre após o serviço receber
        return self._request(action="PUBLISH", method="POST", path="/publish", body=kw)
hub = BridgeHub()
hub.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True,
                       authenticated=True, health=HealthStatus.OK, permissions_ok=True)
nav = NavigatorReal(bridge_hub=hub, escalation_desk=EscalationDesk(AuditTrail(), db_path=db),
                    adapters_by_platform={"youtube": A(vault=TokenVault(db_path=db), account_reference_id=ref,
                                                       timeout_seconds=5)}, db_path=db, audit=AuditTrail())
nav.execute_action(platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL,
                   params={"title": "v"}, actor="navigator", idempotency_key="pub-crash", adapter_method="publish",
                   approved_escalation_id=esc)
'''


class CrashRestartTests(unittest.TestCase):
    def setUp(self):
        _Service.executed = []
        _Service.mode = {"delay": 0.5, "status": 200, "body": None}
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Service)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db)
        db_module.init_db(self.db)
        self.key = "chave-mestra-teste-comando-3"
        self._old = os.environ.get("MGS_VAULT_MASTER_KEY")
        os.environ["MGS_VAULT_MASTER_KEY"] = self.key

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        db_module.remove_db_file(self.db)
        if self._old is None:
            os.environ.pop("MGS_VAULT_MASTER_KEY", None)
        else:
            os.environ["MGS_VAULT_MASTER_KEY"] = self._old

    def _nav(self):
        hub = BridgeHub()
        hub.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True,
                               authenticated=True, health=HealthStatus.OK, permissions_ok=True)
        desk = EscalationDesk(AuditTrail(), db_path=self.db)
        _Adapter.base_url = f"http://127.0.0.1:{self.port}"
        return NavigatorReal(bridge_hub=hub, escalation_desk=desk,
                             adapters_by_platform={"youtube": _Adapter(vault=TokenVault(db_path=self.db),
                                                                       account_reference_id=self.ref)},
                             db_path=self.db, audit=AuditTrail()), desk

    def test_crash_after_send_never_reexecutes_on_restart(self):
        self.ref = TokenVault(db_path=self.db).store_token(platform="youtube",
                                                           token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
        nav, desk = self._nav()
        kw = dict(platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL,
                  params={"title": "v"}, actor="navigator", idempotency_key="pub-crash")
        esc = nav.execute_action(**kw).escalation_id
        desk.approve(esc, decision="ok", resolved_by=OWNER)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run([sys.executable, "-c", _CRASH_WORKER, self.db, str(self.port), esc, self.ref, root],
                              timeout=60, capture_output=True)
        self.assertEqual(proc.returncode, 9, proc.stderr.decode()[-500:])
        self.assertEqual(len(_Service.executed), 1, "o serviço executou antes do crash")
        with db_module.connect(self.db) as conn:
            conn.execute("UPDATE navigator_execution_claims SET claimed_at = claimed_at - 200")
        nav2, _ = self._nav()                                     # restart
        r = nav2.execute_action(**kw, adapter_method="publish", approved_escalation_id=esc)
        time.sleep(0.6)
        self.assertFalse(r.success)
        self.assertEqual(r.delivery_state, "UNKNOWN")
        self.assertEqual(len(_Service.executed), 1, "restart NUNCA reexecuta ação possivelmente executada")
        nav2.reconcile_unknown_outcome(escalation_id=esc, executed=True, evidence="publicação ext_1 confirmada",
                                       resolved_by=OWNER)
        again = nav2.execute_action(**kw, adapter_method="publish", approved_escalation_id=esc)
        self.assertTrue(again.success and again.replayed_from_cache)
        self.assertEqual(len(_Service.executed), 1)


if __name__ == "__main__":
    unittest.main()
