import contextlib, http.server, os, socketserver, sys, tempfile, threading, types
sys.path.insert(0, os.getcwd())
from unittest import mock
from core.models import db as dbm
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
hits = []; BODY = {"v": b'{"result": "x"}'}
class S(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0)); hits.append(self.path)
        out = BODY["v"]; self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), S); threading.Thread(target=srv.serve_forever, daemon=True).start()
U = f"http://127.0.0.1:{srv.server_address[1]}"
from core.agents.runtime.http_search_provider import HttpSearchProvider
from core.agents.runtime.http_generation_provider import HttpGenerationProvider
d = tempfile.mkdtemp(); db = os.path.join(d, "app.db")
print("=== B1: entrypoint api/server.py (run_server) sem main.py")
kill_switch.configure(None); cost_guard.configure(None); kill_switch.set_operational_runtime(False)
os.environ["DB_PATH"] = db
from api.server import run_server
try:
    s = run_server(port=0)
except TypeError:
    s = run_server(host="127.0.0.1", port=0)
print("   apos run_server(): kill switch =", kill_switch.state()["state"], "| CostGuard global =", cost_guard.configured_db())
r = HttpSearchProvider(api_key="k", api_url=U + "/search", paid=True).call(operation="S", params={"estimated_cost": 0.1, "currency": "USD"})
print("   busca PAGA sem limite: available =", r.available, "| hits =", hits)
s.server_close(); hits.clear()
dbm.init_db(db); kill_switch.configure(db); cost_guard.configure(db); g = cost_guard.CostGuard(db)
print("=== B2: limite do Owner = USD 0,00; estimativa 0; provedor nao informa custo")
for p in ("http_search_provider", "http_generation_provider"):
    g.set_limit(principal=OWNER, provider=p, kind="provider_total", amount=0.0, currency="USD")
a = HttpSearchProvider(api_key="k", api_url=U + "/search", paid=True).call(operation="S", params={"estimated_cost": 0.0, "currency": "USD"}).available
b = HttpGenerationProvider(api_key="k", api_url=U + "/gen").call(operation="G", params={"estimated_cost": 0.0, "currency": "USD"}).available
print(f"   search={a} generation={b} | hits={hits} | ledger={ {k: v['budget_charged'] for k, v in g.summary().items()} } | incidentes={len(g.open_incidents())}")
print("=== B3: incidente de moeda 'resolvido' so com texto")
g.set_limit(principal=OWNER, provider="m", kind="provider_total", amount=10.0, currency="USD")
rid = g.authorize(provider="m", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
g.settle(rid, outcome="EXECUTED", success=True, actual_cost=100.0, actual_currency="EUR")
inc = g.open_incidents("m")[0]
try:
    g.resolve_incident(inc["incident_id"], principal=OWNER, evidence="ok, revisado"); print("   resolve so com texto: ACEITO")
except Exception as e:
    print("   resolve so com texto: RECUSADO", type(e).__name__)
print("   apos resolve: ledger m =", g.summary().get("m/USD"), "| proxima reserva USD 1:", g.authorize(provider="m", service="s", operation="o", estimated_cost=1.0, currency="USD")["code"])
print("=== B4: settle repetido com conteudo divergente")
g.set_limit(principal=OWNER, provider="r", kind="provider_total", amount=1000.0, currency="USD")
rid = g.authorize(provider="r", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
print("   1a:", g.settle(rid, outcome="EXECUTED", success=True, actual_cost=1.0, actual_currency="USD"))
try: print("   2a (100):", g.settle(rid, outcome="EXECUTED", success=True, actual_cost=100.0, actual_currency="USD"))
except Exception as e: print("   2a (100): REJEITADA", type(e).__name__)
print("   ledger r =", g.summary().get("r/USD", {}).get("budget_charged"), "| incidentes r =", len(g.open_incidents("r")))
print("=== OBS: navegador — trava ativada durante page.goto")
from bridge.execution.browser_executor import BrowserExecutor
calls = []
class Page:
    def goto(self, url, **kw): calls.append("goto"); kill_switch.activate(db, principal=OWNER, reason="durante goto")
    def evaluate(self, script): calls.append("evaluate")
class Br:
    def new_page(self): return Page()
    def new_context(self, **kw): return self
    def close(self): pass
@contextlib.contextmanager
def sp(): yield types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **k: Br()))
fake = types.ModuleType("playwright.sync_api"); fake.sync_playwright = sp; fake.Error = Exception
kill_switch.deactivate(db, principal=OWNER, reason="reset") if kill_switch.state(db)["state"] == "ENABLED" else None
with mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": fake}), mock.patch.object(BrowserExecutor, "is_browser_available", return_value=(True, "")):
    res = BrowserExecutor().execute_authenticated_action(url=U + "/x", storage_state_path=None, action_script="1")
print("   chamadas:", calls, "| estado:", kill_switch.state(db)["state"], "| success =", res.success)
srv.shutdown()
