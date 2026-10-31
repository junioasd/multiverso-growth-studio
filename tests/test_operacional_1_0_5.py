"""
v1.0.5-OPERATIONAL-CANDIDATE — bloqueadores da auditoria de readiness 1.0.4:
(1) entrypoint api/server.py sem camada operacional; (2) estimativa zero /
custo desconhecido sem teto confiável; (3) incidente financeiro encerrado sem
reconciliação; (4) replay divergente de settle(); + checagem antes de
page.evaluate(). Rede: somente loopback do teste.
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
from core.operations.cost_guard import SettlementConflict
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
    hits, body = [], b'{"result": "x", "asset_id": "a"}'

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Svc.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", str(len(_Svc.body)))
        self.end_headers()
        self.wfile.write(_Svc.body)

    def log_message(self, *a):
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        self._net = mock.patch("urllib.request.urlopen", side_effect=_loopback_only_urlopen)
        self._net.start()
        self.addCleanup(self._net.stop)
        _Svc.hits, _Svc.body = [], b'{"result": "x", "asset_id": "a"}'
        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Svc)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "c.db")
        db_module.init_db(self.db)
        kill_switch.configure(self.db)
        cost_guard.configure(self.db)
        self.addCleanup(kill_switch.configure, None)
        self.addCleanup(cost_guard.configure, None)
        self.addCleanup(kill_switch.set_operational_runtime, False)
        self.g = cost_guard.CostGuard(self.db)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        db_module.remove_db_file(self.db)

    def lim(self, provider, amount, kind="provider_total"):
        self.g.set_limit(principal=OWNER, provider=provider, kind=kind, amount=amount, currency="USD")

    def search(self):
        from core.agents.runtime.http_search_provider import HttpSearchProvider
        return HttpSearchProvider(api_key="k", api_url=self.url + "/search", paid=True)

    def gen(self):
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        return HttpGenerationProvider(api_key="k", api_url=self.url + "/gen")


# ------------------------------------------------------------- B1 entrypoints
class EntrypointTests(_Base):
    def test_run_server_activates_operational_layer(self):
        from api.bootstrap import build_app
        from api.server import run_server
        from tests._auth_test_helpers import fast_hash_password
        kill_switch.configure(None)
        cost_guard.configure(None)
        app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"),
                        db_path=self.db, init_database=True)
        with mock.patch("api.server.build_app", return_value=app):      # entrypoint que CONSTRÓI o próprio app
            srv = run_server(host="127.0.0.1", port=0)
        self.addCleanup(srv.server_close)
        self.assertEqual(kill_switch.state()["state"], "DISABLED", "estado real do banco, não modo biblioteca")
        self.assertEqual(cost_guard.configured_db(), self.db)
        kill_switch.configure(None)                        # mesmo perdendo a configuração depois:
        self.assertEqual(kill_switch.state()["state"], "KILL_SWITCH_NOT_CONFIGURED", "processo é operacional")
        kill_switch.configure(self.db)
        r = self.search().call(operation="S", params={"estimated_cost": 0.1, "currency": "USD"})
        self.assertIn("PAID_PROVIDER_NOT_AUTHORIZED", r.reason)
        self.assertEqual(_Svc.hits, [])

    def test_embedding_with_own_app_does_not_mutate_process_state(self):
        from api.bootstrap import build_app
        from api.server import run_server
        from tests._auth_test_helpers import fast_hash_password
        kill_switch.configure(None)
        app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"),
                        db_path=self.db, init_database=True)
        srv = run_server(host="127.0.0.1", port=0, app=app)
        self.addCleanup(srv.server_close)
        self.assertEqual(kill_switch.state()["state"], "NOT_CONFIGURED_LIBRARY_MODE",
                         "embutir o servidor não vaza estado operacional global para o processo")

    def test_every_production_entrypoint_configures_the_operational_layer(self):
        entry = {}
        for rel in ("main.py", "api/server.py"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                entry[rel] = ast.parse(fh.read())
        mains = []
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in ("tests", "docs", ".git", "__pycache__")]
            for f in files:
                if f.endswith(".py"):
                    with open(os.path.join(base, f), encoding="utf-8") as fh:
                        if 'if __name__ == "__main__"' in fh.read():
                            mains.append(os.path.relpath(os.path.join(base, f), ROOT).replace(os.sep, "/"))
        self.assertEqual(sorted(mains), ["api/server.py", "main.py"], "entrypoint novo exige revisão consciente")

        def calls(fn):
            return {c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", None)
                    for c in ast.walk(fn) if isinstance(c, ast.Call)}
        run_server = next(n for n in ast.walk(entry["api/server.py"]) if isinstance(n, ast.FunctionDef) and n.name == "run_server")
        self.assertIn("configure_operational", calls(run_server))
        for fn in [n for n in entry["main.py"].body if isinstance(n, ast.FunctionDef) and n.name.startswith("cmd_")]:
            c = calls(fn)
            if "build_app" in c:
                self.assertTrue({"_configure_operational", "configure_operational", "run_server"} & c,
                                f"main.py::{fn.name} constrói app sem configurar a camada operacional")


# ----------------------------------------------- B2 estimativa zero / teto
class UnknownCostUpperBoundTests(_Base):
    def test_zero_limit_and_zero_estimate_never_execute_paid_providers(self):
        for p in ("http_search_provider", "http_generation_provider"):
            self.lim(p, 0.0)
        for call in (lambda: self.search().call(operation="S", params={"estimated_cost": 0.0, "currency": "USD"}),
                     lambda: self.gen().call(operation="G", params={"estimated_cost": 0.0, "currency": "USD"})):
            self.assertIn("INVALID_COST", call().reason)
        self.assertIn("LIMIT_EXCEEDED", self.search().call(operation="S", params={"estimated_cost": 0.01,
                                                                                  "currency": "USD"}).reason)
        self.assertEqual(_Svc.hits, [])

    def test_unknown_cost_without_trusted_upper_bound_opens_incident_and_blocks(self):
        self.lim("http_generation_provider", 10.0)
        self.assertTrue(self.gen().call(operation="G", params={"estimated_cost": 0.01, "currency": "USD"}).available)
        self.assertEqual([i["kind"] for i in self.g.open_incidents("http_generation_provider")],
                         ["COST_UNKNOWN_NO_UPPER_BOUND"])
        self.assertIn("COST_INCIDENT_REVIEW_REQUIRED", self.gen().call(
            operation="G", params={"estimated_cost": 0.01, "currency": "USD"}).reason)
        self.assertEqual(len(_Svc.hits), 1)

    def test_trusted_upper_bound_sets_minimum_reservation_not_callers_estimate(self):
        self.lim("http_generation_provider", 1.0)
        self.lim("http_generation_provider", 0.5, kind="unknown_cost_upper_bound")
        self.assertTrue(self.gen().call(operation="G", params={"estimated_cost": 0.01, "currency": "USD"}).available)
        s = self.g.summary()["http_generation_provider/USD"]
        self.assertEqual(s["budget_charged"], 0.5, "cobrado pelo teto confiável do Owner, não pelos 0.01 do chamador")
        self.assertEqual(self.g.open_incidents("http_generation_provider"), [])
        self.assertTrue(self.gen().call(operation="G", params={"estimated_cost": 0.01, "currency": "USD"}).available)
        self.assertIn("LIMIT_EXCEEDED", self.gen().call(operation="G", params={"estimated_cost": 0.01,
                                                                               "currency": "USD"}).reason)
        self.assertEqual(len(_Svc.hits), 2)


# ------------------------------------------------ B3 reconciliação financeira
class FinancialReconciliationTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("p", 10.0)

    def _mismatch(self):
        rid = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=100.0, actual_currency="EUR")
        return self.g.open_incidents("p")[0]["incident_id"]

    def test_status_only_resolution_is_refused_for_financial_incidents(self):
        iid = self._mismatch()
        with self.assertRaises(ValueError):
            self.g.resolve_incident(iid, principal=OWNER, evidence="ok, revisado")
        self.assertEqual(self.g.preview(provider="p", service="s", operation="o", estimated_cost=1.0,
                                        currency="USD")["code"], "COST_INCIDENT_REVIEW_REQUIRED")

    def test_reconciliation_requires_proof_fx_and_budget_currency(self):
        iid = self._mismatch()
        with self.assertRaises(PermissionError):
            self.g.reconcile_incident(iid, principal=Principal(subject="a", role=Role.ATLAS, scopes={"*"}), evidence="e",
                                      source="s", final_amount=1.0, final_currency="USD")
        for kw in ({"evidence": "", "source": "s", "final_amount": 1.0, "final_currency": "USD"},
                   {"evidence": "e", "source": "", "final_amount": 1.0, "final_currency": "USD"},
                   {"evidence": "e", "source": "s", "final_amount": 100.0, "final_currency": "EUR"},   # sem conversão
                   {"evidence": "e", "source": "s", "final_amount": 108.0, "final_currency": "USD"},   # sem FX
                   {"evidence": "e", "source": "s", "final_amount": -5.0, "final_currency": "USD"}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self.g.reconcile_incident(iid, principal=OWNER, **kw)
        self.g.reconcile_incident(iid, principal=OWNER, evidence="fatura", source="provedor", final_amount=8.5,
                                  final_currency="USD", fx_rate=0.085, fx_source="BCE", fx_timestamp="2026-09-25T00:00Z")
        self.assertEqual(self.g.summary()["p/USD"]["budget_charged"], 8.5, "ledger corrigido para o valor final")
        self.assertEqual(self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0,
                                          currency="USD")["code"], "AUTHORIZED")

    def test_write_off_and_settlement_failed_reconciliation(self):
        iid = self._mismatch()
        out = self.g.reconcile_incident(iid, principal=OWNER, evidence="decisão do Owner", source="write-off", write_off=True)
        self.assertEqual(out["reconciliation"], "WRITE_OFF")
        rid = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=2.0, currency="USD")["reservation_id"]
        inc = self.g.open_incident(provider="p", kind="SETTLEMENT_FAILED", reservation_id=rid, detail="db travado")
        self.g.reconcile_incident(inc, principal=OWNER, evidence="painel do provedor", source="provedor", final_amount=1.7,
                                  final_currency="USD")
        self.assertEqual(self.g.open_reservations(), [], "reserva nunca liquidada foi liquidada pelo valor final")
        with db_module.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT budget_charge FROM ai_api_usage WHERE reservation_id = ?",
                                          (rid,)).fetchone()[0], 1.7)


# ------------------------------------------------------ B4 replay de settle
class SettlementReplayTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("r", 1000.0)
        self.rid = self.g.authorize(provider="r", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        self.g.settle(self.rid, outcome="EXECUTED", success=True, actual_cost=1.0, actual_currency="USD")

    def test_identical_retry_is_idempotent(self):
        out = self.g.settle(self.rid, outcome="EXECUTED", success=True, actual_cost=1.0, actual_currency="USD")
        self.assertTrue(out["already_settled"])
        self.assertEqual(self.g.open_incidents("r"), [])

    def test_divergent_replay_is_conflict_with_incident_and_block(self):
        for kw in ({"actual_cost": 100.0}, {"actual_cost": 1.0, "outcome": "NOT_EXECUTED"}):
            args = dict(outcome="EXECUTED", success=True, actual_cost=1.0, actual_currency="USD")
            args.update(kw)
            with self.assertRaises(SettlementConflict, msg=str(kw)):
                self.g.settle(self.rid, **args)
        self.assertEqual(self.g.summary()["r/USD"]["budget_charged"], 1.0, "ledger inalterado")
        self.assertEqual({i["kind"] for i in self.g.open_incidents("r")}, {"SETTLEMENT_REPLAY_CONFLICT"})
        self.assertEqual(self.g.preview(provider="r", service="s", operation="o", estimated_cost=1.0,
                                        currency="USD")["code"], "COST_INCIDENT_REVIEW_REQUIRED")

    def test_legacy_settlement_without_fingerprint_fails_safe(self):
        with db_module.connect(self.db) as conn:
            conn.execute("UPDATE ai_api_reservations SET settlement_fingerprint = NULL WHERE reservation_id = ?", (self.rid,))
        with self.assertRaises(SettlementConflict):
            self.g.settle(self.rid, outcome="EXECUTED", success=True, actual_cost=1.0, actual_currency="USD")


# ------------------------------------------ navegador: antes de page.evaluate
class BrowserMutationTests(_Base):
    def test_kill_switch_during_goto_prevents_evaluate(self):
        from bridge.execution.browser_executor import BrowserExecutor
        calls, db = [], self.db

        class Page:
            def goto(self, url, **kw):
                calls.append("goto")
                kill_switch.activate(db, principal=OWNER, reason="durante a navegação")

            def evaluate(self, script):
                calls.append("evaluate")

        class Br:
            def new_page(self):
                return Page()

            def new_context(self, **kw):
                return self

            def close(self):
                pass

        @contextlib.contextmanager
        def sp():
            yield types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **k: Br()))
        fake = types.ModuleType("playwright.sync_api")
        fake.sync_playwright = sp
        fake.Error = Exception
        with mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": fake}), \
                mock.patch.object(BrowserExecutor, "is_browser_available", return_value=(True, "")):
            r = BrowserExecutor().execute_authenticated_action(url=self.url + "/x", storage_state_path=None,
                                                               action_script="document.title")
        self.assertEqual(calls, ["goto"], "a ação mutável (evaluate) nunca roda após o kill switch")
        self.assertIn(BLOCK, r.reason)


if __name__ == "__main__":
    unittest.main()
