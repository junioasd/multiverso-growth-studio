"""
v1.0.11-OPERATIONAL-CANDIDATE — P1 da auditoria cirúrgica 1.0.10: identidade e
idempotência de PONTA A PONTA (origem -> reserva -> I/O -> liquidação -> uso).
Parte A: run_metered_call propaga correlation_id (execução no máximo uma vez).
Parte B: usage_event_id é identidade forte, resolvida ANTES de qualquer matching.
Rede: somente servidor LOOPBACK do próprio teste. SQLite real; threads reais.
"""
import http.server
import os
import socketserver
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from core.models import db as db_module
from core.operations import cost_guard, kill_switch
from core.operations.cost_guard import SettlementConflict
from core.security.auth import Principal, Role

OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
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

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Svc.hits.append(self.path)
        out = b'{"asset_id": "a", "cost": 1.0, "currency": "USD"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class _Resp:
    def __init__(self, ok, reason=""):
        self.available, self.reason = ok, reason


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "c.db")
        db_module.init_db(self.db)
        kill_switch.configure(self.db)
        cost_guard.configure(self.db)
        self.addCleanup(kill_switch.configure, None)
        self.addCleanup(cost_guard.configure, None)
        self.g = cost_guard.CostGuard(self.db)
        self.g.set_limit(principal=OWNER, provider="paid", kind="provider_total", amount=100.0, currency="USD")
        self.calls = []

    def tearDown(self):
        db_module.remove_db_file(self.db)

    def sql(self, q, params=()):
        c = sqlite3.connect(self.db)
        try:
            return c.execute(q, params).fetchall()
        finally:
            c.close()

    def perform(self):
        self.calls.append(1)
        return (_Resp(True), "EXECUTED", 1.0, "USD")

    def run_call(self, cid="logical-1", amount=1.0, perform=None, **extra):
        params = {"estimated_cost": amount, "currency": "USD", "correlation_id": cid}
        params.update(extra)
        return cost_guard.run_metered_call(provider="paid", classification="PAID", operation="gen", params=params,
                                           perform=perform or self.perform, blocked=lambda why: _Resp(False, why))

    def ledger(self, provider="paid"):
        return (self.sql("SELECT COUNT(*) FROM ai_api_reservations WHERE provider=?", (provider,))[0][0],
                self.sql("SELECT COUNT(*), COALESCE(SUM(budget_charge),0) FROM ai_api_usage WHERE provider=?", (provider,))[0])


# ------------------------------------------ PARTE A: run_metered_call (1–9)
class MeteredCallIdentityTests(_Base):
    def test_1_to_6_retry_never_reexecutes_or_duplicates_and_completed_is_terminal(self):
        self.assertTrue(self.run_call().available)
        second = self.run_call()
        self.assertFalse(second.available)
        self.assertIn("ALREADY_COMPLETED", second.reason)
        self.assertEqual(len(self.calls), 1, "perform() executado UMA vez")
        self.assertEqual(self.ledger(), (1, (1, 1.0)), "1 reserva, 1 uso, custo não duplicado")
        self.assertEqual(self.sql("SELECT correlation_id, status FROM ai_api_reservations"), [("logical-1", "COMMITTED")])

    def test_7_divergent_payload_is_conflict_before_io(self):
        self.run_call()
        for kw in ({"amount": 2.0}, {"project": "outro"}):
            out = self.run_call(**kw)
            self.assertIn("CORRELATION_ID_CONFLICT", out.reason, kw)
        self.assertEqual(len(self.calls), 1, "nenhum I/O no conflito")

    def test_open_reservation_retry_does_not_execute_while_in_flight(self):
        started, release, results = threading.Event(), threading.Event(), []

        def slow():
            self.calls.append(1)
            started.set()
            release.wait(5)
            return (_Resp(True), "EXECUTED", 1.0, "USD")
        t = threading.Thread(target=lambda: results.append(self.run_call(perform=slow)))
        t.start()
        started.wait(5)
        during = self.run_call()                      # retry idêntico com a 1ª ainda EM CURSO
        release.set()
        t.join()
        self.assertIn("ALREADY_RESERVED", during.reason)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(results[0].available)

    def test_8_concurrent_same_correlation_executes_exactly_once(self):
        barrier, outs = threading.Barrier(8), []

        def go():
            barrier.wait()
            outs.append(self.run_call(cid="conc-1"))
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(1 for o in outs if o.available), 1)
        self.assertEqual(self.ledger(), (1, (1, 1.0)))

    def test_9_restart_preserves_idempotency(self):
        self.run_call(cid="r-1")
        cost_guard.configure(None)
        cost_guard.configure(self.db)                   # "restart" do processo/guarda
        self.assertIn("ALREADY_COMPLETED", self.run_call(cid="r-1").reason)
        self.assertEqual(len(self.calls), 1)

    def test_end_to_end_real_provider_path_sends_one_request(self):
        """EXECUTADO_REALMENTE (local): HttpGenerationProvider real -> run_metered_call -> socket loopback."""
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        net = mock.patch("urllib.request.urlopen", side_effect=_loopback_only_urlopen)
        net.start()
        self.addCleanup(net.stop)
        _Svc.hits = []
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Svc)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.g.set_limit(principal=OWNER, provider="http_generation_provider", kind="provider_total", amount=100.0,
                         currency="USD")
        p = HttpGenerationProvider(api_key="k", api_url=f"http://127.0.0.1:{srv.server_address[1]}/gen")
        params = {"estimated_cost": 1.0, "currency": "USD", "correlation_id": "video-42"}
        first, retry = p.call(operation="G", params=params), p.call(operation="G", params=params)
        self.assertTrue(first.available)
        self.assertIn("ALREADY_COMPLETED", retry.reason)
        self.assertEqual(_Svc.hits, ["/gen"], "o provedor externo recebeu UMA requisição")
        self.assertEqual(self.ledger("http_generation_provider"), (1, (1, 1.0)))


# --------------------------------------- PARTE B: usage_event_id (10–21)
class UsageEventStrongIdentityTests(_Base):
    def setUp(self):
        super().setUp()
        self.g.set_limit(principal=OWNER, provider="p", kind="provider_total", amount=100.0, currency="USD")

    def auth(self, amount=1.0, **kw):
        return self.g.authorize(provider="p", service="s", operation="o", estimated_cost=amount, currency="USD",
                                **kw)["reservation_id"]

    def deliver(self, ev, cost=1.0, **kw):
        return self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=cost, currency="USD",
                                   usage_event_id=ev, **kw)

    def status(self, rid):
        return self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (rid,))[0][0]

    def test_10_to_13_redelivery_after_new_reservation_never_settles_it(self):
        a = self.auth()
        first = self.deliver("evt-1")                                  # único candidato: liquida A
        b = self.auth()                                               # nova reserva criada DEPOIS
        again = self.deliver("evt-1")                                 # redelivery atrasado
        self.assertEqual((again["usage_id"], again["reservation_id"], again["already_recorded"]),
                         (first["usage_id"], a, True), "mesma resposta, mesmo uso")
        self.assertEqual((self.status(a), self.status(b)), ("COMMITTED", "RESERVED"), "B intacta")
        self.assertEqual(self.sql("SELECT COUNT(*), SUM(budget_charge) FROM ai_api_usage WHERE provider='p'"), [(1, 1.0)])
        self.assertEqual(self.sql("SELECT usage_event_id FROM ai_api_usage WHERE reservation_id=?", (a,)), [("evt-1",)])
        with self.assertRaises(ValueError) as ctx:                     # nem apontando B explicitamente
            self.deliver("evt-1", reservation_id=b)
        self.assertIn("USAGE_EVENT_CONFLICT", str(ctx.exception))
        self.assertEqual(self.status(b), "RESERVED")

    def test_14_divergent_payload_same_event_is_conflict(self):
        a = self.auth()
        self.deliver("evt-2", reservation_id=a)
        with self.assertRaises(ValueError) as ctx:
            self.deliver("evt-2", cost=9.0, reservation_id=a)
        self.assertIn("USAGE_EVENT_CONFLICT", str(ctx.exception))

    def test_15_16_out_of_order_with_explicit_identity(self):
        a, b = self.auth(correlation_id="call-A"), self.auth(amount=5.0, correlation_id="call-B")
        self.deliver("evt-B", cost=5.0, correlation_id="call-B")      # B chega antes
        self.deliver("evt-A", reservation_id=a)
        self.assertEqual((self.status(a), self.status(b)), ("COMMITTED", "COMMITTED"))
        self.assertEqual(dict(self.sql("SELECT usage_event_id, reservation_id FROM ai_api_usage WHERE provider='p'")),
                         {"evt-A": a, "evt-B": b})
        self.assertEqual(self.g.open_incidents("p"), [], "nenhum overrun cruzado")

    def test_17_18_no_identity_with_two_candidates_is_ambiguous_explicit_identity_wins(self):
        a, b = self.auth(), self.auth()
        with self.assertRaises(ValueError) as ctx:
            self.deliver("evt-3")                                      # event id NOVO + 2 candidatos
        self.assertIn("AMBIGUOUS_RESERVATION_MATCH", str(ctx.exception))
        self.assertEqual((self.status(a), self.status(b)), ("RESERVED", "RESERVED"))
        self.deliver("evt-3", reservation_id=b)                        # identidade explícita decide
        self.assertEqual((self.status(a), self.status(b)), ("RESERVED", "COMMITTED"))

    def test_concurrent_redelivery_of_same_event_settles_exactly_once(self):
        a = self.auth()
        barrier, outs, errs = threading.Barrier(6), [], []

        def go():
            barrier.wait()
            try:
                outs.append(self.deliver("evt-conc", reservation_id=a)["usage_id"])
            except SettlementConflict as exc:                          # nunca deve ocorrer: payload idêntico
                errs.append(exc)
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_usage WHERE usage_event_id='evt-conc'"), [(1,)])
        self.assertEqual(self.sql("SELECT COUNT(*), SUM(budget_charge) FROM ai_api_usage WHERE provider='p'"), [(1, 1.0)])

    def test_19_idempotency_survives_restart(self):
        a = self.auth()
        first = self.deliver("evt-4", reservation_id=a)
        g2 = cost_guard.CostGuard(self.db)
        again = g2.record_usage(provider="p", service="s", operation="o", success=True, cost=1.0, currency="USD",
                                usage_event_id="evt-4", reservation_id=a)
        self.assertEqual(again["usage_id"], first["usage_id"])

    def test_20_21_divergent_replay_and_identity_mismatch_still_blocked(self):
        a = self.auth()
        b = self.auth()           # criada ANTES: o replay divergente abre incidente e bloqueia o provedor
        self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=1.0, currency="USD",
                            reservation_id=a)
        with self.assertRaises(SettlementConflict):
            self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=7.0, currency="USD",
                                reservation_id=a)
        with self.assertRaises(ValueError) as ctx:
            self.deliver("evt-5", reservation_id=b, project="outro")
        self.assertIn("RESERVATION_IDENTITY_MISMATCH", str(ctx.exception))
        self.assertEqual(self.status(b), "RESERVED")


# ------------------------------------------ 22–24 regras preservadas
class PreservedRulesTests(_Base):
    def test_22_23_24(self):
        self.assertFalse(cost_guard.valid_currency("ZZZ"))
        self.g.set_limit(principal=OWNER, provider="u", kind="provider_total", amount=1000.0, currency="USD")
        self.g.set_limit(principal=OWNER, provider="u", kind="unknown_cost_upper_bound", amount=50.0, currency="USD")
        old = self.g.record_usage(provider="u", service="s", operation="o", success=True, usage_event_id="ev-old")
        self.g.set_limit(principal=OWNER, provider="u", kind="unknown_cost_upper_bound", amount=1.0, currency="USD")
        self.g.reconcile_usage(old["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        self.assertEqual(self.sql("SELECT budget_charge FROM ai_api_usage WHERE usage_id=?", (old["usage_id"],)), [(50.0,)])
        self.g.set_limit(principal=OWNER, provider="w", kind="provider_total", amount=1000.0, currency="USD")
        rid = self.g.authorize(provider="w", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        self.g.settle(rid, outcome="NOT_EXECUTED", success=False, actual_cost=100.0, actual_currency="USD")
        self.g.reconcile_incident(self.g.open_incidents("w")[0]["incident_id"], principal=OWNER, evidence="e",
                                  source="write-off", write_off=True)
        self.assertEqual(self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (rid,)), [("WRITTEN_OFF",)])


if __name__ == "__main__":
    unittest.main()
