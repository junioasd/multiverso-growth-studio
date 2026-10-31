import http.server, os, socketserver, sys, tempfile, threading, urllib.request
sys.path.insert(0, os.getcwd())
from core.models import db as dbm
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
hits = []
class S(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0)); hits.append(self.path)
        out = b'{"results": []}'; self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), S); threading.Thread(target=srv.serve_forever, daemon=True).start()
U = f"http://127.0.0.1:{srv.server_address[1]}"
from core.agents.runtime.http_search_provider import HttpSearchProvider as _HSP
def HttpSearchProvider(paid="__omit__", **kw):
    """Adaptativo: a 1.0.3 não tem `paid`; na 1.0.4 o cenário B2 precisa de um
    provedor CLASSIFICADO (gratuito) para chegar de fato ao transporte."""
    if paid == "__omit__":
        return _HSP(**kw)
    try:
        return _HSP(paid=paid, **kw)
    except TypeError:
        return _HSP(**kw)
print("=== B1: kill switch NAO configurado")
kill_switch.configure(None); cost_guard.configure(None)
r = HttpSearchProvider(api_key="k", api_url=U + "/search1").call(operation="SEARCH", params={"query": "q"})
print("   estado:", kill_switch.state()["state"], "| available =", r.available, "| hits =", hits)
os.environ["APP_ENV"] = "production"
r = HttpSearchProvider(api_key="k", api_url=U + "/search1p").call(operation="SEARCH", params={"query": "q"})
print("   com APP_ENV=production: available =", r.available, "| hits =", hits); os.environ.pop("APP_ENV")
d = tempfile.mkdtemp(); db = os.path.join(d, "a.db"); dbm.init_db(db); kill_switch.configure(db); cost_guard.configure(db)
print("=== B2: kill switch ativado DEPOIS da pre-checagem, ANTES do transporte")
hits.clear(); orig = urllib.request.Request.__init__
def arm(self, *a, **k):
    orig(self, *a, **k)
    if kill_switch.state(db)["state"] != "ENABLED": kill_switch.activate(db, principal=OWNER, reason="entre checagem e transporte")
urllib.request.Request.__init__ = arm
r = HttpSearchProvider(paid=False, api_key="k", api_url=U + "/search2").call(operation="SEARCH", params={"query": "q"})
urllib.request.Request.__init__ = orig
print("   estado:", kill_switch.state(db)["state"], "| available =", r.available, "| hits =", hits)
kill_switch.deactivate(db, principal=OWNER, reason="fim")
print("=== B3: reserva USD 1, custo real EUR 100")
g = cost_guard.CostGuard(db); g.set_limit(principal=OWNER, provider="p", kind="provider_total", amount=10.0, currency="USD")
rid = g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
out = g.settle(rid, outcome="EXECUTED", success=True, actual_cost=100.0, actual_currency="EUR")
print("   settle:", out, "| incidentes abertos:", len(g.open_incidents("p")), "| proxima reserva USD 1:", g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["code"])
print("=== B4: HttpSearchProvider com CostGuard configurado e SEM limite")
hits.clear()
r = HttpSearchProvider(api_key="k", api_url=U + "/search4").call(operation="SEARCH", params={"query": "q", "estimated_cost": 0.5, "currency": "USD"})
print("   sem classificacao: available =", r.available, "| motivo:", (r.reason or "")[:40], "| hits =", hits, "| ledger:", {k: v for k, v in g.summary().items() if "search" in k} or "vazio")
r = HttpSearchProvider(paid=True, api_key="k", api_url=U + "/search4b").call(operation="SEARCH", params={"query": "q", "estimated_cost": 0.5, "currency": "USD"})
print("   classificado como PAGO, sem limite: available =", r.available, "| motivo:", (r.reason or "")[:40], "| hits =", hits)
srv.shutdown()
