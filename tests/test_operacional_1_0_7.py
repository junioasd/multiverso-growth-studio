"""
v1.0.7-OPERATIONAL-CANDIDATE — bloqueadores da auditoria de readiness 1.0.6:
(1) set_limit atômico (uma moeda por provedor); (2) migração sem reprecificação
retroativa; (3) cadeia de migração operacional (materialização por registro);
(4) reconciliação FX com validação matemática. SQLite real; threads reais.
"""
import os
import sqlite3
import tempfile
import threading
import unittest

from core.models import db as db_module
from core.operations import cost_guard, kill_switch
from core.operations.cost_guard import FX_RECONCILIATION_TOLERANCE, ReconciliationConflict, SettlementConflict
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

    def history(self, p, amount=50.0):
        self.lim(p, 100.0)
        rid = self.g.authorize(provider=p, service="s", operation="o", estimated_cost=amount, currency="USD")["reservation_id"]
        self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=amount, actual_currency="USD", cost_confirmed=True)

    def mig(self, p, fr, to, rate, **kw):
        args = dict(FX, **kw)
        return self.g.migrate_budget_currency(principal=OWNER, provider=p, from_currency=fr, to_currency=to, fx_rate=rate,
                                              **args)

    def preview(self, p, amount, cur):
        return self.g.preview(provider=p, service="s", operation="o", estimated_cost=amount, currency=cur)["code"]


# ------------------------------------------------ 1–5 set_limit atômico
class SetLimitAtomicityTests(_Base):
    def _race(self, specs):
        out, barrier = {}, threading.Barrier(len(specs))

        def go(kind, cur):
            barrier.wait()
            try:
                self.lim("conc", 100.0, kind=kind, cur=cur)
                out[kind] = "OK"
            except ValueError as exc:
                out[kind] = "CONFLICT" if "BUDGET_CURRENCY_CONFLICT" in str(exc) else f"OUTRO:{exc}"
        ts = [threading.Thread(target=go, args=s) for s in specs]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return out

    def test_1_concurrent_same_currency_both_succeed(self):
        out = self._race([("provider_total", "USD"), ("per_call", "USD"), ("per_day", "USD")])
        self.assertEqual(set(out.values()), {"OK"})
        self.assertEqual(self.sql("SELECT DISTINCT currency FROM ai_api_limits WHERE provider='conc'"), [("USD",)])

    def test_2_3_concurrent_different_currencies_exactly_one_wins_never_mixed(self):
        for _ in range(5):
            self.sql("DELETE FROM ai_api_limits WHERE provider='conc'")
            out = self._race([("provider_total", "USD"), ("per_call", "EUR"), ("per_day", "GBP")])
            self.assertEqual(sorted(out.values()), ["CONFLICT", "CONFLICT", "OK"], out)
            self.assertEqual(len(self.sql("SELECT DISTINCT currency FROM ai_api_limits WHERE provider='conc'")), 1)

    def test_4_5_crash_during_set_limit_persists_nothing_and_restart_keeps_invariant(self):
        self.sql("CREATE TRIGGER falha BEFORE INSERT ON ai_api_limits BEGIN SELECT RAISE(ABORT, 'crash simulado'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.lim("p", 10.0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_limits WHERE provider='p'"), [(0,)])
        self.sql("DROP TRIGGER falha")
        self.lim("p", 10.0)
        g2 = cost_guard.CostGuard(self.db)                                  # restart
        with self.assertRaises(ValueError):
            g2.set_limit(principal=OWNER, provider="p", kind="per_call", amount=1.0, currency="EUR")


# ------------------------------------------ 6–15 migração materializada
class MigrationTests(_Base):
    def test_6_to_10_first_migration_idempotent_retry_divergent_repeat_and_no_repricing(self):
        self.history("fx")
        first = self.mig("fx", "USD", "EUR", 0.9)
        self.assertTrue(self.mig("fx", "USD", "EUR", 0.9)["already_migrated"], "retry idêntico = idempotente")
        for rate in (0.5, 0.95):
            with self.assertRaises(ValueError) as ctx:
                self.mig("fx", "USD", "EUR", rate)
            self.assertIn("FX_HISTORY_ALREADY_MIGRATED", str(ctx.exception))
        row = self.sql("SELECT converted_amount, converted_currency, fx_migration_id, fx_chain FROM ai_api_usage "
                       "WHERE provider='fx' AND budget_charge != 0")[0]
        self.assertAlmostEqual(row[0], 45.0)
        self.assertEqual((row[1], row[2]), ("EUR", first["migration_id"]))
        self.assertIn("USD->EUR@0.9", row[3])
        self.assertEqual(self.preview("fx", 45.0, "EUR"), "AUTHORIZED")
        self.assertEqual(self.preview("fx", 45.01, "EUR"), "LIMIT_EXCEEDED", "histórico NÃO reprecificado")

    def test_11_12_14_chain_is_composed_deterministically_operational_and_survives_restart(self):
        self.history("ch")
        self.mig("ch", "USD", "EUR", 0.9)
        g2 = cost_guard.CostGuard(self.db)                                 # restart entre migrações
        g2.migrate_budget_currency(principal=OWNER, provider="ch", from_currency="EUR", to_currency="GBP", fx_rate=0.8, **FX)
        amount, cur, chain = self.sql("SELECT converted_amount, converted_currency, fx_chain FROM ai_api_usage "
                                      "WHERE provider='ch' AND budget_charge != 0")[0]
        self.assertAlmostEqual(amount, 36.0)                               # 50 × 0,9 × 0,8
        self.assertEqual(cur, "GBP")
        self.assertIn("USD->EUR@0.9 > ", chain)
        self.assertIn("EUR->GBP@0.8", chain)
        self.assertEqual(self.sql("SELECT amount, currency FROM ai_api_limits WHERE provider='ch'"), [(72.0, "GBP")])
        self.assertEqual(self.preview("ch", 36.0, "GBP"), "AUTHORIZED", "cadeia NÃO cria estado inoperante")
        self.assertEqual(self.preview("ch", 36.01, "GBP"), "LIMIT_EXCEEDED")

    def test_13_divergent_chain_is_conflict_before_any_state_change(self):
        self.history("dv")
        self.mig("dv", "USD", "EUR", 0.9)
        with self.assertRaises(ValueError) as ctx:
            self.mig("dv", "USD", "GBP", 0.7)          # não parte da moeda operacional atual (EUR)
        self.assertIn("FX_MIGRATION_CONFLICT", str(ctx.exception))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_currency_migrations WHERE provider='dv'"), [(1,)])
        self.assertEqual(self.preview("dv", 45.0, "EUR"), "AUTHORIZED")

    def test_15_crash_during_migration_rolls_back_everything(self):
        self.history("cr")
        self.sql("CREATE TRIGGER falha BEFORE UPDATE ON ai_api_limits BEGIN SELECT RAISE(ABORT, 'crash simulado'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.mig("cr", "USD", "EUR", 0.9)
        self.sql("DROP TRIGGER falha")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_currency_migrations WHERE provider='cr'"), [(0,)])
        self.assertEqual(self.sql("SELECT converted_amount FROM ai_api_usage WHERE provider='cr' AND budget_charge != 0"),
                         [(None,)], "histórico não materializado pela metade")
        self.assertEqual(self.preview("cr", 50.0, "USD"), "AUTHORIZED")
        self.mig("cr", "USD", "EUR", 0.9)                                   # retry após o crash funciona

    def test_legacy_1_0_6_migration_fails_closed_until_explicit_materialization(self):
        self.history("lg")
        self.sql("INSERT INTO ai_api_currency_migrations (provider, from_currency, to_currency, fx_rate, fx_source, "
                 "fx_timestamp, evidence, actor, created_at) VALUES ('lg','USD','EUR',0.9,'BCE','t','e','owner',0)")
        self.sql("UPDATE ai_api_limits SET amount = 90, currency = 'EUR' WHERE provider = 'lg'")
        self.assertEqual(self.preview("lg", 1.0, "EUR"), "BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION")
        mid = self.sql("SELECT migration_id FROM ai_api_currency_migrations WHERE provider='lg'")[0][0]
        self.g.materialize_legacy_migration(principal=OWNER, provider="lg", migration_id=mid, evidence="upgrade")
        self.assertEqual(self.preview("lg", 45.0, "EUR"), "AUTHORIZED")
        self.assertEqual(self.preview("lg", 45.01, "EUR"), "LIMIT_EXCEEDED")

    def test_legacy_1_0_6_repeated_migration_is_ambiguous_and_refused(self):
        """Banco vindo da 1.0.6 no estado EXATO da auditoria: USD->EUR registrado 2× (0,9 e 0,5)."""
        self.history("amb")
        for rate in (0.9, 0.5):
            self.sql("INSERT INTO ai_api_currency_migrations (provider, from_currency, to_currency, fx_rate, fx_source, "
                     "fx_timestamp, evidence, actor, created_at) VALUES ('amb','USD','EUR',?,'BCE','t','e','owner',0)", (rate,))
        self.sql("UPDATE ai_api_limits SET amount = 90, currency = 'EUR' WHERE provider = 'amb'")
        mid = self.sql("SELECT MAX(migration_id) FROM ai_api_currency_migrations WHERE provider='amb'")[0][0]
        with self.assertRaises(ValueError) as ctx:
            self.g.materialize_legacy_migration(principal=OWNER, provider="amb", migration_id=mid, evidence="upgrade")
        self.assertIn("FX_MIGRATION_CONFLICT", str(ctx.exception))
        self.assertEqual(self.preview("amb", 1.0, "EUR"), "BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION",
                         "segue bloqueado (fail-closed) — taxa ambígua nunca escolhida implicitamente")

    def test_direct_usage_in_other_currency_is_rejected(self):
        self.lim("u", 100.0)
        with self.assertRaises(ValueError) as ctx:
            self.g.record_usage(provider="u", service="s", operation="o", success=True, currency="EUR", cost=5.0)
        self.assertIn("BUDGET_CURRENCY_CONFLICT", str(ctx.exception))


# ------------------------------------------- 16–27 reconciliação FX
class FxReconciliationTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("m", 1000.0)
        rid = self.g.authorize(provider="m", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=100.0, actual_currency="EUR")
        self.iid = self.g.open_incidents("m")[0]["incident_id"]

    def rec(self, final, rate=0.5, **kw):
        args = dict(evidence="fatura", source="provedor", final_amount=final, final_currency="USD", fx_rate=rate,
                    fx_source="BCE", fx_timestamp="2026-09-25T10:00:00Z")
        args.update(kw)
        return self.g.reconcile_incident(self.iid, principal=OWNER, **args)

    def test_16_17_valid_math_accepted_incoherent_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.rec(1.0)
        self.assertIn("FX_RECONCILIATION_MISMATCH", str(ctx.exception))
        out = self.rec(50.0)
        self.assertEqual(out["status"], "RECONCILED")
        row = self.sql("SELECT original_amount, original_currency, fx_rate, expected_amount FROM ai_api_cost_incidents "
                       "WHERE incident_id = ?", (self.iid,))[0]
        self.assertEqual(row, (100.0, "EUR", 0.5, 50.0))
        self.assertEqual(self.g.summary()["m/USD"]["budget_charged"], 50.0)

    def test_18_19_explicit_fees_and_adjustments_are_part_of_the_math(self):
        with self.assertRaises(ValueError):
            self.rec(53.0)                                        # sem declarar a taxa de 3,00
        self.assertEqual(self.rec(51.0, fees=3.0, adjustments=-2.0)["status"], "RECONCILED")
        self.assertEqual(self.sql("SELECT fees, adjustments FROM ai_api_cost_incidents WHERE incident_id = ?",
                                  (self.iid,))[0], (3.0, -2.0))

    def test_20_to_24_fx_field_validation(self):
        for kw in ({"fx_source": ""}, {"fx_timestamp": ""}, {"rate": 0.0}, {"rate": -0.5}, {"rate": float("nan")},
                   {"rate": float("inf")}, {"fees": -1.0}, {"adjustments": float("nan")}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self.rec(50.0, **kw)

    def test_25_tolerance_is_well_defined(self):
        self.assertEqual(FX_RECONCILIATION_TOLERANCE, 0.01)
        with self.assertRaises(ValueError):
            self.rec(50.02)
        self.assertEqual(self.rec(50.009)["status"], "RECONCILED")

    def test_26_27_identical_retry_idempotent_divergent_conflict(self):
        self.rec(50.0)
        self.assertTrue(self.rec(50.0)["already_reconciled"])
        with self.assertRaises(ReconciliationConflict):
            self.rec(51.0, fees=1.0)
        self.assertEqual(self.g.summary()["m/USD"]["budget_charged"], 50.0)

    def test_legacy_incident_without_structured_original_cannot_be_fx_reconciled(self):
        self.sql("UPDATE ai_api_cost_incidents SET original_amount = NULL WHERE incident_id = ?", (self.iid,))
        with self.assertRaises(ValueError) as ctx:
            self.rec(50.0)
        self.assertIn("FX_RECONCILIATION_MISMATCH", str(ctx.exception))
        self.assertEqual(self.g.reconcile_incident(self.iid, principal=OWNER, evidence="decisão", source="write-off",
                                                   write_off=True)["reconciliation"], "WRITE_OFF")


# ---------------------------------------------- 28–32 regras preservadas
class PreservedRulesTests(_Base):
    def test_28_to_32(self):
        self.lim("c", 1000.0)
        self.lim("c", 1.0, kind="unknown_cost_upper_bound")
        rid = self.g.authorize(provider="c", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        self.assertEqual(self.g.settle(rid, outcome="NOT_EXECUTED", success=False, actual_cost=5.0,
                                       actual_currency="USD")["cost_status"], "SETTLEMENT_STATE_CONFLICT")
        self.lim("r", 1000.0)
        rid2 = self.g.authorize(provider="r", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        self.g.settle(rid2, outcome="EXECUTED", success=True, actual_cost=1.0, actual_currency="USD")
        with self.assertRaises(SettlementConflict):
            self.g.settle(rid2, outcome="EXECUTED", success=True, actual_cost=9.0, actual_currency="USD")
        with self.assertRaises(ValueError):
            self.lim("z", 0.0, kind="unknown_cost_upper_bound")
        self.lim("o", 1000.0)
        rid3 = self.g.authorize(provider="o", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
        self.g.settle(rid3, outcome="EXECUTED", success=True, actual_cost=7.0, actual_currency="USD")
        self.assertEqual([i["kind"] for i in self.g.open_incidents("o")], ["COST_OVERRUN"])


if __name__ == "__main__":
    unittest.main()
