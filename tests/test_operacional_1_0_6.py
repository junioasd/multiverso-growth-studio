"""
v1.0.6-OPERATIONAL-CANDIDATE — bloqueadores da auditoria de readiness 1.0.5:
(1) teto confiável estritamente > 0; (2) reconciliação atômica e idempotente;
(3) nenhuma soma entre moedas sem FX registrado; (4) settle() rejeita estados
contraditórios — e variantes da mesma classe. SQLite real; threads reais.
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
from core.operations.cost_guard import ReconciliationConflict, SettlementConflict
from core.security.auth import Principal, Role

OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
OWNER2 = Principal(subject="owner-sessao-2", role=Role.OWNER, scopes={"*"})
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
        out = b'{"asset_id": "a"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        self._net = mock.patch("urllib.request.urlopen", side_effect=_loopback_only_urlopen)
        self._net.start()
        self.addCleanup(self._net.stop)
        _Svc.hits = []
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

    def lim(self, provider, amount, kind="provider_total", currency="USD"):
        self.g.set_limit(principal=OWNER, provider=provider, kind=kind, amount=amount, currency=currency)

    def rsv(self, provider, amount=1.0, currency="USD"):
        return self.g.authorize(provider=provider, service="s", operation="o", estimated_cost=amount,
                                currency=currency)["reservation_id"]

    def charged(self, provider, currency="USD"):
        return self.g.summary().get(f"{provider}/{currency}", {}).get("budget_charged")


# --------------------------------------------------- 1–7 teto confiável
class UpperBoundTests(_Base):
    def test_2_to_5_invalid_upper_bounds_rejected_at_set_limit(self):
        for bad in (0.0, 0, -1.0, float("nan"), float("inf"), True, "1"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.lim("p", bad, kind="unknown_cost_upper_bound")

    def test_legacy_zero_upper_bound_already_stored_is_invalid_at_use(self):
        self.lim("http_generation_provider", 10.0)
        with db_module.connect(self.db) as conn:     # teto 0 gravado por versão anterior (bypass do set_limit)
            conn.execute("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) "
                         "VALUES ('http_generation_provider', 'unknown_cost_upper_bound', '*', 0, 'USD', 0, 'legado')")
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        p = HttpGenerationProvider(api_key="k", api_url=self.url + "/gen")
        self.assertTrue(p.call(operation="G", params={"estimated_cost": 1.0, "currency": "USD"}).available)
        self.assertEqual([i["kind"] for i in self.g.open_incidents("http_generation_provider")],
                         ["COST_UNKNOWN_NO_UPPER_BOUND"], "teto 0 = SEM teto válido")
        self.assertIn("COST_INCIDENT_REVIEW_REQUIRED", p.call(operation="G", params={"estimated_cost": 1.0,
                                                                                      "currency": "USD"}).reason)
        self.assertEqual(len(_Svc.hits), 1)

    def test_1_6_7_valid_bound_accounts_by_policy_and_missing_bound_blocks(self):
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        self.lim("http_generation_provider", 10.0)
        self.lim("http_generation_provider", 2.5, kind="unknown_cost_upper_bound")
        p = HttpGenerationProvider(api_key="k", api_url=self.url + "/gen")
        self.assertTrue(p.call(operation="G", params={"estimated_cost": 1.0, "currency": "USD"}).available)
        self.assertEqual(self.charged("http_generation_provider"), 2.5, "cobrado pelo teto confiável")
        self.assertEqual(self.g.open_incidents("http_generation_provider"), [])


# ------------------------------------------- 8–12 reconciliação atômica
class ReconciliationTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("m", 1000.0)
        rid = self.rsv("m")
        self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=100.0, actual_currency="EUR")
        self.iid = self.g.open_incidents("m")[0]["incident_id"]
        self.fx = dict(fx_rate=0.1, fx_source="BCE", fx_timestamp="2026-09-25T00:00:00Z")

    def rec(self, amount, who=OWNER):
        # v1.0.7 (auditoria 1.0.6, B4): a reconciliação FX passou a exigir que a conta feche
        # (100 EUR × 0,1 = 10 USD). Valores acima de 10 são expressos como `fees` EXPLÍCITAS,
        # preservando a intenção do teste (reconciliações válidas e DISTINTAS).
        return self.g.reconcile_incident(self.iid, principal=who, evidence="fatura", source="provedor",
                                         final_amount=amount, final_currency="USD", fees=round(amount - 10.0, 2), **self.fx)

    def test_8_concurrent_reconciliations_exactly_one_wins(self):
        results, barrier = {}, threading.Barrier(6)

        def go(v, who):
            barrier.wait()
            try:
                results[v] = self.rec(v, who)["status"]
            except ReconciliationConflict:
                results[v] = "CONFLICT"
            except Exception as exc:          # qualquer outra falha invalidaria a prova de concorrência
                results[v] = f"OUTRO_ERRO:{type(exc).__name__}"
        ts = [threading.Thread(target=go, args=(float(v), OWNER if v % 2 else OWNER2)) for v in range(10, 16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        winners = [v for v, s in results.items() if s == "RECONCILED"]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(sorted(results.values()).count("CONFLICT"), 5, "os 5 perdedores perdem por CONCORRÊNCIA")
        self.assertEqual(self.charged("m"), winners[0], "ledger = valor do ÚNICO vencedor")

    def test_9_10_identical_retry_idempotent_divergent_conflict(self):
        self.rec(10.0)
        self.assertTrue(self.rec(10.0)["already_reconciled"])
        with self.assertRaises(ReconciliationConflict):
            self.rec(20.0)
        self.assertEqual(self.charged("m"), 10.0, "a 1ª reconciliação é preservada")

    def test_11_crash_during_reconciliation_rolls_back_ledger_and_status_together(self):
        before = self.charged("m")
        c = sqlite3.connect(self.db)
        try:
            c.execute("CREATE TRIGGER falha_simulada BEFORE UPDATE OF status ON ai_api_cost_incidents "
                      "BEGIN SELECT RAISE(ABORT, 'crash simulado no meio da reconciliação'); END")
            c.commit()
        finally:
            c.close()
        with self.assertRaises(sqlite3.DatabaseError):
            self.rec(10.0)
        self.assertEqual(self.charged("m"), before, "ledger NÃO foi alterado parcialmente")
        self.assertEqual(len(self.g.open_incidents("m")), 1, "incidente continua OPEN")
        c = sqlite3.connect(self.db)
        try:
            c.execute("DROP TRIGGER falha_simulada")
            c.commit()
        finally:
            c.close()
        self.assertEqual(self.rec(10.0)["status"], "RECONCILED")

    def test_12_restart_after_reconciliation_and_legacy_without_fingerprint(self):
        self.rec(10.0)
        g2 = cost_guard.CostGuard(self.db)              # "restart"
        self.assertTrue(g2.reconcile_incident(self.iid, principal=OWNER, evidence="fatura", source="provedor",
                                              final_amount=10.0, final_currency="USD", **self.fx)["already_reconciled"])
        with db_module.connect(self.db) as conn:
            conn.execute("UPDATE ai_api_cost_incidents SET reconciliation_fingerprint = NULL WHERE incident_id = ?", (self.iid,))
        with self.assertRaises(ReconciliationConflict):
            self.rec(10.0)

    def test_reservation_reconcile_variant_is_single_winner_and_rejects_contradiction(self):
        self.lim("h", 1000.0)
        rid = self.rsv("h")
        self.g.settle(rid, outcome="UNKNOWN", success=False)
        with self.assertRaises(ValueError):
            self.g.reconcile(rid, principal=OWNER, executed=False, actual_cost=5.0, evidence="x")
        self.g.reconcile(rid, principal=OWNER, executed=True, actual_cost=0.4, evidence="fatura")
        with self.assertRaises(ReconciliationConflict):
            self.g.reconcile(rid, principal=OWNER, executed=False, actual_cost=None, evidence="outra sessão")


# --------------------------------------------------------- 13–16 moedas
class CurrencyIntegrityTests(_Base):
    def _usd_history(self, provider="fxp"):
        self.lim(provider, 100.0)
        rid = self.rsv(provider, 50.0)
        self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=50.0, actual_currency="USD", cost_confirmed=True)

    def test_13_usd_to_eur_with_history_is_blocked(self):
        self._usd_history()
        with self.assertRaises(ValueError) as ctx:
            self.lim("fxp", 100.0, currency="EUR")
        self.assertIn("BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION", str(ctx.exception))
        self.assertEqual(self.g.preview(provider="fxp", service="s", operation="o", estimated_cost=10.0,
                                        currency="EUR")["code"], "CURRENCY_MISMATCH")

    def test_14_new_provider_without_history_uses_its_own_currency(self):
        self.lim("novo", 100.0, currency="EUR")
        self.assertEqual(self.g.preview(provider="novo", service="s", operation="o", estimated_cost=10.0,
                                        currency="EUR")["code"], "AUTHORIZED")
        with self.assertRaises(ValueError):
            self.lim("novo", 5.0, kind="per_call", currency="USD")     # limites do provedor não misturam moedas

    def test_15_16_migration_with_real_fx_is_recorded_and_sums_never_mix(self):
        self._usd_history()
        for bad in ({"fx_rate": 0.0}, {"fx_source": ""}, {"fx_timestamp": ""}, {"evidence": ""}):
            args = dict(principal=OWNER, provider="fxp", from_currency="USD", to_currency="EUR", fx_rate=0.9,
                        fx_source="BCE", fx_timestamp="2026-09-25T10:00:00Z", evidence="decisão do Owner")
            args.update(bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                self.g.migrate_budget_currency(**args)
        self.g.migrate_budget_currency(principal=OWNER, provider="fxp", from_currency="USD", to_currency="EUR",
                                       fx_rate=0.9, fx_source="BCE", fx_timestamp="2026-09-25T10:00:00Z",
                                       evidence="decisão do Owner")
        with db_module.connect(self.db) as conn:
            m = dict(conn.execute("SELECT * FROM ai_api_currency_migrations").fetchone())
            lim = conn.execute("SELECT amount, currency FROM ai_api_limits WHERE provider='fxp'").fetchone()
        self.assertEqual((m["fx_source"], m["fx_timestamp"], m["actor"]), ("BCE", "2026-09-25T10:00:00Z", "owner"))
        self.assertEqual((lim[0], lim[1]), (90.0, "EUR"))
        # histórico USD 50 entra como EUR 45 (taxa registrada), nunca 1:1
        self.assertEqual(self.g.preview(provider="fxp", service="s", operation="o", estimated_cost=45.0,
                                        currency="EUR")["code"], "AUTHORIZED")
        self.assertEqual(self.g.preview(provider="fxp", service="s", operation="o", estimated_cost=45.01,
                                        currency="EUR")["code"], "LIMIT_EXCEEDED")

    def test_db_upgraded_from_1_0_5_with_currency_already_switched_never_ignores_history(self):
        """Estado de um banco da 1.0.5, onde a troca USD->EUR era ACEITA: limites já em EUR,
        histórico em USD sem migração. Nunca somar 1:1 — e nunca ignorar o histórico."""
        self._usd_history()
        with db_module.connect(self.db) as conn:
            conn.execute("UPDATE ai_api_limits SET currency = 'EUR' WHERE provider = 'fxp'")
        self.assertEqual(self.g.preview(provider="fxp", service="s", operation="o", estimated_cost=10.0,
                                        currency="EUR")["code"], "BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION")

    def test_migration_blocked_with_open_reservation_or_incident(self):
        self.lim("fxq", 100.0)
        self.rsv("fxq")
        with self.assertRaises(ValueError):
            self.g.migrate_budget_currency(principal=OWNER, provider="fxq", from_currency="USD", to_currency="EUR",
                                           fx_rate=0.9, fx_source="BCE", fx_timestamp="t", evidence="e")


# -------------------------------------------- 17–24 invariantes de settle
class SettlementInvariantTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("c", 1000.0)
        self.lim("c", 1.0, kind="unknown_cost_upper_bound")

    def test_17_not_executed_without_cost_releases(self):
        out = self.g.settle(self.rsv("c"), outcome="NOT_EXECUTED", success=False)
        self.assertEqual((out["status"], out["budget_charge"]), ("RELEASED", 0.0))
        self.assertEqual(self.g.open_incidents("c"), [])

    def test_18_21_not_executed_with_real_cost_or_success_is_conflict(self):
        out = self.g.settle(self.rsv("c"), outcome="NOT_EXECUTED", success=False, actual_cost=100.0,
                            actual_currency="USD", cost_confirmed=True)
        self.assertEqual((out["status"], out["cost_status"], out["budget_charge"]),
                         ("HELD_CONFLICT", "SETTLEMENT_STATE_CONFLICT", 100.0), "custo real NUNCA descartado")
        self.assertEqual([i["kind"] for i in self.g.open_incidents("c")], ["SETTLEMENT_STATE_CONFLICT"])
        self.assertEqual(self.g.preview(provider="c", service="s", operation="o", estimated_cost=1.0,
                                        currency="USD")["code"], "COST_INCIDENT_REVIEW_REQUIRED")
        self.lim("c2", 1000.0)
        out2 = self.g.settle(self.rsv("c2"), outcome="NOT_EXECUTED", success=True)
        self.assertEqual(out2["cost_status"], "SETTLEMENT_STATE_CONFLICT", "NOT_EXECUTED com sucesso é contraditório")

    def test_19_20_unknown_without_cost_holds_and_with_cost_preserves_it(self):
        out = self.g.settle(self.rsv("c"), outcome="UNKNOWN", success=False)
        self.assertEqual((out["status"], out["budget_charge"]), ("HELD_UNKNOWN", 1.0))
        out2 = self.g.settle(self.rsv("c"), outcome="UNKNOWN", success=False, actual_cost=100.0, actual_currency="USD",
                             cost_confirmed=True)
        self.assertEqual((out2["budget_charge"], out2["cost_status"]), (100.0, "SETTLEMENT_STATE_CONFLICT"))

    def test_22_23_24_replay_rules_preserved(self):
        rid = self.rsv("c")
        self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=0.5, actual_currency="USD")
        self.assertTrue(self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=0.5,
                                      actual_currency="USD")["already_settled"])
        with self.assertRaises(SettlementConflict):
            self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=5.0, actual_currency="USD")
        with db_module.connect(self.db) as conn:
            conn.execute("UPDATE ai_api_reservations SET settlement_fingerprint = NULL WHERE reservation_id = ?", (rid,))
        with self.assertRaises(SettlementConflict):
            self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=0.5, actual_currency="USD")


# ------------------------------------------ 25–27 regras anteriores preservadas
class PreservedRulesTests(_Base):
    def test_25_concurrency_still_protected(self):
        self.lim("k", 10.0)
        codes, barrier = [], threading.Barrier(8)

        def go():
            barrier.wait()
            codes.append(self.g.authorize(provider="k", service="s", operation="o", estimated_cost=6.0,
                                          currency="USD")["code"])
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(codes.count("AUTHORIZED"), 1)

    def test_26_27_overrun_and_currency_mismatch_still_block(self):
        self.lim("o", 100.0)
        self.g.settle(self.rsv("o"), outcome="EXECUTED", success=True, actual_cost=5.0, actual_currency="USD")
        self.assertEqual([i["kind"] for i in self.g.open_incidents("o")], ["COST_OVERRUN"])
        self.lim("x", 100.0)
        self.g.settle(self.rsv("x"), outcome="EXECUTED", success=True, actual_cost=2.0, actual_currency="EUR")
        self.assertEqual([i["kind"] for i in self.g.open_incidents("x")], ["CURRENCY_MISMATCH_INCIDENT"])
        for p in ("o", "x"):
            self.assertEqual(self.g.preview(provider=p, service="s", operation="o", estimated_cost=1.0,
                                            currency="USD")["code"], "COST_INCIDENT_REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
