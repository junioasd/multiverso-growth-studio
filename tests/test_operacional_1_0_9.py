"""
v1.0.9-OPERATIONAL-CANDIDATE — bloqueadores da auditoria de readiness 1.0.8:
(1) recuperação de histórico em moeda retirada; (2) retirada pela moeda EFETIVA;
(3) snapshot imutável da política no uso desconhecido; (4) correlação reserva<->uso
sem adivinhação. SQLite real; threads reais; triggers de crash.
"""
# v1.0.10 (auditoria 1.0.9, B3): uso SEM reserva passou a exigir `usage_event_id` (identidade idempotente
# do evento). Expectativa antiga: registrar sem identidade — era o próprio defeito (retry duplicava custo).
# Cada chamada recebeu um id DISTINTO, preservando a intenção original (eventos distintos seguem distintos).
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from core.models import db as db_module
from core.operations import cost_guard, kill_switch
from core.operations.cost_guard import ReconciliationConflict, SettlementConflict
from core.security.auth import Principal, Role

OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
FX = dict(fx_source="BCE", fx_timestamp="2026-09-25T10:00:00Z", evidence="decisão do Owner")


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

    def tearDown(self):
        db_module.remove_db_file(self.db)

    def sql(self, q, params=()):
        c = sqlite3.connect(self.db)
        try:
            out = c.execute(q, params).fetchall()
            c.commit()
            return out
        finally:
            c.close()

    def lim(self, p, amount, kind="provider_total", cur="USD"):
        self.g.set_limit(principal=OWNER, provider=p, kind=kind, amount=amount, currency=cur)

    def preview(self, p, amount, cur="USD"):
        return self.g.preview(provider=p, service="s", operation="o", estimated_cost=amount, currency=cur)["code"]

    def legacy(self, p="lz", code="ZZZ", spent=5.0):
        self.sql("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) "
                 "VALUES (?, 'provider_total', '*', 100, ?, 0, 'legado')", (p, code))
        self.sql("INSERT INTO ai_api_usage (provider, service, operation, created_at, cost, cost_status, currency, success, "
                 "budget_charge) VALUES (?, 's', 'o', 0, ?, 'CONFIRMED', ?, 1, ?)", (p, spent, code, spent))

    def rec_legacy(self, p="lz", code="ZZZ", rate=2.0, declared=10.0, **kw):
        args = dict(principal=OWNER, provider=p, legacy_currency=code, target_currency="USD", fx_rate=rate,
                    declared_converted_total=declared, **FX)
        args.update(kw)
        return self.g.reconcile_unsupported_currency_history(**args)


# ------------------------------------------- 1–10 moeda legada
class LegacyCurrencyRecoveryTests(_Base):
    def test_1_2_retire_without_history_but_never_with_history(self):
        self.sql("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) "
                 "VALUES ('vazio', 'provider_total', '*', 100, 'ZZZ', 0, 'legado')")
        self.assertEqual(self.g.retire_unsupported_currency_limits(principal=OWNER, provider="vazio", evidence="e")["retired"],
                         ["ZZZ"])
        self.legacy()
        with self.assertRaises(ValueError) as ctx:
            self.g.retire_unsupported_currency_limits(principal=OWNER, provider="lz", evidence="e")
        self.assertIn("CURRENCY_RETIREMENT_REQUIRES_RECONCILIATION", str(ctx.exception))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_limits WHERE provider='lz'"), [(1,)])

    def test_3_4_public_flow_reconciles_legacy_to_usd_and_restores_operation(self):
        self.legacy()
        out = self.rec_legacy(rate=2.0, declared=10.0)                    # 5 ZZZ × 2 = 10 USD
        self.assertEqual(out["converted_total"], 10.0)
        self.assertEqual(self.sql("SELECT converted_amount, converted_currency FROM ai_api_usage WHERE provider='lz'"),
                         [(10.0, "USD")])
        self.assertEqual(self.sql("SELECT amount, currency FROM ai_api_limits WHERE provider='lz'"), [(200.0, "USD")])
        self.assertEqual(self.preview("lz", 190.0), "AUTHORIZED")
        self.assertEqual(self.preview("lz", 190.01), "LIMIT_EXCEEDED")
        audit = self.sql("SELECT kind, original_currency, original_amount, fx_rate, fx_source FROM ai_api_cost_incidents "
                         "WHERE provider='lz'")
        self.assertEqual(audit, [("LEGACY_CURRENCY_RECONCILED", "ZZZ", 5.0, 2.0, "BCE")])

    def test_5_6_invalid_fx_and_incoherent_math_rejected(self):
        self.legacy()
        for kw in ({"rate": 0.0}, {"rate": -1.0}, {"rate": float("nan")}, {"fx_source": ""}, {"fx_timestamp": ""},
                   {"evidence": ""}, {"target_currency": "ZZZ"}, {"legacy_currency": "USD"}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self.rec_legacy(**kw)
        with self.assertRaises(ValueError) as ctx:
            self.rec_legacy(rate=2.0, declared=1.0)                        # 5 × 2 = 10 ≠ 1
        self.assertIn("FX_RECONCILIATION_MISMATCH", str(ctx.exception))
        with self.assertRaises(ValueError):
            self.rec_legacy(rate=2.0, declared=5.0, adjustments=-5.0)       # ajuste líquido negativo recusado
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_currency_migrations"), [(0,)])

    def test_7_crash_during_legacy_reconciliation_keeps_state_intact(self):
        self.legacy()
        self.sql("CREATE TRIGGER falha BEFORE UPDATE ON ai_api_limits BEGIN SELECT RAISE(ABORT, 'crash simulado'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.rec_legacy()
        self.sql("DROP TRIGGER falha")
        self.assertEqual(self.sql("SELECT converted_amount FROM ai_api_usage WHERE provider='lz'"), [(None,)])
        self.assertEqual(self.sql("SELECT currency FROM ai_api_limits WHERE provider='lz'"), [("ZZZ",)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_currency_migrations"), [(0,)])
        self.assertEqual(self.rec_legacy()["converted_total"], 10.0)

    def test_8_9_10_restart_identical_retry_idempotent_divergent_conflict(self):
        self.legacy()
        self.rec_legacy()
        g2 = cost_guard.CostGuard(self.db)                                   # restart
        self.assertTrue(g2.reconcile_unsupported_currency_history(
            principal=OWNER, provider="lz", legacy_currency="ZZZ", target_currency="USD", fx_rate=2.0,
            declared_converted_total=10.0, **FX)["already_reconciled"])
        with self.assertRaises(ValueError) as ctx:
            self.rec_legacy(rate=3.0, declared=15.0)
        self.assertIn("FX_HISTORY_ALREADY_MIGRATED", str(ctx.exception))
        self.assertEqual(self.sql("SELECT converted_amount FROM ai_api_usage WHERE provider='lz'"), [(10.0,)])


# ------------------------------------- 11–14 moeda efetiva na retirada
class EffectiveCurrencyRetirementTests(_Base):
    def test_11_to_14_usd_to_jpy_then_jpy_retired_is_blocked_until_reconciled(self):
        with mock.patch.dict(os.environ, {"MGS_EXTRA_CURRENCIES": "JPY"}):
            self.lim("j", 100.0)
            rid = self.g.authorize(provider="j", service="s", operation="o", estimated_cost=10.0, currency="USD")["reservation_id"]
            self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=10.0, actual_currency="USD", cost_confirmed=True)
            self.g.migrate_budget_currency(principal=OWNER, provider="j", from_currency="USD", to_currency="JPY",
                                           fx_rate=150.0, **FX)
        # JPY sai da política (restart sem MGS_EXTRA_CURRENCIES)
        with self.assertRaises(ValueError) as ctx:
            self.g.retire_unsupported_currency_limits(principal=OWNER, provider="j", evidence="e")
        self.assertIn("CURRENCY_RETIREMENT_REQUIRES_RECONCILIATION", str(ctx.exception))
        self.assertEqual(self.sql("SELECT currency FROM ai_api_limits WHERE provider='j'"), [("JPY",)],
                         "limite NÃO apagado com histórico efetivo em JPY")
        # recuperação: JPY efetivo -> USD (cadeia continua auditável)
        self.g.reconcile_unsupported_currency_history(principal=OWNER, provider="j", legacy_currency="JPY",
                                                      target_currency="USD", fx_rate=0.0067,
                                                      declared_converted_total=10.05, **FX)
        amount, cur, chain = self.sql("SELECT converted_amount, converted_currency, fx_chain FROM ai_api_usage "
                                      "WHERE provider='j' AND budget_charge != 0")[0]
        self.assertAlmostEqual(amount, 10.05)
        self.assertEqual(cur, "USD")
        self.assertIn("USD->JPY@150.0 > ", chain)
        self.assertIn("JPY->USD@0.0067", chain)
        self.assertEqual(self.sql("SELECT currency FROM ai_api_limits WHERE provider='j'"), [("USD",)])
        self.assertNotEqual(self.preview("j", 1.0), "PAID_PROVIDER_NOT_AUTHORIZED", "provedor volta a operar")

    def test_effective_summary_matches_budget_calculation(self):
        self.lim("es", 100.0)
        rid = self.g.authorize(provider="es", service="s", operation="o", estimated_cost=10.0, currency="USD")["reservation_id"]
        self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=10.0, actual_currency="USD", cost_confirmed=True)
        self.g.migrate_budget_currency(principal=OWNER, provider="es", from_currency="USD", to_currency="EUR", fx_rate=0.9, **FX)
        self.assertEqual(self.g.summary(effective=True)["es/EUR"]["budget_charged"], 9.0)
        self.assertEqual(self.g.summary()["es/USD"]["budget_charged"], 10.0, "resumo nativo preservado (compatível)")


# ---------------------------------------- 15–20 snapshot da política
class PolicySnapshotTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("u", 1000.0)
        self.lim("u", 100.0, kind="unknown_cost_upper_bound")

    def test_15_to_18_snapshot_frozen_at_event_and_new_event_uses_new_policy(self):
        old = self.g.record_usage(usage_event_id="ev-1_0_9-174", provider="u", service="s", operation="o", success=True)
        snap = json.loads(self.sql("SELECT policy_snapshot FROM ai_api_cost_incidents WHERE incident_id=?",
                                   (old["incident_id"],))[0][0])
        self.assertEqual((snap["unknown_cost_upper_bound"], snap["currency"], snap["service"]), (100.0, ["USD"], "s"))
        self.assertTrue(snap["policy_version"])
        self.lim("u", 1.0, kind="unknown_cost_upper_bound")                 # política muda DEPOIS do evento
        new = self.g.record_usage(usage_event_id="ev-1_0_9-180", provider="u", service="s", operation="o", success=True)
        self.g.reconcile_usage(old["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        self.g.reconcile_usage(new["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        charges = dict(self.sql("SELECT usage_id, budget_charge FROM ai_api_usage WHERE usage_id IN (?, ?)",
                                (old["usage_id"], new["usage_id"])))
        self.assertEqual((charges[old["usage_id"]], charges[new["usage_id"]]), (100.0, 1.0))

    def test_19_legacy_event_without_snapshot_goes_to_review(self):
        old = self.g.record_usage(usage_event_id="ev-1_0_9-188", provider="u", service="s", operation="o", success=True)
        self.sql("UPDATE ai_api_cost_incidents SET policy_snapshot = NULL, snapshot_upper_bound = NULL WHERE incident_id=?",
                 (old["incident_id"],))
        with self.assertRaises(ValueError) as ctx:
            self.g.reconcile_usage(old["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        self.assertIn("LEGACY_SNAPSHOT_MISSING", str(ctx.exception))
        self.g.reconcile_usage(old["usage_id"], principal=OWNER, evidence="fatura", source="provedor", final_amount=7.0,
                               final_currency="USD")

    def test_20_snapshot_persists_after_restart(self):
        old = self.g.record_usage(usage_event_id="ev-1_0_9-198", provider="u", service="s", operation="o", success=True)
        self.lim("u", 1.0, kind="unknown_cost_upper_bound")
        g2 = cost_guard.CostGuard(self.db)
        g2.reconcile_usage(old["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        self.assertEqual(self.sql("SELECT budget_charge FROM ai_api_usage WHERE usage_id=?", (old["usage_id"],)), [(100.0,)])


# ---------------------------------------- 21–31 correlação reserva<->uso
class ReservationCorrelationTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("p", 1000.0)

    def two(self):
        a = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=10.0, currency="USD",
                             correlation_id="call-A")["reservation_id"]
        b = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=100.0, currency="USD",
                             correlation_id="call-B")["reservation_id"]
        return a, b

    def status(self, rid):
        return self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (rid,))[0][0]

    def test_21_to_24_out_of_order_results_settle_the_right_reservations(self):
        a, b = self.two()
        self.assertNotEqual(a, b)
        self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=100.0, currency="USD",
                            reservation_id=b)                              # B chega ANTES de A
        self.assertEqual((self.status(a), self.status(b)), ("RESERVED", "COMMITTED"))
        self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=9.0, currency="USD",
                            correlation_id="call-A")
        self.assertEqual(self.status(a), "COMMITTED")
        self.assertEqual(self.g.open_incidents("p"), [], "nenhum overrun artificial")

    def test_25_single_candidate_without_id_is_unambiguous(self):
        a = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=10.0, currency="USD")["reservation_id"]
        self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=10.0, currency="USD")
        self.assertEqual(self.status(a), "COMMITTED")

    def test_26_27_28_multiple_candidates_without_id_fail_closed_changing_nothing(self):
        a, b = self.two()
        before = self.sql("SELECT reservation_id, status FROM ai_api_reservations ORDER BY reservation_id")
        with self.assertRaises(ValueError) as ctx:
            self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=100.0, currency="USD")
        self.assertIn("AMBIGUOUS_RESERVATION_MATCH", str(ctx.exception))
        self.assertEqual(self.sql("SELECT reservation_id, status FROM ai_api_reservations ORDER BY reservation_id"), before)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_usage WHERE provider='p'"), [(0,)])
        self.assertEqual(self.g.open_incidents("p"), [])

    def test_concurrent_results_with_ids_never_cross(self):
        rids = {f"call-{i}": self.g.authorize(provider="p", service="s", operation="o", estimated_cost=float(i + 1),
                                              currency="USD", correlation_id=f"call-{i}")["reservation_id"]
                for i in range(6)}
        barrier = threading.Barrier(6)

        def deliver(i):
            barrier.wait()
            self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=float(i + 1), currency="USD",
                                correlation_id=f"call-{i}")
        ts = [threading.Thread(target=deliver, args=(i,)) for i in reversed(range(6))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        rows = dict(self.sql("SELECT reservation_id, budget_charge FROM ai_api_usage WHERE provider='p'"))
        self.assertEqual({rid: rows[rid] for rid in rids.values()},
                         {rids[f"call-{i}"]: float(i + 1) for i in range(6)}, "cada custo na SUA reserva")
        self.assertEqual(self.g.open_incidents("p"), [])

    def test_29_30_31_correlation_persists_restart_retry_idempotent_divergent_conflict(self):
        a, _ = self.two()
        g2 = cost_guard.CostGuard(self.db)                                   # restart
        self.assertTrue(g2.authorize(provider="p", service="s", operation="o", estimated_cost=10.0, currency="USD",
                                     correlation_id="call-A")["already_reserved"])
        with self.assertRaises(ValueError):
            g2.authorize(provider="p", service="outro", operation="o", estimated_cost=10.0, currency="USD",
                         correlation_id="call-A")
        g2.record_usage(provider="p", service="s", operation="o", success=True, cost=9.0, currency="USD",
                        correlation_id="call-A")
        g2.record_usage(provider="p", service="s", operation="o", success=True, cost=9.0, currency="USD",
                        correlation_id="call-A")                          # retry idêntico
        self.assertEqual(self.sql("SELECT COUNT(*), SUM(budget_charge) FROM ai_api_usage WHERE reservation_id=?", (a,)),
                         [(1, 9.0)], "retry idêntico NÃO cobra duas vezes")
        with self.assertRaises(SettlementConflict):
            g2.record_usage(provider="p", service="s", operation="o", success=True, cost=50.0, currency="USD",
                            correlation_id="call-A")


# ---------------------------------------- 32–39 regras preservadas
class PreservedRulesTests(_Base):
    def test_32_to_39(self):
        self.assertFalse(cost_guard.valid_currency("ZZZ"))
        self.lim("big", 1e12)
        with self.assertRaises(ValueError):
            self.g.migrate_budget_currency(principal=OWNER, provider="big", from_currency="USD", to_currency="EUR",
                                           fx_rate=1e12, **FX)
        self.lim("w", 1000.0)
        rid = self.g.authorize(provider="w", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        self.g.settle(rid, outcome="NOT_EXECUTED", success=False, actual_cost=100.0, actual_currency="USD")
        self.g.reconcile_incident(self.g.open_incidents("w")[0]["incident_id"], principal=OWNER, evidence="e",
                                  source="write-off", write_off=True)
        self.assertEqual(self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (rid,)), [("WRITTEN_OFF",)])
        self.lim("u", 100.0)
        u = self.g.record_usage(usage_event_id="ev-1_0_9-299", provider="u", service="s", operation="o", success=True)
        self.g.reconcile_usage(u["usage_id"], principal=OWNER, evidence="f", source="p", final_amount=3.0, final_currency="USD")
        self.lim("h", 100.0)
        r2 = self.g.authorize(provider="h", service="s", operation="o", estimated_cost=50.0, currency="USD")["reservation_id"]
        self.g.settle(r2, outcome="EXECUTED", success=True, actual_cost=50.0, actual_currency="USD", cost_confirmed=True)
        self.g.migrate_budget_currency(principal=OWNER, provider="h", from_currency="USD", to_currency="EUR", fx_rate=0.9, **FX)
        with self.assertRaises(ValueError):
            self.g.migrate_budget_currency(principal=OWNER, provider="h", from_currency="USD", to_currency="EUR",
                                           fx_rate=0.5, **FX)
        self.g.migrate_budget_currency(principal=OWNER, provider="h", from_currency="EUR", to_currency="GBP", fx_rate=0.8, **FX)
        self.assertEqual(self.preview("h", 36.0, "GBP"), "AUTHORIZED")
        self.lim("m", 1000.0)
        r3 = self.g.authorize(provider="m", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        self.g.settle(r3, outcome="EXECUTED", success=True, actual_cost=100.0, actual_currency="EUR")
        with self.assertRaises(ValueError):
            self.g.reconcile_incident(self.g.open_incidents("m")[0]["incident_id"], principal=OWNER, evidence="e",
                                      source="s", final_amount=1.0, final_currency="USD", fx_rate=0.5, fx_source="BCE",
                                      fx_timestamp="t")
        out, barrier = {}, threading.Barrier(2)

        def go(kind, cur):
            barrier.wait()
            try:
                self.lim("conc", 10.0, kind=kind, cur=cur)
                out[kind] = "OK"
            except ValueError:
                out[kind] = "CONFLICT"
        ts = [threading.Thread(target=go, args=a) for a in (("provider_total", "USD"), ("per_call", "EUR"))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(out.values()), ["CONFLICT", "OK"])


if __name__ == "__main__":
    unittest.main()
