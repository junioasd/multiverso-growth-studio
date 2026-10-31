"""
v1.0.12-OPERATIONAL-CANDIDATE — P1 da auditoria cirúrgica final 1.0.11: o correlation_id
é vinculado ao PAYLOAD OPERACIONAL canônico (prompt, query, conteúdo, modelo, parâmetros),
não só aos metadados financeiros. Conflito SEMPRE antes de qualquer I/O.
Rede: somente servidor LOOPBACK do próprio teste. SQLite real; threads reais.
"""
import http.server
import json
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
from core.operations.cost_guard import canonical_json, request_fingerprint
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


def fp(params, provider="paid", operation="gen"):
    return request_fingerprint(provider=provider, service=operation, operation=operation, params=params)


# ------------------------------------------ 7–11 canonicalização (unitário)
class CanonicalizationTests(unittest.TestCase):
    def test_7_8_key_order_and_nested_structures_do_not_matter(self):
        a = {"prompt": "x", "opts": {"temp": 0.2, "model": "m1", "tags": [{"b": 1, "a": 2}]}}
        b = {"opts": {"tags": [{"a": 2, "b": 1}], "model": "m1", "temp": 0.2}, "prompt": "x"}
        self.assertEqual(fp(a), fp(b))
        self.assertEqual(canonical_json(a), canonical_json(b))

    def test_9_list_order_is_semantically_relevant(self):
        self.assertNotEqual(fp({"refs": ["a", "b"]}), fp({"refs": ["b", "a"]}))
        self.assertEqual(fp({"refs": ("a", "b")}), fp({"refs": ["a", "b"]}), "tupla = lista (política explícita)")

    def test_10_types_are_preserved_explicitly(self):
        values = [fp({"n": 1}), fp({"n": "1"}), fp({"n": 1.0}), fp({"n": True})]
        self.assertEqual(len(set(values)), 4, "1, '1', 1.0 e True são distintos")

    def test_11_none_differs_from_absent(self):
        self.assertNotEqual(fp({"seed": None}), fp({}))

    def test_excluded_fields_are_only_identities(self):
        self.assertEqual(cost_guard.FINGERPRINT_EXCLUDED_FIELDS, ("correlation_id", "usage_event_id"))
        self.assertEqual(fp({"prompt": "x", "correlation_id": "a", "usage_event_id": "e1"}),
                         fp({"prompt": "x", "correlation_id": "b"}))

    def test_ambiguous_types_are_rejected_never_stringified(self):
        for bad in ({"s": {1, 2}}, {"b": b"x"}, {"o": object()}, {"f": float("nan")}, {"k": {1: "x"}}):
            with self.assertRaises(ValueError, msg=str(bad)) as ctx:
                fp(bad)
            self.assertIn("REQUEST_NOT_CANONICALIZABLE", str(ctx.exception))

    def test_fingerprint_is_sha256_hex_and_stable_across_calls(self):
        f1 = fp({"prompt": "x", "opts": {"a": 1}})
        self.assertRegex(f1, r"^[0-9a-f]{64}$")
        self.assertEqual(f1, fp({"opts": {"a": 1}, "prompt": "x"}))


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

    def call(self, cid="cid-1", **payload):
        params = {"estimated_cost": 1.0, "currency": "USD", "correlation_id": cid}
        params.update(payload)

        def perform():
            self.calls.append(json.dumps(payload, sort_keys=True))
            return (_Resp(True), "EXECUTED", 1.0, "USD")
        return cost_guard.run_metered_call(provider="paid", classification="PAID", operation="gen", params=params,
                                           perform=perform, blocked=lambda why: _Resp(False, why))

    def ledger(self):
        return (self.sql("SELECT COUNT(*) FROM ai_api_reservations WHERE provider='paid'")[0][0],
                self.sql("SELECT COUNT(*), COALESCE(SUM(budget_charge),0) FROM ai_api_usage WHERE provider='paid'")[0])


# --------------------------------------- 1–6, 12, 18–21 caminho central
class OperationalPayloadIdentityTests(_Base):
    def test_1_18_19_20_21_identical_retry_is_idempotent_no_duplication(self):
        self.assertTrue(self.call(prompt="A", model="m1").available)
        again = self.call(model="m1", prompt="A")                  # mesma carga, outra ordem de chaves
        self.assertIn("ALREADY_COMPLETED", again.reason)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ledger(), (1, (1, 1.0)), "reserva, uso e custo não duplicam")

    def test_2_to_6_12_any_operational_difference_is_conflict_before_perform(self):
        self.call(prompt="A", query="q", content="c", params={"temperature": 0.2}, model="m1")
        for change in ({"prompt": "B"}, {"query": "q2"}, {"content": "c2"}, {"params": {"temperature": 0.9}},
                       {"model": "m2"}, {"extra_option": True}):
            payload = dict(prompt="A", query="q", content="c", params={"temperature": 0.2}, model="m1")
            payload.update(change)
            out = self.call(**payload)
            self.assertIn("CORRELATION_ID_CONFLICT", out.reason, change)
        self.assertEqual(len(self.calls), 1, "nenhum perform() nos conflitos")
        self.assertEqual(self.ledger(), (1, (1, 1.0)))

    def test_uncanonicalizable_payload_is_blocked_before_io(self):
        out = self.call(prompt="A", tags={"a", "b"})
        self.assertIn("REQUEST_NOT_CANONICALIZABLE", out.reason)
        self.assertEqual((len(self.calls), self.ledger()[0]), (0, 0))

    def test_15_concurrent_identical_payload_single_io(self):
        barrier, outs = threading.Barrier(8), []

        def go():
            barrier.wait()
            outs.append(self.call(cid="conc", prompt="A"))
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(1 for o in outs if o.available), 1)

    def test_16_concurrent_divergent_payloads_one_wins_others_conflict(self):
        barrier, outs = threading.Barrier(6), []

        def go(i):
            barrier.wait()
            outs.append(self.call(cid="race", prompt=f"P{i}"))
        ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(self.calls), 1, "exatamente um payload executou")
        self.assertEqual(sum(1 for o in outs if o.available), 1)
        self.assertEqual(sum(1 for o in outs if "CORRELATION_ID_CONFLICT" in (o.reason or "")), 5)

    def test_17_restart_preserves_fingerprint(self):
        self.call(cid="r", prompt="A")
        cost_guard.configure(None)
        cost_guard.configure(self.db)
        self.assertIn("ALREADY_COMPLETED", self.call(cid="r", prompt="A").reason)
        self.assertIn("CORRELATION_ID_CONFLICT", self.call(cid="r", prompt="B").reason)
        self.assertEqual(len(self.calls), 1)

    def test_legacy_reservation_without_operational_fingerprint_fails_safe(self):
        self.call(cid="leg", prompt="A")
        self.sql_exec("UPDATE ai_api_reservations SET correlation_fingerprint = ? WHERE correlation_id = 'leg'",
                      ('["reservation_payload", "paid", "gen", "gen", "1.0", "USD", null, null, null]',))
        out = self.call(cid="leg", prompt="A")                      # mesmo payload, mas não comprovável
        self.assertIn("CORRELATION_ID_CONFLICT", out.reason)
        self.assertIn("não comprovável", out.reason)
        self.assertEqual(len(self.calls), 1)

    def sql_exec(self, q, params=()):
        c = sqlite3.connect(self.db)
        try:
            c.execute(q, params)
            c.commit()
        finally:
            c.close()


# ------------------------------------------- 13–14 HTTP loopback real
class _Svc(http.server.BaseHTTPRequestHandler):
    prompts = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        _Svc.prompts.append(body["params"].get("prompt"))
        out = b'{"asset_id": "a", "cost": 1.0, "currency": "USD"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class RealProviderLoopbackTests(_Base):
    def test_13_14_identical_retry_one_request_divergent_no_second_request(self):
        """EXECUTADO_REALMENTE (local): HttpGenerationProvider -> run_metered_call -> egress -> socket loopback."""
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        net = mock.patch("urllib.request.urlopen", side_effect=_loopback_only_urlopen)
        net.start()
        self.addCleanup(net.stop)
        _Svc.prompts = []
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Svc)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.g.set_limit(principal=OWNER, provider="http_generation_provider", kind="provider_total", amount=100.0,
                         currency="USD")
        p = HttpGenerationProvider(api_key="k", api_url=f"http://127.0.0.1:{srv.server_address[1]}/gen")
        base = {"estimated_cost": 1.0, "currency": "USD", "correlation_id": "same-id"}
        self.assertTrue(p.call(operation="G", params=dict(base, prompt="A")).available)
        self.assertIn("ALREADY_COMPLETED", p.call(operation="G", params=dict(base, prompt="A")).reason)
        self.assertIn("CORRELATION_ID_CONFLICT", p.call(operation="G", params=dict(base, prompt="B")).reason)
        self.assertEqual(_Svc.prompts, ["A"], "servidor recebeu exatamente 1 requisição, só com o prompt A")


# --------------------------------------------- 22–23 regras preservadas
class PreservedIdentityRulesTests(_Base):
    def test_22_23_usage_event_and_matching_order_intact(self):
        self.g.set_limit(principal=OWNER, provider="p", kind="provider_total", amount=100.0, currency="USD")
        a = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        first = self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=1.0, currency="USD",
                                    usage_event_id="evt-1")
        b = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        again = self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=1.0, currency="USD",
                                    usage_event_id="evt-1")
        self.assertEqual((again["usage_id"], again["reservation_id"]), (first["usage_id"], a))
        self.assertEqual(self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (b,)), [("RESERVED",)])
        c = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        with self.assertRaises(ValueError) as ctx:
            self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=1.0, currency="USD",
                                usage_event_id="evt-2")
        self.assertIn("AMBIGUOUS_RESERVATION_MATCH", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.g.record_usage(provider="p", service="WRONG", operation="o", success=True, cost=1.0, currency="USD",
                                reservation_id=c)
        self.assertIn("RESERVATION_IDENTITY_MISMATCH", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
