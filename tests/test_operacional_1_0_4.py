"""
v1.0.4-OPERATIONAL-CANDIDATE — bloqueadores da auditoria de readiness 1.0.3:
(1) kill switch não configurado falha fechado em runtime operacional/produção;
(2) última checagem NO transporte (ativação entre pré-checagem e envio);
(3) divergência de moeda => incidente; (4) classificação de custo obrigatória.
Rede: somente o servidor LOOPBACK do próprio teste.
"""
import ast
import contextlib
import http.server
import os
import socketserver
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from unittest import mock

from core.models import db as db_module
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role

OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCK = kill_switch.BLOCK_CODE
_REAL_URLOPEN = urllib.request.urlopen


def _loopback_only_urlopen(request, *args, **kwargs):
    """Controle de urlopen exigido pela guarda de rede: chamadas REAIS, só loopback."""
    import urllib.parse
    host = urllib.parse.urlparse(getattr(request, "full_url", request)).hostname
    if host not in ("127.0.0.1", "localhost"):
        raise AssertionError(f"rede EXTERNA proibida em teste: {host}")
    return _REAL_URLOPEN(request, *args, **kwargs)


class _Svc(http.server.BaseHTTPRequestHandler):
    hits = []
    body = b'{"results": [], "access_token": "t", "token_type": "Bearer", "expires_in": 60, "asset_id": "a"}'

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
    configure = True

    def setUp(self):
        self._net = mock.patch("urllib.request.urlopen", side_effect=_loopback_only_urlopen)
        self._net.start()
        self.addCleanup(self._net.stop)
        _Svc.hits = []
        _Svc.body = b'{"results": [], "access_token": "t", "token_type": "Bearer", "expires_in": 60, "asset_id": "a"}'
        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Svc)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "k.db")
        db_module.init_db(self.db)
        self.addCleanup(kill_switch.configure, None)
        self.addCleanup(cost_guard.configure, None)
        self.addCleanup(kill_switch.set_operational_runtime, False)
        if self.configure:
            kill_switch.configure(self.db)
            cost_guard.configure(self.db)
        self.g = cost_guard.CostGuard(self.db)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        db_module.remove_db_file(self.db)

    def search(self, paid=False):
        from core.agents.runtime.http_search_provider import HttpSearchProvider
        return HttpSearchProvider(api_key="k", api_url=self.url + "/search", paid=paid)

    def oauth(self):
        from bridge.execution.oauth2 import OAuth2Flow, OAuth2ProviderConfig
        return OAuth2Flow(OAuth2ProviderConfig(platform="p", authorization_endpoint=self.url + "/auth",
                                               token_endpoint=self.url + "/oauth", client_id="c", client_secret="s",
                                               redirect_uri="http://127.0.0.1/cb", scopes=["x"]))


# --------------------------------------------- 1–5 fail-closed do estado
class FailClosedStateTests(_Base):
    configure = False

    def test_1_not_configured_blocks_in_production_and_operational_runtime(self):
        from core.multimodal.real_webpage_analyzer import RealWebPageAnalyzer
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}):
            self.assertEqual(kill_switch.state()["state"], "KILL_SWITCH_NOT_CONFIGURED")
            self.assertIn(BLOCK, self.search().call(operation="S", params={}).reason)
            self.assertEqual(RealWebPageAnalyzer().analyze(reference=self.url + "/p").confidence, 0.0)
        kill_switch.set_operational_runtime(True)                     # processo `main.py` operacional
        self.assertFalse(kill_switch.external_execution_allowed())
        self.assertIn(BLOCK, self.search().call(operation="S", params={}).reason)
        self.assertEqual(_Svc.hits, [])
        kill_switch.set_operational_runtime(False)
        self.assertEqual(kill_switch.state()["state"], "NOT_CONFIGURED_LIBRARY_MODE", "modo permissivo é EXPLÍCITO")

    def test_2_3_4_5_unavailable_missing_invalid_and_read_error_block(self):
        kill_switch.configure(self.db)
        c = sqlite3.connect(self.db)
        try:
            c.execute("UPDATE operational_kill_switch SET state = 'QUALQUER' WHERE id = 1")
            c.commit()
            self.assertFalse(kill_switch.external_execution_allowed(), "estado inválido")
            c.execute("DELETE FROM operational_kill_switch")
            c.commit()
            self.assertFalse(kill_switch.external_execution_allowed(), "estado ausente")
        finally:
            c.close()
        self.assertFalse(kill_switch.external_execution_allowed(os.path.join(self.tmp, "nao_existe.db")),
                         "banco inacessível")
        with mock.patch.object(db_module, "connect", side_effect=sqlite3.OperationalError("disk I/O error")):
            self.assertFalse(kill_switch.external_execution_allowed(self.db), "erro de leitura")
        self.assertIn(BLOCK, self.search().call(operation="S", params={}).reason)
        self.assertEqual(_Svc.hits, [])


# ------------------------------------ 6–14 ativação entre checagem e transporte
@contextlib.contextmanager
def _arm_on_request_construction(db):
    """Ativa o kill switch DEPOIS da pré-checagem, durante a construção da
    requisição — imediatamente antes do transporte (cenário da auditoria)."""
    orig = urllib.request.Request.__init__

    def arm(self, *a, **k):
        orig(self, *a, **k)
        if kill_switch.state(db)["state"] != "ENABLED":
            kill_switch.activate(db, principal=OWNER, reason="entre checagem e transporte")
    with mock.patch.object(urllib.request.Request, "__init__", arm):
        yield


class TransportBoundaryTests(_Base):
    def _executors(self):
        from bridge.execution.platform_adapters import PlatformApiAdapter
        from bridge.execution.vault import TokenType, TokenVault
        from bridge.execution.webhooks import RetryPolicy, WebhookDispatcher
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        from core.multimodal.real_webpage_analyzer import RealWebPageAnalyzer
        base = self.url

        class A(PlatformApiAdapter):
            platform = "x"
            base_url = base

            def publish(self, **kw):
                return self._request(action="P", method="POST", path="/api", body=kw)
        vault = TokenVault(db_path=self.db)
        ref = vault.store_token(platform="x", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="t")
        self.g.set_limit(principal=OWNER, provider="http_generation_provider", kind="provider_total", amount=10.0,
                         currency="USD")
        return {
            "7 HttpSearchProvider": lambda: self.search().call(operation="S", params={}).reason,
            "8 RealWebPageAnalyzer": lambda: RealWebPageAnalyzer().analyze(reference=self.url + "/page").summary,
            "9 OAuth2 exchange": lambda: self.oauth().exchange_code_for_token(authorization_code="c").reason,
            "10 OAuth2 refresh": lambda: self.oauth().refresh_access_token(refresh_token="r").reason,
            "11 WebhookDispatcher": lambda: WebhookDispatcher(retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01),
                                                              sleep_fn=lambda s: None).deliver(url=self.url + "/h", payload={}).reason,
            "PlatformApiAdapter": lambda: A(vault=vault, account_reference_id=ref).publish(t=1).reason,
            "13 HttpGenerationProvider": lambda: HttpGenerationProvider(api_key="k", api_url=self.url + "/gen").call(
                operation="G", params={"estimated_cost": 0.1, "currency": "USD"}).reason,
        }

    def test_6_to_13_activation_between_precheck_and_transport_sends_nothing(self):
        for name, fn in self._executors().items():
            kill_switch.deactivate(self.db, principal=OWNER, reason=f"preparar {name}")
            with _arm_on_request_construction(self.db):
                reason = fn() or ""
            self.assertEqual(kill_switch.state(self.db)["state"], "ENABLED", name)
            self.assertIn(BLOCK, reason, name)
        self.assertEqual(_Svc.hits, [], "nenhuma requisição saiu em nenhum executor")
        gen = [r for r in self.g.open_reservations()]
        self.assertEqual(gen, [], "geração bloqueada no transporte liberou a reserva (NOT_EXECUTED)")

    def test_12_browser_radar_never_navigates_after_activation_at_launch(self):
        from bridge.execution.browser_executor import BrowserExecutor
        visits, db = [], self.db

        class Page:
            def goto(self, url, **kw):
                visits.append(url)

        class Browser:
            def new_page(self):
                return Page()

            def new_context(self, **kw):
                return self

            def close(self):
                pass

        class Chromium:
            def launch(self, **kw):              # a trava é ativada ao ABRIR o navegador
                kill_switch.activate(db, principal=OWNER, reason="ativado no launch")
                return Browser()

        @contextlib.contextmanager
        def sync_playwright():
            yield types.SimpleNamespace(chromium=Chromium())
        fake = types.ModuleType("playwright.sync_api")
        fake.sync_playwright = sync_playwright
        fake.Error = Exception
        with mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": fake}), \
                mock.patch.object(BrowserExecutor, "is_browser_available", return_value=(True, "")):
            r = BrowserExecutor().navigate_and_extract(url=self.url + "/radar")
            self.assertIn(BLOCK, r.reason)
            kill_switch.deactivate(db, principal=OWNER, reason="reset")
            r2 = BrowserExecutor().execute_authenticated_action(url=self.url + "/auth", storage_state_path=None,
                                                                action_script="")
            self.assertIn(BLOCK, r2.reason)
        self.assertEqual(visits, [], "nenhuma navegação iniciada")

    def test_14_all_known_external_io_goes_through_the_central_transport_and_has_cost_policy(self):
        raw, modules = [], set()
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in ("tests", "docs", ".git", "__pycache__")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(base, f)
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                for c in ast.walk(tree):
                    if not isinstance(c, ast.Call) or not isinstance(c.func, ast.Attribute):
                        continue
                    if c.func.attr == "urlopen":
                        modules.add(rel)
                        v = c.func.value
                        if isinstance(v, ast.Attribute) and v.attr == "request":     # urllib.request.urlopen direto
                            raw.append(rel)
                    if c.func.attr in ("goto", "launch") or (isinstance(c.func, ast.Name) and c.func.id == "sync_playwright"):
                        modules.add(rel)
        self.assertEqual(raw, ["core/operations/egress.py"], "urllib.request.urlopen só no transporte central")
        missing = sorted(m for m in modules if m not in cost_guard.PROVIDER_COST_POLICY)
        self.assertEqual(missing, [], "todo módulo com I/O externo tem classificação de custo explícita")


# ------------------------------------------------ 15–18 moeda
class CurrencyTests(_Base):
    def setUp(self):
        super().setUp()
        self.g.set_limit(principal=OWNER, provider="p", kind="provider_total", amount=10.0, currency="USD")

    def _rid(self):
        return self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]

    def test_15_same_currency_settles_normally(self):
        out = self.g.settle(self._rid(), outcome="EXECUTED", success=True, actual_cost=0.8, actual_currency="USD",
                            cost_confirmed=True)
        self.assertEqual((out["cost_status"], out["budget_charge"]), ("CONFIRMED", 0.8))
        self.assertEqual(self.g.open_incidents("p"), [])

    def test_16_17_18_divergent_or_missing_currency_opens_incident_blocks_and_never_converts(self):
        out = self.g.settle(self._rid(), outcome="EXECUTED", success=True, actual_cost=100.0, actual_currency="EUR")
        self.assertEqual((out["cost_status"], out["budget_charge"]), ("CURRENCY_MISMATCH", 1.0),
                         "sem conversão: cobra a reserva em USD, custo real não é convertido")
        with db_module.connect(self.db) as conn:
            self.assertIsNone(conn.execute("SELECT cost FROM ai_api_usage").fetchone()[0])
        self.assertEqual([i["kind"] for i in self.g.open_incidents("p")], ["CURRENCY_MISMATCH_INCIDENT"])
        self.assertEqual(self.g.preview(provider="p", service="s", operation="o", estimated_cost=1.0,
                                        currency="USD")["code"], "COST_INCIDENT_REVIEW_REQUIRED")
        # v1.0.5 (auditoria 1.0.4, B3): encerrar exige reconciliação com fonte FX completa
        self.g.reconcile_incident(self.g.open_incidents("p")[0]["incident_id"], principal=OWNER, evidence="revisado",
                                  source="fatura EUR", final_amount=108.0, final_currency="USD", fx_rate=1.08,
                                  fx_source="BCE", fx_timestamp="2026-09-25T12:00:00Z")
        # a reconciliação tem EFEITO financeiro real: USD 108 reconhecidos > limite USD 10
        self.assertEqual(self.g.preview(provider="p", service="s", operation="o", estimated_cost=1.0,
                                        currency="USD")["code"], "LIMIT_EXCEEDED")
        self.assertEqual(self.g.summary()["p/USD"]["budget_charged"], 108.0)
        self.g.set_limit(principal=OWNER, provider="p", kind="provider_total", amount=1000.0, currency="USD")
        out2 = self.g.settle(self._rid(), outcome="EXECUTED", success=True, actual_cost=0.5, actual_currency=None)
        self.assertEqual(out2["cost_status"], "CURRENCY_MISMATCH", "custo sem moeda também é incidente")


# ---------------------------------------------- 19–22 classificação de custo
class ProviderCostClassificationTests(_Base):
    def test_19_paid_search_without_limit_is_blocked(self):
        r = self.search(paid=True).call(operation="S", params={"estimated_cost": 0.2, "currency": "USD"})
        self.assertIn("PAID_PROVIDER_NOT_AUTHORIZED", r.reason)
        self.assertEqual(_Svc.hits, [])

    def test_20_paid_search_with_limit_reserves_and_settles(self):
        self.g.set_limit(principal=OWNER, provider="http_search_provider", kind="provider_total", amount=1.0, currency="USD")
        _Svc.body = b'{"result": "x", "cost": 0.05, "currency": "USD"}'
        self.assertTrue(self.search(paid=True).call(operation="S", params={"estimated_cost": 0.2, "currency": "USD"}).available)
        s = self.g.summary()["http_search_provider/USD"]
        self.assertEqual((s["known_cost"], s["budget_charged"]), (0.05, 0.05))
        self.assertEqual(self.g.open_reservations(), [])

    def test_21_explicitly_free_search_works_without_budget_and_is_audited(self):
        self.assertTrue(self.search(paid=False).call(operation="S", params={}).available)
        with db_module.connect(self.db) as conn:
            row = dict(conn.execute("SELECT cost_status, budget_charge FROM ai_api_usage").fetchone())
        self.assertEqual(row, {"cost_status": "FREE", "budget_charge": 0.0})

    def test_22_unclassified_provider_is_blocked_and_env_classification_works(self):
        with mock.patch.dict(os.environ, {"MGS_SEARCH_PROVIDER_PAID": ""}):
            self.assertIn("PAYMENT_CLASSIFICATION_REQUIRED", self.search(paid=None).call(operation="S", params={}).reason)
        with mock.patch.dict(os.environ, {"MGS_SEARCH_PROVIDER_PAID": "false"}):
            self.assertTrue(self.search(paid=None).call(operation="S", params={}).available)
        self.assertEqual(len(_Svc.hits), 1)

    def test_cost_guard_missing_in_production_blocks_paid_call(self):
        cost_guard.configure(None)
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}):
            self.assertIn("COST_GUARD_NOT_CONFIGURED", self.search(paid=True).call(
                operation="S", params={"estimated_cost": 0.1, "currency": "USD"}).reason)
        self.assertEqual(_Svc.hits, [])


# ------------------------------------------ 23–28 endurecimento do CostGuard
class CostGuardHardeningTests(_Base):
    def setUp(self):
        super().setUp()
        self.g.set_limit(principal=OWNER, provider="p", kind="provider_total", amount=10.0, currency="USD")

    def test_23_24_25_and_extremes_and_invalid_currency(self):
        for bad in (-1.0, float("nan"), float("inf"), 1e13, True, "5"):
            self.assertEqual(self.g.authorize(provider="p", service="s", operation="o", estimated_cost=bad,
                                              currency="USD")["code"], "INVALID_COST", repr(bad))
        for cur in ("", "usd", "US", "EURO", None, 840):
            self.assertEqual(self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0,
                                              currency=cur)["code"], "INVALID_CURRENCY", repr(cur))
        with self.assertRaises(ValueError):
            self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=1.0, currency=None)
        with db_module.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ai_api_reservations").fetchone()[0], 0)

    def test_26_concurrency_still_never_exceeds_limit(self):
        codes, barrier = [], threading.Barrier(8)

        def go():
            barrier.wait()
            codes.append(self.g.authorize(provider="p", service="s", operation="o", estimated_cost=6.0, currency="USD")["code"])
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(codes.count("AUTHORIZED"), 1)

    def test_27_overrun_still_opens_incident(self):
        rid = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=5.0, actual_currency="USD")
        self.assertEqual([i["kind"] for i in self.g.open_incidents("p")], ["COST_OVERRUN"])

    def test_28_settle_failure_never_frees_budget_and_blocks_provider(self):
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        self.g.set_limit(principal=OWNER, provider="http_generation_provider", kind="provider_total", amount=1.0,
                         currency="USD")
        with mock.patch.object(cost_guard.CostGuard, "settle", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertTrue(HttpGenerationProvider(api_key="k", api_url=self.url + "/gen").call(
                operation="G", params={"estimated_cost": 0.6, "currency": "USD"}).available)
        self.assertEqual([r["status"] for r in self.g.open_reservations()], ["RESERVED"], "reserva segue contando")
        self.assertEqual([i["kind"] for i in self.g.open_incidents("http_generation_provider")], ["SETTLEMENT_FAILED"])
        blocked = HttpGenerationProvider(api_key="k", api_url=self.url + "/gen").call(
            operation="G", params={"estimated_cost": 0.1, "currency": "USD"})
        self.assertIn("COST_INCIDENT_REVIEW_REQUIRED", blocked.reason)
        self.assertEqual(len(_Svc.hits), 1)
        with mock.patch.object(cost_guard.CostGuard, "reserve", side_effect=sqlite3.OperationalError("disk I/O error")):
            self.assertIn("COST_GUARD_UNAVAILABLE", HttpGenerationProvider(api_key="k", api_url=self.url + "/gen").call(
                operation="G", params={"estimated_cost": 0.1, "currency": "USD"}).reason)
        self.assertEqual(len(_Svc.hits), 1, "falha da guarda nunca libera I/O")


if __name__ == "__main__":
    unittest.main()
