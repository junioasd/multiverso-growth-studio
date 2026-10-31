import http.server, json, math, os, socketserver, sys, tempfile, threading
sys.path.insert(0, os.getcwd())
from core.models import db as dbm
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
hits = []; BODY = {"v": b'{"results": [], "access_token": "tok-teste", "asset_id": "a"}'}
class S(http.server.BaseHTTPRequestHandler):
    def _r(self):
        hits.append(f"{self.command} {self.path}"); out = BODY["v"]
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_GET(self): self._r()
    def do_POST(self): self.rfile.read(int(self.headers.get("Content-Length") or 0)); self._r()
    def log_message(self, *a): pass
srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), S); threading.Thread(target=srv.serve_forever, daemon=True).start()
U = f"http://127.0.0.1:{srv.server_address[1]}"
d = tempfile.mkdtemp(); db = os.path.join(d, "a.db"); dbm.init_db(db); kill_switch.configure(db); cost_guard.configure(db)
print("=== BLOQUEADOR 1: kill switch ENABLED")
kill_switch.activate(db, principal=OWNER, reason="auditoria")
print("   estado:", kill_switch.state(db)["state"])
from core.agents.runtime.http_search_provider import HttpSearchProvider
r = HttpSearchProvider(api_key="k", api_url=U + "/search").call(operation="SEARCH", params={"query": "x"})
print("   HttpSearchProvider available =", r.available)
from core.multimodal.real_webpage_analyzer import RealWebPageAnalyzer
BODY["v"] = b"<html><title>pagina</title></html>"
e = RealWebPageAnalyzer().analyze(reference=U + "/page"); print("   RealWebPageAnalyzer confidence =", e.confidence)
BODY["v"] = b'{"access_token": "tok-teste", "token_type": "Bearer", "expires_in": 3600}'
from bridge.execution.oauth2 import OAuth2Flow, OAuth2ProviderConfig
import inspect
cfg_params = inspect.signature(OAuth2ProviderConfig).parameters
cfg = OAuth2ProviderConfig(**{k: (U + "/oauth" if "token" in k and "url" in k else ("https://x/auth" if "url" in k or "endpoint" in k else ["s"] if "scope" in k else "v")) for k in cfg_params if cfg_params[k].default is inspect._empty})
print("   OAuth2 exchange success =", OAuth2Flow(cfg).exchange_code_for_token(authorization_code="c").success, "| refresh success =", OAuth2Flow(cfg).refresh_access_token(refresh_token="r").success)
print("   hits recebidos pelo servidor:", hits)
src = open("bridge/execution/browser_executor.py").read(); print("   BrowserExecutor consulta kill switch?", "kill_switch" in src or "egress" in src)
kill_switch.deactivate(db, principal=OWNER, reason="fim"); hits.clear()
print("=== BLOQUEADOR 2: integridade monetaria (limite provider_total USD 10)")
g = cost_guard.CostGuard(db)
for prov in ("neg", "http_generation_provider", "nan", "over"):
    g.set_limit(principal=OWNER, provider=prov, kind="provider_total", amount=10.0, currency="USD")
for v in (-5.0, -100.0):
    print(f"   estimated_cost={v}:", g.authorize(provider="neg", service="s", operation="o", estimated_cost=v, currency="USD")["code"])
try: print("   estimated_cost=NaN:", g.authorize(provider="nan", service="s", operation="o", estimated_cost=float("nan"), currency="USD")["code"])
except Exception as ex: print("   estimated_cost=NaN: EXCECAO", type(ex).__name__, str(ex)[:60])
from core.agents.runtime.http_generation_provider import HttpGenerationProvider
BODY["v"] = b'{"asset_id": "a", "cost": -20.0, "currency": "USD"}'
p = HttpGenerationProvider(api_key="k", api_url=U + "/gen")
print("   provider devolve cost=-20: available =", p.call(operation="G", params={"estimated_cost": 1.0, "currency": "USD"}).available, "| ledger:", g.summary().get("http_generation_provider/USD"))
BODY["v"] = b'{"asset_id": "a"}'
print("   depois, chamada de USD 20 com limite 10:", "EXECUTADA" if p.call(operation="G", params={"estimated_cost": 20.0, "currency": "USD"}).available else "bloqueada")
BODY["v"] = b'{"asset_id": "a", "cost": 20.0, "currency": "USD"}'
g2 = HttpGenerationProvider(api_key="k", api_url=U + "/gen"); g2.name = "over"
print("   overrun: reserva 1, custo real 20 ->", g2.call(operation="G", params={"estimated_cost": 1.0, "currency": "USD"}).available, "| ledger:", g.summary().get("over/USD"))
print("   proxima chamada 'over' est=0.01:", g.preview(provider="over", service="G", operation="G", estimated_cost=0.01, currency="USD")["code"])
srv.shutdown()
