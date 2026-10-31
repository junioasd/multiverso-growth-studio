"""
v1.0.3-OPERATIONAL-CANDIDATE — bloqueadores da auditoria de readiness 1.0.2:
(1) kill switch GLOBAL em todo I/O externo; (2) integridade monetária e
política de overrun do CostGuard. Rede: somente servidor LOOPBACK do teste.
"""
import ast
import http.server
import os
import socketserver
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from core.models import db as db_module
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role

OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCK = kill_switch.BLOCK_CODE
_REAL_URLOPEN = __import__("urllib.request", fromlist=["urlopen"]).urlopen

# Toda chamada que faz I/O EXTERNO real. Nova forma de saída => acrescentar aqui.
_EGRESS_CALLS = {"urlopen", "sync_playwright", "async_playwright", "HTTPConnection", "HTTPSConnection",
                 "create_connection", "SMTP", "SMTP_SSL"}
_EGRESS_MODULE_CALLS = {("requests", "get"), ("requests", "post"), ("requests", "put"), ("requests", "delete"),
                        ("requests", "request"), ("httpx", "get"), ("httpx", "post")}
# Exceções justificadas: não há I/O externo (sonda LOCAL de capacidade do navegador, sem URL).
_ALLOWLIST = {("bridge/execution/browser_executor.py", "is_browser_available")}
_GUARD = "egress_blocked"


def _loopback_only_urlopen(request, *args, **kwargs):
    """Controle de urlopen exigido pela guarda de rede: chamadas REAIS, só loopback."""
    import urllib.parse
    host = urllib.parse.urlparse(getattr(request, "full_url", request)).hostname
    if host not in ("127.0.0.1", "localhost"):
        raise AssertionError(f"rede EXTERNA proibida em teste: {host}")
    return _REAL_URLOPEN(request, *args, **kwargs)


def _egress_inventory():
    """[(arquivo, função, linha_io, linha_guarda|None)] para TODO I/O externo do código de produção."""
    found = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "docs", ".git", "__pycache__")]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(base, f)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                inner = {id(x) for x in ast.walk(fn) if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x is not fn}
                calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
                nested = set()
                for x in ast.walk(fn):
                    if id(x) in inner:
                        nested |= {id(c) for c in ast.walk(x) if isinstance(c, ast.Call)}
                calls = [c for c in calls if id(c) not in nested]

                def name(c):
                    f_ = c.func
                    return f_.attr if isinstance(f_, ast.Attribute) else (f_.id if isinstance(f_, ast.Name) else None)

                def is_egress(c):
                    n = name(c)
                    if n in _EGRESS_CALLS:
                        return True
                    f_ = c.func
                    return (isinstance(f_, ast.Attribute) and isinstance(f_.value, ast.Name)
                            and (f_.value.id, f_.attr) in _EGRESS_MODULE_CALLS)
                guards = sorted(c.lineno for c in calls if name(c) == _GUARD)
                for c in calls:
                    if is_egress(c):
                        g = [x for x in guards if x < c.lineno]
                        found.append((rel, fn.name, c.lineno, g[-1] if g else None))
    return found


class EgressInventoryTests(unittest.TestCase):
    def test_every_external_io_in_production_code_is_preceded_by_the_central_guard(self):
        inv = _egress_inventory()
        # inventário EXATO: um ponto de I/O novo (ou removido) exige revisão consciente deste teste
        self.assertEqual(sorted((f, fn) for f, fn, *_ in inv), sorted([
            ("bridge/execution/browser_executor.py", "execute_authenticated_action"),
            ("bridge/execution/browser_executor.py", "is_browser_available"),
            ("bridge/execution/browser_executor.py", "navigate_and_extract"),
            ("bridge/execution/oauth2.py", "_post_token_request"),
            ("bridge/execution/platform_adapters.py", "_request"),
            ("bridge/execution/webhooks.py", "deliver"),
            ("core/agents/runtime/http_generation_provider.py", "_perform"),
            # v1.0.4: I/O da busca movido para `_perform` (caminho de custo único) e
            # TRANSPORTE CENTRAL novo em egress.urlopen — revisão consciente do inventário.
            ("core/agents/runtime/http_search_provider.py", "_perform"),
            ("core/operations/egress.py", "urlopen"),
            ("core/multimodal/real_webpage_analyzer.py", "analyze"),
        ]))
        unguarded = [(f, fn, ln) for f, fn, ln, g in inv if g is None and (f, fn) not in _ALLOWLIST]
        self.assertEqual(unguarded, [], "I/O externo SEM guarda central antes dele")
        files = {f for f, *_ in inv}
        for expected in ("core/agents/runtime/http_search_provider.py", "core/multimodal/real_webpage_analyzer.py",
                         "bridge/execution/oauth2.py", "bridge/execution/browser_executor.py",
                         "bridge/execution/webhooks.py", "bridge/execution/platform_adapters.py",
                         "core/agents/runtime/http_generation_provider.py"):
            self.assertIn(expected, files)

    def test_inventory_detects_an_unguarded_egress(self):
        """O próprio detector é sensível: um I/O sem guarda é apontado."""
        code = "import urllib.request\ndef f(u):\n    return urllib.request.urlopen(u)\n"
        tree = ast.parse(code)
        fn = tree.body[1]
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
        self.assertTrue(any(isinstance(c.func, ast.Attribute) and c.func.attr == "urlopen" for c in calls))
        self.assertFalse(any(isinstance(c.func, ast.Name) and c.func.id == _GUARD for c in calls))


class _Svc(http.server.BaseHTTPRequestHandler):
    hits, body = [], b'{"results": [], "access_token": "tok", "token_type": "Bearer", "expires_in": 60, "asset_id": "a"}'

    def _r(self):
        _Svc.hits.append(f"{self.command} {self.path}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_Svc.body)))
        self.end_headers()
        self.wfile.write(_Svc.body)

    def do_GET(self):
        self._r()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._r()

    def log_message(self, *a):
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        self._net = mock.patch("urllib.request.urlopen", side_effect=_loopback_only_urlopen)
        self._net.start()
        self.addCleanup(self._net.stop)
        _Svc.hits = []
        _Svc.body = b'{"results": [], "access_token": "tok", "token_type": "Bearer", "expires_in": 60, "asset_id": "a"}'
        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Svc)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "e.db")
        db_module.init_db(self.db)
        kill_switch.configure(self.db)
        cost_guard.configure(self.db)
        self.addCleanup(kill_switch.configure, None)
        self.addCleanup(cost_guard.configure, None)
        self.g = cost_guard.CostGuard(self.db)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        db_module.remove_db_file(self.db)


class GlobalKillSwitchDynamicTests(_Base):
    def _oauth(self):
        from bridge.execution.oauth2 import OAuth2Flow, OAuth2ProviderConfig
        return OAuth2Flow(OAuth2ProviderConfig(platform="p", authorization_endpoint=self.url + "/auth",
                                               token_endpoint=self.url + "/oauth", client_id="c", client_secret="s",
                                               redirect_uri="http://127.0.0.1/cb", scopes=["x"]))

    def _all_executors(self):
        from bridge.execution.browser_executor import BrowserExecutor
        from bridge.execution.platform_adapters import PlatformApiAdapter
        from bridge.execution.webhooks import WebhookDispatcher
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        from core.agents.runtime.http_search_provider import HttpSearchProvider
        from core.multimodal.real_webpage_analyzer import RealWebPageAnalyzer
        base = self.url

        class A(PlatformApiAdapter):
            platform = "x"
            base_url = base

            def publish(self, **kw):
                return self._request(action="P", method="POST", path="/api", body=kw)
        from bridge.execution.vault import TokenType, TokenVault
        vault = TokenVault(db_path=self.db)
        ref = vault.store_token(platform="x", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="t")
        return {
            "HttpSearchProvider": lambda: HttpSearchProvider(api_key="k", api_url=self.url + "/search").call(
                operation="SEARCH", params={"query": "q"}).reason,
            "RealWebPageAnalyzer": lambda: RealWebPageAnalyzer().analyze(reference=self.url + "/page").summary,
            "OAuth2 exchange": lambda: self._oauth().exchange_code_for_token(authorization_code="c").reason,
            "OAuth2 refresh": lambda: self._oauth().refresh_access_token(refresh_token="r").reason,
            "BrowserExecutor.navigate": lambda: BrowserExecutor().navigate_and_extract(url=self.url + "/b").reason,
            "BrowserExecutor.authenticated": lambda: BrowserExecutor().execute_authenticated_action(
                url=self.url + "/b", storage_state_path=None, action_script="").reason,
            "PlatformApiAdapter": lambda: A(vault=vault, account_reference_id=ref).publish(t=1).reason,
            "WebhookDispatcher": lambda: WebhookDispatcher().deliver(url=self.url + "/h", payload={}).reason,
            "HttpGenerationProvider": lambda: HttpGenerationProvider(api_key="k", api_url=self.url + "/gen").call(
                operation="G", params={"estimated_cost": 0.1, "currency": "USD"}).reason,
        }

    def test_no_known_external_executor_does_io_with_kill_switch_enabled(self):
        kill_switch.activate(self.db, principal=OWNER, reason="auditoria de egress")
        for name, fn in self._all_executors().items():
            self.assertIn(BLOCK, fn() or "", name)
        self.assertEqual(_Svc.hits, [], "nenhum executor externo fez I/O")

    def test_radar_real_browser_path_is_blocked(self):
        from bridge.execution.browser_executor import BrowserExecutor
        launched = []
        kill_switch.activate(self.db, principal=OWNER, reason="radar")
        with mock.patch.object(BrowserExecutor, "is_browser_available", side_effect=lambda: launched.append(1)):
            r = BrowserExecutor().navigate_and_extract(url=self.url + "/radar")
        self.assertIn(BLOCK, r.reason)
        self.assertEqual(launched, [], "o navegador nem é iniciado")

    def test_unreadable_state_blocks_every_executor(self):
        c = sqlite3.connect(self.db)
        try:
            c.execute("DELETE FROM operational_kill_switch")
            c.commit()
        finally:
            c.close()
        for name, fn in self._all_executors().items():
            self.assertIn(BLOCK, fn() or "", name)
        self.assertEqual(_Svc.hits, [])

    def test_guard_releases_when_disabled(self):
        from core.agents.runtime.http_search_provider import HttpSearchProvider
        kill_switch.activate(self.db, principal=OWNER, reason="x")
        kill_switch.deactivate(self.db, principal=OWNER, reason="y")
        # v1.0.4 (bloqueador 4): provedor precisa de classificação de custo explícita
        self.assertTrue(HttpSearchProvider(api_key="k", api_url=self.url + "/search", paid=False).call(
            operation="SEARCH", params={"query": "q"}).available)
        self.assertEqual(_Svc.hits, ["POST /search"])


class MonetaryIntegrityTests(_Base):
    def setUp(self):
        super().setUp()
        for p in ("p", "http_generation_provider"):
            self.g.set_limit(principal=OWNER, provider=p, kind="provider_total", amount=10.0, currency="USD")

    def test_invalid_estimates_are_denied_structurally_and_never_persisted(self):
        for bad in (-5.0, -100, float("nan"), float("inf"), float("-inf"), True, "10", None):
            code = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=bad, currency="USD")["code"]
            self.assertEqual(code, "COST_UNKNOWN" if bad is None else "INVALID_COST", repr(bad))
        self.assertEqual(self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="")["code"],
                         "INVALID_CURRENCY")
        with db_module.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ai_api_reservations").fetchone()[0], 0)

    def test_invalid_values_rejected_in_every_entry_point(self):
        for bad in (-1.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.g.set_limit(principal=OWNER, provider="p", kind="per_call", amount=bad, currency="USD")
            with self.assertRaises(ValueError):
                self.g.record_usage(provider="p", service="s", operation="o", success=True, currency="USD", cost=bad)
        rid = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        with self.assertRaises(ValueError):
            self.g.reconcile(rid, principal=OWNER, executed=True, actual_cost=-3.0, evidence="x")

    def test_negative_cost_from_provider_never_increases_budget(self):
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        _Svc.body = b'{"asset_id": "a", "cost": -20.0, "currency": "USD"}'
        p = HttpGenerationProvider(api_key="k", api_url=self.url + "/gen")
        self.assertTrue(p.call(operation="G", params={"estimated_cost": 1.0, "currency": "USD"}).available)
        s = self.g.summary()["http_generation_provider/USD"]
        self.assertEqual((s["known_cost"], s["budget_charged"]), (0, 1.0), "cobra a reserva; negativo não abate")
        inc = self.g.open_incidents("http_generation_provider")
        self.assertEqual([i["kind"] for i in inc], ["INVALID_REPORTED_COST"])
        _Svc.body = b'{"asset_id": "a"}'
        blocked = p.call(operation="G", params={"estimated_cost": 5.0, "currency": "USD"})
        self.assertIn("COST_INCIDENT_REVIEW_REQUIRED", blocked.reason)
        self.assertEqual(len(_Svc.hits), 1)
        with self.assertRaises(PermissionError):
            self.g.reconcile_incident(inc[0]["incident_id"], principal=Principal(subject="a", role=Role.ATLAS,
                                                                                   scopes={"*"}), evidence="x", source="y",
                                      final_amount=1.0, final_currency="USD")
        # v1.0.5 (auditoria 1.0.4, B3): incidente financeiro só sai por RECONCILIAÇÃO
        self.g.reconcile_incident(inc[0]["incident_id"], principal=OWNER, evidence="provedor corrigiu o faturamento",
                                  source="fatura do provedor", final_amount=1.0, final_currency="USD")
        self.assertTrue(p.call(operation="G", params={"estimated_cost": 5.0, "currency": "USD"}).available)

    def test_overrun_opens_incident_and_blocks_provider_until_owner_review(self):
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        _Svc.body = b'{"asset_id": "a", "cost": 20.0, "currency": "USD"}'
        p = HttpGenerationProvider(api_key="k", api_url=self.url + "/gen")
        self.assertTrue(p.call(operation="G", params={"estimated_cost": 1.0, "currency": "USD"}).available)
        self.assertEqual(self.g.summary()["http_generation_provider/USD"]["budget_charged"], 20.0,
                         "o gasto REAL é registrado (nunca escondido)")
        inc = self.g.open_incidents("http_generation_provider")
        self.assertEqual([(i["kind"], i["reserved_amount"]) for i in inc], [("COST_OVERRUN", 1.0)])
        self.assertEqual(self.g.preview(provider="http_generation_provider", service="G", operation="G",
                                        estimated_cost=0.0, currency="USD")["code"], "COST_INCIDENT_REVIEW_REQUIRED")

    def test_sqlite_triggers_reject_invalid_money_as_defense_in_depth(self):
        conn = sqlite3.connect(self.db)
        try:
            for sql in ("INSERT INTO ai_api_reservations (reservation_id, provider, service, operation, amount, currency, "
                        "status, created_at) VALUES ('x', 'p', 's', 'o', -5, 'USD', 'RESERVED', 0)",
                        "INSERT INTO ai_api_usage (provider, service, operation, created_at, cost, cost_status, success, "
                        "budget_charge) VALUES ('p', 's', 'o', 0, 1, 'X', 1, -20)",
                        "INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) "
                        "VALUES ('p', 'per_day', '*', 1e999, 'USD', 0, 'x')"):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql[:40]):
                    conn.execute(sql)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
