import http.server, json, os, socketserver, subprocess, sys, tempfile, threading, time
sys.path.insert(0, '.')
from atlas.escalation import EscalationDesk
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.execution.vault import TokenType, TokenVault
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.models import db as dbm
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role
EXEC = []
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); EXEC.append(self.rfile.read(n)); time.sleep(0.5)
        out = b'{"id":"p"}'
        try: self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        except Exception: pass
    def log_message(self, *a): pass
srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start(); port = srv.server_address[1]
fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd); dbm.remove_db_file(db); dbm.init_db(db)
desk = EscalationDesk(AuditTrail(), db_path=db); vault = TokenVault(db_path=db)
ref = vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
hub = BridgeHub(); hub.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True)
class A(PlatformApiAdapter):
    platform = "youtube"; base_url = f"http://127.0.0.1:{port}"
    def publish(self, **kw): return self._request(action="PUBLISH", method="POST", path="/publish", body=kw)
nav = NavigatorReal(bridge_hub=hub, escalation_desk=desk, adapters_by_platform={"youtube": A(vault=vault, account_reference_id=ref)}, db_path=db, audit=AuditTrail())
kw = dict(platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL, params={"title": "v"}, actor="navigator", idempotency_key="pub-crash")
esc = nav.execute_action(**kw).escalation_id; desk.approve(esc, decision="ok", resolved_by=Principal(subject="o", role=Role.OWNER, scopes={"*"}))
rc = subprocess.run([sys.executable, "/tmp/crash_worker.py", db, str(port), esc, ref], timeout=30).returncode
print(f"worker morreu (rc={rc}); publicacoes executadas no servico: {len(EXEC)}")
with dbm.connect(db) as c: print("claim apos o crash:", dict(c.execute("SELECT status FROM navigator_execution_claims").fetchone()))
with dbm.connect(db) as c: c.execute("UPDATE navigator_execution_claims SET claimed_at = claimed_at - 200")  # passagem do tempo (mesmo recurso do teste existente)
nav2 = NavigatorReal(bridge_hub=hub, escalation_desk=EscalationDesk(AuditTrail(), db_path=db), adapters_by_platform={"youtube": A(vault=TokenVault(db_path=db), account_reference_id=ref)}, db_path=db, audit=AuditTrail())
r = nav2.execute_action(**kw, adapter_method="publish", approved_escalation_id=esc)
time.sleep(0.6)
print(f"apos restart, retomada: success={r.success}; publicacoes executadas no servico: {len(EXEC)} {'<-- DUPLICADA' if len(EXEC)>1 else ''}")
srv.shutdown(); srv.server_close(); dbm.remove_db_file(db)
