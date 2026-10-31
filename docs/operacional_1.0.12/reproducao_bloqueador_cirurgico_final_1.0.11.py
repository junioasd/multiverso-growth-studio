import http.server, json, os, socketserver, sqlite3, sys, tempfile, threading
sys.path.insert(0, os.getcwd())
from core.models import db as dbm
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
d = tempfile.mkdtemp(); db = os.path.join(d, "a.db"); dbm.init_db(db); kill_switch.configure(db); cost_guard.configure(db)
g = cost_guard.CostGuard(db)
def sql(q, p=()):
    c = sqlite3.connect(db); out = c.execute(q, p).fetchall(); c.close(); return out
class Resp:
    def __init__(self, ok, reason=""): self.available, self.reason = ok, reason
print("=== 1) run_metered_call: mesmo correlation_id, PROMPT A e depois PROMPT B")
g.set_limit(principal=OWNER, provider="paid", kind="provider_total", amount=100.0, currency="USD")
calls = []
def perform(prompt):
    return lambda: (calls.append(prompt) or (Resp(True), "EXECUTED", 1.0, "USD"))
for prompt in ("PROMPT A", "PROMPT B"):
    r = cost_guard.run_metered_call(provider="paid", classification="PAID", operation="gen", params={"estimated_cost": 1.0, "currency": "USD", "correlation_id": "cid-1", "prompt": prompt}, perform=perform(prompt), blocked=lambda why: Resp(False, why))
    print(f"   {prompt}: available={r.available} | {(r.reason or '')[:45]}")
print("   executados:", calls, "| fingerprint persistido:", sql("SELECT correlation_fingerprint FROM ai_api_reservations WHERE correlation_id='cid-1'")[0][0][:110])
print("=== 2) HttpGenerationProvider -> socket loopback: prompt A e depois prompt B com o mesmo correlation_id")
bodies = []
class S(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))["params"].get("prompt"))
        out = b'{"asset_id": "a", "cost": 1.0, "currency": "USD"}'; self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), S); threading.Thread(target=srv.serve_forever, daemon=True).start()
from core.agents.runtime.http_generation_provider import HttpGenerationProvider
g.set_limit(principal=OWNER, provider="http_generation_provider", kind="provider_total", amount=100.0, currency="USD")
p = HttpGenerationProvider(api_key="k", api_url=f"http://127.0.0.1:{srv.server_address[1]}/gen")
for prompt in ("A", "B"):
    r = p.call(operation="G", params={"estimated_cost": 1.0, "currency": "USD", "correlation_id": "same-id", "prompt": prompt})
    print(f"   prompt {prompt}: available={r.available} | {(r.reason or '')[:45]}")
print("   requisicoes recebidas pelo servidor (prompts):", bodies)
srv.shutdown(); srv.server_close()
