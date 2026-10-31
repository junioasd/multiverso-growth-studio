import http.server, os, socketserver, sys, tempfile, threading, sqlite3
sys.path.insert(0, os.getcwd())
from core.models import db as dbm
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
hits = []; codes = []
class S(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0)); hits.append(1)
        code = codes.pop(0) if codes else 200
        if code == 503 and hook: hook()
        out = b'{"asset_id":"a1"}'; self.send_response(code); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
hook = None
srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), S); threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}"
d = tempfile.mkdtemp(); db = os.path.join(d, "a.db"); dbm.init_db(db); kill_switch.configure(db); cost_guard.configure(db)
g = cost_guard.CostGuard(db)
print("=== 1) provedor pago: limite acumulado USD 1,00; 2 chamadas de 0,60")
g.set_limit(principal=OWNER, provider="generation_http", kind="provider_total", amount=1.0, currency="USD")
from core.agents.runtime.http_generation_provider import HttpGenerationProvider
p = HttpGenerationProvider(api_key="k-local", api_url=url + "/gen")
print("   provider.name =", p.name)
g.set_limit(principal=OWNER, provider=p.name, kind="provider_total", amount=1.0, currency="USD")
for i in range(2):
    r = p.call(operation="GENERATE", params={"estimated_cost": 0.6, "currency": "USD"})
    print(f"   chamada {i+1}: available={r.available} reason={r.reason[:50]!r}")
print(f"   hits HTTP={len(hits)} | consumo registrado={g.summary()}")
print("=== 2) corrida concorrente: limite 10, duas reservas de 6")
g.set_limit(principal=OWNER, provider="conc", kind="provider_total", amount=10.0, currency="USD")
barrier = threading.Barrier(2); res = []
def go():
    a = g.authorize(provider="conc", service="s", operation="o", estimated_cost=6.0, currency="USD"); barrier.wait()
    if a["authorized"]: g.record_usage(provider="conc", service="s", operation="o", success=True, currency="USD", cost=6.0)
    res.append(a["code"])
ts = [threading.Thread(target=go) for _ in range(2)]; [t.start() for t in ts]; [t.join() for t in ts]
print("   autorizacoes:", res, "| gasto final:", g.summary().get("conc/USD"))
print("=== 3) kill switch ativado entre retries de webhook")
hits.clear(); codes[:] = [503, 200]
hook = lambda: kill_switch.activate(db, principal=OWNER, reason="emergencia entre retries")
from bridge.execution.webhooks import RetryPolicy, WebhookDispatcher
r = WebhookDispatcher(retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01), sleep_fn=lambda s: None).deliver(url=url + "/h", payload={})
print(f"   hits={len(hits)} kill_switch={kill_switch.state(db)['state']} resultado success={r.success} delivery={r.delivery} attempts={r.attempts}")
hook = None
print("=== 4) linha do kill switch ausente")
c = sqlite3.connect(db); c.execute("DELETE FROM operational_kill_switch"); c.commit(); c.close()
s = kill_switch.state(db); print(f"   apos remover a linha: state={s['state']} external_execution_allowed={s['external_execution_allowed']}")
d2 = tempfile.mkdtemp(); db2 = os.path.join(d2, "n.db"); dbm.init_db(db2)
print("   banco NOVO tem linha inicial explicita?", sqlite3.connect(db2).execute("SELECT COUNT(*) FROM operational_kill_switch").fetchone()[0] == 1)
srv.shutdown()
