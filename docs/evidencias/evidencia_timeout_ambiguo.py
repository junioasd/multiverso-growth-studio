import http.server, json, socketserver, sys, threading, time
sys.path.insert(0, '.')
from atlas.escalation import EscalationDesk
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.execution.vault import TokenType, TokenVault
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
EXECUTED = []; MODE = {"delay": 0.0, "status": 200}
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n)
        EXECUTED.append(json.loads(body))           # o servico EXECUTA a publicacao
        time.sleep(MODE["delay"])                     # ...e a resposta atrasa
        out = json.dumps({"id": f"post_{len(EXECUTED)}"}).encode()
        try:
            self.send_response(MODE["status"]); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        except Exception: pass
    def log_message(self, *a): pass
class A(PlatformApiAdapter):
    platform = "youtube"
    def publish(self, **kw): return self._request(action="PUBLISH", method="POST", path="/publish", body=kw)
srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
audit = AuditTrail(); hub = BridgeHub()
hub.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True)
desk = EscalationDesk(audit); vault = TokenVault()
ref = vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="SEGREDO-XYZ")
A.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
nav = NavigatorReal(bridge_hub=hub, escalation_desk=desk, adapters_by_platform={"youtube": A(vault=vault, account_reference_id=ref, timeout_seconds=0.3)}, audit=audit)
kw = dict(platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL, params={"title": "video 1"}, actor="navigator", idempotency_key="pub-1")
esc = nav.execute_action(**kw).escalation_id; desk.approve(esc, decision="ok", resolved_by=OWNER)
print("=== CENARIO A: servico executa, resposta chega apos o timeout (resultado ambiguo)")
MODE["delay"] = 0.8
r1 = nav.execute_action(**kw, adapter_method="publish", approved_escalation_id=esc)
print(f"tentativa 1: success={r1.success} reason={r1.reason[:70]!r}")
time.sleep(0.9); MODE["delay"] = 0.0
r2 = nav.execute_action(**kw, adapter_method="publish", approved_escalation_id=esc)
print(f"tentativa 2 (retry, mesma aprovacao e idempotency_key): success={r2.success}")
print(f">>> PUBLICACOES EXECUTADAS NO SERVICO: {len(EXECUTED)}  {'<-- DUPLICADA' if len(EXECUTED) > 1 else ''}")
print("=== CENARIO B: HTTP 202 (aceito para processamento, ainda nao executado)")
EXECUTED.clear(); MODE["status"] = 202
esc2 = nav.execute_action(**{**kw, "idempotency_key": "pub-2", "params": {"title": "video 2"}}).escalation_id; desk.approve(esc2, decision="ok", resolved_by=OWNER)
r3 = nav.execute_action(**{**kw, "idempotency_key": "pub-2", "params": {"title": "video 2"}}, adapter_method="publish", approved_escalation_id=esc2)
print(f"HTTP 202 -> success={r3.success}  (aceito foi tratado como concluido)" )
print("=== segredo em auditoria?", any("SEGREDO-XYZ" in json.dumps(e.__dict__, default=str) for e in audit.all()) if hasattr(audit, "all") else "sem all()")
srv.shutdown(); srv.server_close()
