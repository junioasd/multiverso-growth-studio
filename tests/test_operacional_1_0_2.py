"""
v1.0.2-OPERATIONAL-CANDIDATE — correção dos 4 bloqueadores da auditoria
independente de readiness da 1.0.1. Caminho real: provedor pago contra
servidor HTTP LOCAL, SQLite, processos reais (concorrência e SIGKILL).
"""
import http.server
import json
import os
import signal
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from core.agents.runtime.http_generation_provider import HttpGenerationProvider
from core.models import db as db_module
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role

OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Svc(http.server.BaseHTTPRequestHandler):
    hits, script, hook, delay, body = [], [], None, 0.0, b'{"asset_id": "a1"}'

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Svc.hits.append(1)
        code = _Svc.script.pop(0) if _Svc.script else 200
        if _Svc.hook and code >= 500:
            _Svc.hook()
        time.sleep(_Svc.delay)
        try:
            self.send_response(code)
            self.send_header("Content-Length", str(len(_Svc.body)))
            self.end_headers()
            self.wfile.write(_Svc.body)
        except Exception:
            pass

    def log_message(self, *a):
        pass


_REAL_URLOPEN = __import__("urllib.request", fromlist=["urlopen"]).urlopen


def _loopback_only_urlopen(request, *args, **kwargs):
    """CONTROLE de `urllib.request.urlopen` (exigido pela guarda
    `test_no_test_calls_network_methods_without_mocking_urlopen`): as chamadas
    continuam REAIS, mas só contra o servidor LOOPBACK do próprio teste —
    host externo é RECUSADO."""
    import urllib.parse
    host = urllib.parse.urlparse(getattr(request, "full_url", request)).hostname
    if host not in ("127.0.0.1", "localhost"):
        raise AssertionError(f"rede EXTERNA proibida em teste: {host}")
    return _REAL_URLOPEN(request, *args, **kwargs)


class _Base(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self._net = mock.patch("urllib.request.urlopen", side_effect=_loopback_only_urlopen)
        self._net.start()
        self.addCleanup(self._net.stop)
        _Svc.hits, _Svc.script, _Svc.hook, _Svc.delay, _Svc.body = [], [], None, 0.0, b'{"asset_id": "a1"}'
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
        self.g = cost_guard.CostGuard(self.db)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        db_module.remove_db_file(self.db)

    def provider(self, timeout=5.0):
        return HttpGenerationProvider(api_key="k-local-teste", api_url=self.url + "/gen", timeout_seconds=timeout)

    def limit(self, amount, provider="http_generation_provider", kind="provider_total"):
        self.g.set_limit(principal=OWNER, provider=provider, kind=kind, amount=amount, currency="USD")

    def call(self, cost=0.6, **extra):
        return self.provider(**extra).call(operation="GENERATE", params={"estimated_cost": cost, "currency": "USD"})


# ---------------------------------------------------- 1–2 limite + contabilização
class CostAccountingTests(_Base):
    def test_0_external_host_is_refused_by_the_test_control(self):
        self.limit(10.0)
        r = HttpGenerationProvider(api_key="k", api_url="https://exemplo-externo.invalid/gen").call(
            operation="GENERATE", params={"estimated_cost": 0.1, "currency": "USD"})
        self.assertFalse(r.available)
        self.assertIn("rede EXTERNA proibida", r.reason)

    def test_1_sequential_calls_respect_cumulative_limit(self):
        self.limit(1.0)
        # v1.0.5 (auditoria 1.0.4, B2): provedor pago que não informa custo exige teto
        # CONFIÁVEL do Owner; 0.6 mantém as mesmas reservas/asserções deste teste.
        self.limit(0.6, kind="unknown_cost_upper_bound")
        self.assertTrue(self.call(0.6).available)
        second = self.call(0.6)
        self.assertFalse(second.available)
        self.assertIn("LIMIT_EXCEEDED", second.reason)
        self.assertEqual(len(_Svc.hits), 1, "a 2ª chamada paga nunca saiu")
        self.assertEqual(self.g.summary()["http_generation_provider/USD"]["budget_charged"], 0.6)

    def test_2_accounting_is_integrated_into_the_real_provider(self):
        self.limit(10.0)
        # v1.0.5 (auditoria 1.0.4, B2): teto confiável do Owner para custo desconhecido;
        # 0.5 mantém exatamente as reservas/lançamentos esperados abaixo.
        self.limit(0.5, kind="unknown_cost_upper_bound")
        _Svc.body = b'{"asset_id": "a1", "cost": 0.42, "currency": "USD"}'
        self.assertTrue(self.call(0.6).available)
        _Svc.body = b'{"asset_id": "a2"}'
        self.assertTrue(self.call(0.5).available)
        _Svc.script = [503]
        self.assertFalse(self.call(0.7).available)
        _Svc.script = [500]
        self.assertFalse(self.call(0.8).available)
        with db_module.connect(self.db) as conn:
            rows = [dict(r) for r in conn.execute("SELECT cost, cost_status, budget_charge, success FROM ai_api_usage "
                                                  "ORDER BY usage_id")]
        self.assertEqual([(r["cost"], r["cost_status"], r["budget_charge"]) for r in rows],
                         [(0.42, "CONFIRMED", 0.42),          # custo reportado pelo provedor
                          (None, "COST_UNKNOWN", 0.5),        # executado sem custo: cobra a reserva, não inventa
                          (None, "NOT_EXECUTED", 0.0),        # 503: nada processado, reserva liberada
                          (None, "COST_UNKNOWN", 0.8)])       # 500: ambíguo, cobrança mantida
        self.assertEqual(len(_Svc.hits), 4)


# ------------------------------------------------------------ 3 concorrência
_RESERVE = r'''
import sys, time, json
sys.path.insert(0, sys.argv[1])
from core.operations.cost_guard import CostGuard
while time.time() < float(sys.argv[3]):
    time.sleep(0.001)
r = CostGuard(sys.argv[2]).authorize(provider="conc", service="s", operation="o", estimated_cost=6.0, currency="USD")
print(json.dumps({"code": r["code"]}))
'''


class ConcurrencyTests(_Base):
    def test_3_threads_never_exceed_limit(self):
        self.limit(10.0, provider="conc")
        codes = []
        barrier = threading.Barrier(8)

        def go():   # cenário da auditoria: autoriza e, SE autorizado, registra o uso
            barrier.wait()
            code = self.g.authorize(provider="conc", service="s", operation="o", estimated_cost=6.0,
                                    currency="USD")["code"]
            if code == "AUTHORIZED":
                self.g.record_usage(provider="conc", service="s", operation="o", success=True, currency="USD", cost=6.0)
            codes.append(code)
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(codes.count("AUTHORIZED"), 1)
        self.assertEqual(self.g.summary()["conc/USD"]["budget_charged"], 6.0, "sem cobrança dupla, dentro do limite")
        self.assertEqual(self.g.open_reservations(), [], "reserva liquidada pelo registro de uso")

    def test_3b_separate_processes_never_exceed_limit(self):
        self.limit(10.0, provider="conc")
        start = time.time() + 1.0
        procs = [subprocess.Popen([sys.executable, "-c", _RESERVE, ROOT, self.db, str(start)], stdout=subprocess.PIPE,
                                  text=True) for _ in range(4)]
        codes = [json.loads(p.communicate(timeout=60)[0].strip().splitlines()[-1])["code"] for p in procs]
        self.assertEqual(codes.count("AUTHORIZED"), 1, codes)

    def test_3c_concurrent_real_provider_calls(self):
        self.limit(1.0)
        results = []
        ts = [threading.Thread(target=lambda: results.append(self.call(0.6).available)) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual((results.count(True), len(_Svc.hits)), (1, 1))


# ---------------------------------------------- 4 crash e timeout ambíguo
_CRASH = r'''
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from core.operations.cost_guard import CostGuard
r = CostGuard(sys.argv[2]).authorize(provider="p", service="s", operation="o", estimated_cost=7.0, currency="USD")
print(r["reservation_id"], flush=True)
os.kill(os.getpid(), signal.SIGKILL)       # morre ANTES de liquidar
'''


class CrashAndAmbiguityTests(_Base):
    def test_4_orphan_reservation_survives_crash_and_owner_reconciles(self):
        self.limit(10.0, provider="p")
        p = subprocess.run([sys.executable, "-c", _CRASH, ROOT, self.db], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, -signal.SIGKILL)
        rid = p.stdout.strip()
        g2 = cost_guard.CostGuard(self.db)                           # "restart"
        self.assertEqual([r["reservation_id"] for r in g2.open_reservations()], [rid])
        self.assertEqual(g2.authorize(provider="p", service="s", operation="o", estimated_cost=5.0,
                                      currency="USD")["code"], "LIMIT_EXCEEDED", "reserva órfã continua contando")
        with self.assertRaises(PermissionError):
            g2.reconcile(rid, principal=Principal(subject="a", role=Role.ATLAS, scopes={"*"}), executed=False,
                         actual_cost=None, evidence="x")
        g2.reconcile(rid, principal=OWNER, executed=False, actual_cost=None, evidence="painel do provedor: sem cobrança")
        self.assertEqual(g2.authorize(provider="p", service="s", operation="o", estimated_cost=5.0,
                                      currency="USD")["code"], "AUTHORIZED")

    def test_4b_ambiguous_timeout_keeps_charge_until_reconciled(self):
        self.limit(1.0)
        _Svc.delay = 0.8
        r = self.call(0.6, timeout=0.3)
        self.assertFalse(r.available)
        time.sleep(0.9)
        _Svc.delay = 0.0
        held = [x for x in self.g.open_reservations() if x["status"] == "HELD_UNKNOWN"]
        self.assertEqual(len(held), 1)
        self.assertIn("LIMIT_EXCEEDED", self.call(0.6).reason, "cobrança ambígua mantida")
        self.g.reconcile(held[0]["reservation_id"], principal=OWNER, executed=True, actual_cost=0.3,
                         evidence="fatura do provedor: 0.30 USD")
        self.assertTrue(self.call(0.6).available, "após reconciliar pelo custo real, há saldo")


# ------------------------------------------- 5–6 kill switch entre retries
class KillSwitchRetryTests(_Base):
    def test_5_kill_switch_between_webhook_retries_stops_next_attempt(self):
        from bridge.execution.webhooks import RetryPolicy, WebhookDispatcher
        _Svc.script = [503, 200]
        _Svc.hook = lambda: kill_switch.activate(self.db, principal=OWNER, reason="emergência entre retries")
        r = WebhookDispatcher(retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01),
                              sleep_fn=lambda s: None).deliver(url=self.url + "/h", payload={})
        self.assertEqual(len(_Svc.hits), 1, "nenhuma tentativa após o kill switch")
        self.assertFalse(r.success)
        self.assertIn(kill_switch.BLOCK_CODE, r.reason)

    def test_6_operation_runner_retry_is_stopped_by_kill_switch(self):
        from api.bootstrap import build_app
        from bridge.execution.navigator_real import ActionCriticality  # noqa: F401
        from bridge.execution.platform_adapters import PlatformApiAdapter
        from bridge.execution.vault import TokenType
        from bridge.hub import ConnectorType, HealthStatus
        from core.security.auth import principal_from_session
        from navigator.planning import OperationPlanner, OperationRunner, StepSpec
        from bridge.execution.webhooks import RetryPolicy
        from tests._auth_test_helpers import fast_hash_password
        app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"),
                        db_path=self.db, init_database=True)
        ow = principal_from_session(app.auth.current_session(app.auth.login("o@t.com", "TestPassword123")))
        base = self.url

        class A(PlatformApiAdapter):
            platform = "youtube"
            base_url = base

            def publish(self, **kw):
                return self._request(action="PUBLISH", method="POST", path="/p", body=kw)
        ref = app.atlas.token_vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="t")
        app.atlas.bridge.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
                                            is_configured=True, authenticated=True, health=HealthStatus.OK,
                                            permissions_ok=True)
        app.atlas.navigator_real.register_adapter(platform="youtube", adapter=A(vault=app.atlas.token_vault,
                                                                                account_reference_id=ref))
        r = OperationRunner(db_path=self.db, planner=OperationPlanner(bridge_hub=app.atlas.bridge),
                            navigator=app.atlas.navigator_real, escalation_desk=app.atlas.escalation_desk, actor="T",
                            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.05, max_delay_seconds=0.1))
        run = r.create_run(objective="o", requested_by="T", steps=[StepSpec(
            step_id="p", platform="youtube", action="PUBLISH_VIDEO", adapter_method="publish", params={"title": "t"})])
        r.advance(run)
        _, st, _ = r.load(run)
        app.atlas.escalation_desk.approve(st["p"]["escalation_id"], decision="ok", resolved_by=ow)
        _Svc.script = [503]
        r.advance(run)
        _, st, _ = r.load(run)
        self.assertEqual(st["p"]["status"], "RETRYING")
        kill_switch.activate(self.db, principal=OWNER, reason="parar retries")
        time.sleep(0.15)
        r.advance(run)
        _, st, _ = r.load(run)
        self.assertEqual((st["p"]["status"], st["p"]["attempts"], len(_Svc.hits)), ("RETRYING", 1, 1))
        self.assertIn(kill_switch.BLOCK_CODE, " ".join(st["p"]["reasons"]))


class LivenessTests(_Base):
    def test_6b_kill_switch_before_first_advance_never_hangs_runner_or_worker(self):
        """Defeito REAL da 1.0.1 revelado por mutante travado: com o kill switch
        ativo antes do pedido de aprovação, `advance()` girava para sempre."""
        from api.bootstrap import build_app
        from bridge.hub import ConnectorType, HealthStatus
        from core.jobs.queue import JobQueue
        from core.operations.worker import run_worker_cycle
        from navigator.planning import OperationPlanner, OperationRunner, StepSpec, schedule_advance
        from tests._auth_test_helpers import fast_hash_password
        app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"),
                        db_path=self.db, init_database=True)
        app.atlas.bridge.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
                                            is_configured=True, authenticated=True, health=HealthStatus.OK,
                                            permissions_ok=True)
        kill_switch.activate(self.db, principal=OWNER, reason="ativo antes do primeiro advance")
        r = OperationRunner(db_path=self.db, planner=OperationPlanner(bridge_hub=app.atlas.bridge),
                            navigator=app.atlas.navigator_real, escalation_desk=app.atlas.escalation_desk, actor="T")
        run = r.create_run(objective="o", requested_by="T", steps=[StepSpec(
            step_id="p", platform="youtube", action="PUBLISH_VIDEO", adapter_method="publish", params={"title": "t"})])
        out = {}
        t = threading.Thread(target=lambda: out.update(s=r.advance(run)), daemon=True)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), "advance() travou com o kill switch ativo")
        _, st, _ = r.load(run)
        self.assertEqual((st["p"]["status"], st["p"]["escalation_id"]), ("PLANNED", None))
        self.assertIn(kill_switch.BLOCK_CODE, " ".join(st["p"]["reasons"]))
        schedule_advance(JobQueue(db_path=self.db), run, tick=1)
        w = threading.Thread(target=lambda: out.update(w=run_worker_cycle(db_path=self.db, runner=r)), daemon=True)
        w.start()
        w.join(10)
        self.assertFalse(w.is_alive(), "worker travou")
        kill_switch.deactivate(self.db, principal=OWNER, reason="fim da emergência")
        self.assertEqual(r.advance(run), "WAITING_APPROVAL", "após desativar, o fluxo normal retoma")


# ------------------------------------------- 7–8 estado do kill switch
class KillSwitchStateTests(_Base):
    def _rows(self, sql, params=()):
        c = sqlite3.connect(self.db)
        try:
            out = c.execute(sql, params).fetchall()
            c.commit()
            return out
        finally:
            c.close()

    def test_7_missing_or_invalid_row_fails_closed_and_restart_does_not_heal_it(self):
        self._rows("DELETE FROM operational_kill_switch")
        self.assertEqual(kill_switch.state(self.db)["state"], "UNREADABLE")
        self.assertFalse(kill_switch.external_execution_allowed(self.db))
        db_module.init_db(self.db)                                   # restart/migração
        self.assertEqual(kill_switch.state(self.db)["state"], "UNREADABLE", "linha apagada NÃO é recriada como DISABLED")
        self.assertIn(kill_switch.BLOCK_CODE, self.call(0.1).reason)
        kill_switch.deactivate(self.db, principal=OWNER, reason="estado redefinido após incidente")
        self.assertTrue(kill_switch.external_execution_allowed(self.db))
        self._rows("UPDATE operational_kill_switch SET state = 'TALVEZ' WHERE id = 1")
        self.assertFalse(kill_switch.external_execution_allowed(self.db))

    def test_8_new_and_upgraded_databases_get_explicit_initial_state(self):
        rows = self._rows("SELECT state, actor FROM operational_kill_switch")
        self.assertEqual(rows, [("DISABLED", "system:init_db")])
        self.assertEqual(kill_switch.history(self.db)[0]["actor"], "system:init_db")
        up = os.path.join(self.tmp, "v100.db")                     # banco da v1.0.0: sem a tabela
        db_module.init_db(up)
        c = sqlite3.connect(up)
        try:
            c.execute("DROP TABLE operational_kill_switch")
            c.execute("DROP TABLE operational_kill_switch_events")
            c.commit()
        finally:
            c.close()
        db_module.init_db(up)                                        # upgrade real
        self.assertEqual(kill_switch.state(up)["state"], "DISABLED")
        db_module.remove_db_file(up)


if __name__ == "__main__":
    unittest.main()
