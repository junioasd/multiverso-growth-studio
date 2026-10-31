import http.server, os, socketserver, sys, tempfile, threading
sys.path.insert(0, os.getcwd())
from core.models import db as dbm
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
hits = []
class S(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0)); hits.append(self.path)
        out = b'{"asset_id": "a"}'; self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), S); threading.Thread(target=srv.serve_forever, daemon=True).start()
U = f"http://127.0.0.1:{srv.server_address[1]}"
d = tempfile.mkdtemp(); db = os.path.join(d, "a.db"); dbm.init_db(db); kill_switch.configure(db); cost_guard.configure(db)
g = cost_guard.CostGuard(db)
def safe(f):
    try: return f()
    except Exception as e: return f"REJEITADO {type(e).__name__}: {str(e)[:70]}"
print("=== B1: unknown_cost_upper_bound = 0")
g.set_limit(principal=OWNER, provider="http_generation_provider", kind="provider_total", amount=10.0, currency="USD")
print("   set_limit(ub=0):", safe(lambda: g.set_limit(principal=OWNER, provider="http_generation_provider", kind="unknown_cost_upper_bound", amount=0.0, currency="USD")) or "ACEITO")
from core.agents.runtime.http_generation_provider import HttpGenerationProvider
av = [HttpGenerationProvider(api_key="k", api_url=U + "/gen").call(operation="G", params={"estimated_cost": 1.0, "currency": "USD"}).available for _ in range(2)]
print(f"   2 chamadas sem custo: available={av} hits={hits} incidentes={len(g.open_incidents('http_generation_provider'))} ledger={g.summary().get('http_generation_provider/USD')}")
print("=== B2: duas reconciliacoes concorrentes do mesmo incidente")
g.set_limit(principal=OWNER, provider="m", kind="provider_total", amount=1000.0, currency="USD")
rid = g.authorize(provider="m", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
g.settle(rid, outcome="EXECUTED", success=True, actual_cost=100.0, actual_currency="EUR")
iid = g.open_incidents("m")[0]["incident_id"]; res = {}; bar = threading.Barrier(2)
def rec(v):
    bar.wait(); res[v] = safe(lambda: g.reconcile_incident(iid, principal=OWNER, evidence="e", source="s", final_amount=v, final_currency="USD", fx_rate=0.1, fx_source="BCE", fx_timestamp="t"))
ts = [threading.Thread(target=rec, args=(v,)) for v in (10.0, 20.0)]; [t.start() for t in ts]; [t.join() for t in ts]
print("   resultados:", {k: (v if isinstance(v, str) else v.get("status")) for k, v in res.items()}, "| ledger m =", g.summary()["m/USD"]["budget_charged"])
print("=== B3: limite trocado de USD para EUR com historico em USD")
g.set_limit(principal=OWNER, provider="fxp", kind="provider_total", amount=100.0, currency="USD")
r2 = g.authorize(provider="fxp", service="s", operation="o", estimated_cost=50.0, currency="USD")["reservation_id"]
g.settle(r2, outcome="EXECUTED", success=True, actual_cost=50.0, actual_currency="USD", cost_confirmed=True)
print("   set_limit EUR:", safe(lambda: g.set_limit(principal=OWNER, provider="fxp", kind="provider_total", amount=100.0, currency="EUR")) or "ACEITO")
print("   preview EUR 40:", g.preview(provider="fxp", service="s", operation="o", estimated_cost=40.0, currency="EUR")["code"], "| EUR 60:", g.preview(provider="fxp", service="s", operation="o", estimated_cost=60.0, currency="EUR")["code"], "(os 50 USD contados como EUR?)")
print("=== B4: settle contraditorio")
g.set_limit(principal=OWNER, provider="c", kind="provider_total", amount=1000.0, currency="USD")
g.set_limit(principal=OWNER, provider="c", kind="unknown_cost_upper_bound", amount=1.0, currency="USD")
ra = g.authorize(provider="c", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
print("   A NOT_EXECUTED + custo 100:", safe(lambda: g.settle(ra, outcome="NOT_EXECUTED", success=False, actual_cost=100.0, actual_currency="USD", cost_confirmed=True)))
g.set_limit(principal=OWNER, provider="c2", kind="provider_total", amount=1000.0, currency="USD")
g.set_limit(principal=OWNER, provider="c2", kind="unknown_cost_upper_bound", amount=1.0, currency="USD")
rb = g.authorize(provider="c2", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
print("   B UNKNOWN + custo 100:", safe(lambda: g.settle(rb, outcome="UNKNOWN", success=False, actual_cost=100.0, actual_currency="USD", cost_confirmed=True)))
print("   ledger c =", g.summary().get("c/USD", {}).get("budget_charged"), "| incidentes c =", len(g.open_incidents("c")), "| ledger c2 =", g.summary().get("c2/USD", {}).get("budget_charged"), "| incidentes c2 =", len(g.open_incidents("c2")))
srv.shutdown()
