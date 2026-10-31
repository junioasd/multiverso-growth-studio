"""
v1.0.8-OPERATIONAL-CANDIDATE — bloqueadores da auditoria de readiness 1.0.7:
(1) política oficial de moedas (não só regex); (2) valores RESULTANTES de migração
dentro de MAX_MONEY; (3) write-off encerra a reserva (WRITTEN_OFF) atomicamente;
(4) uso sem custo sempre tem objeto reconciliável. SQLite real; threads reais.
"""
# v1.0.10 (auditoria 1.0.9, B3): uso SEM reserva passou a exigir `usage_event_id` (identidade idempotente
# do evento). Expectativa antiga: registrar sem identidade — era o próprio defeito (retry duplicava custo).
# Cada chamada recebeu um id DISTINTO, preservando a intenção original (eventos distintos seguem distintos).
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from core.models import db as db_module
from core.operations import cost_guard, kill_switch
from core.operations.cost_guard import MAX_MONEY, ReconciliationConflict, SettlementConflict
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

    def rsv(self, p, amount=1.0, cur="USD"):
        return self.g.authorize(provider=p, service="s", operation="o", estimated_cost=amount, currency=cur)["reservation_id"]

    def preview(self, p, amount, cur="USD"):
        return self.g.preview(provider=p, service="s", operation="o", estimated_cost=amount, currency=cur)["code"]


# -------------------------------------------------------- 1–7 moedas
class CurrencyPolicyTests(_Base):
    def test_1_2_3_supported_accepted_unknown_and_malformed_rejected(self):
        for code in ("BRL", "USD", "EUR", "GBP"):
            self.assertTrue(cost_guard.valid_currency(code), code)
        for code in ("ZZZ", "XYZ", "ABC", "US", "USDD", "usd", "", None, 840, "JPY"):
            self.assertFalse(cost_guard.valid_currency(code), repr(code))
        with self.assertRaises(ValueError) as ctx:
            self.lim("z", 100.0, cur="ZZZ")
        self.assertIn("UNSUPPORTED_CURRENCY", str(ctx.exception))
        self.assertEqual(self.preview("z", 1.0, "ZZZ"), "INVALID_CURRENCY")

    def test_owner_extension_only_with_real_iso_codes(self):
        with mock.patch.dict(os.environ, {"MGS_EXTRA_CURRENCIES": "JPY,ZZZ,QQQ"}):
            self.assertTrue(cost_guard.valid_currency("JPY"))
            self.assertFalse(cost_guard.valid_currency("ZZZ"), "extensão não inventa moeda")
            self.assertFalse(cost_guard.valid_currency("QQQ"))

    def test_4_legacy_unknown_currency_fails_closed_and_has_review_path(self):
        self.sql("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) "
                 "VALUES ('leg', 'provider_total', '*', 100, 'ZZZ', 0, 'legado')")
        self.assertEqual(self.preview("leg", 1.0, "ZZZ"), "INVALID_CURRENCY")
        self.assertEqual(self.preview("leg", 1.0, "USD"), "CURRENCY_MISMATCH")
        out = self.g.retire_unsupported_currency_limits(principal=OWNER, provider="leg", evidence="typo do legado")
        self.assertEqual(out["retired"], ["ZZZ"])
        self.lim("leg", 100.0)
        self.assertEqual(self.preview("leg", 1.0), "AUTHORIZED")

    def test_legacy_unknown_currency_with_financial_history_requires_reconciliation(self):
        self.sql("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) "
                 "VALUES ('legh', 'provider_total', '*', 100, 'ZZZ', 0, 'legado')")
        self.sql("INSERT INTO ai_api_usage (provider, service, operation, created_at, cost, cost_status, currency, success, "
                 "budget_charge) VALUES ('legh', 's', 'o', 0, 5, 'CONFIRMED', 'ZZZ', 1, 5)")
        with self.assertRaises(ValueError) as ctx:
            self.g.retire_unsupported_currency_limits(principal=OWNER, provider="legh", evidence="x")
        # v1.0.9 (auditoria 1.0.8): o código deste caso passou a ser
        # CURRENCY_RETIREMENT_REQUIRES_RECONCILIATION (exigido pelo comando); intenção idêntica.
        self.assertIn("CURRENCY_RETIREMENT_REQUIRES_RECONCILIATION", str(ctx.exception))

    def test_5_6_7_unsupported_currency_never_enters_limit_settlement_or_migration(self):
        self.lim("p", 100.0)
        with self.assertRaises(ValueError):
            self.lim("p", 5.0, kind="per_call", cur="ZZZ")
        rid = self.rsv("p")
        out = self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=1.0, actual_currency="ZZZ")
        self.assertEqual(out["cost_status"], "CURRENCY_MISMATCH", "moeda reportada inválida vai para incidente")
        with self.assertRaises(ValueError):
            self.g.migrate_budget_currency(principal=OWNER, provider="p", from_currency="USD", to_currency="ZZZ",
                                           fx_rate=1.0, **FX)
        with self.assertRaises(ValueError):
            self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=1.0, currency="ZZZ")


# ---------------------------------------------------- 8–13 MAX_MONEY
class MaxMoneyTests(_Base):
    def _hist(self, p, limit, spent):
        self.lim(p, limit)
        rid = self.rsv(p, spent)
        self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=spent, actual_currency="USD", cost_confirmed=True)

    def test_8_9_within_and_exactly_at_the_limit(self):
        self._hist("ok", 1e11, 10.0)
        self.g.migrate_budget_currency(principal=OWNER, provider="ok", from_currency="USD", to_currency="EUR", fx_rate=10.0, **FX)
        self.assertEqual(self.sql("SELECT amount FROM ai_api_limits WHERE provider='ok'"), [(1e12,)], "exatamente MAX_MONEY é aceito")

    def test_10_11_12_above_limit_aborts_everything(self):
        self._hist("big", 1e12, 50.0)
        before = (self.sql("SELECT amount, currency FROM ai_api_limits WHERE provider='big'"),
                  self.sql("SELECT converted_amount FROM ai_api_usage WHERE provider='big' AND budget_charge != 0"))
        with self.assertRaises(ValueError) as ctx:
            self.g.migrate_budget_currency(principal=OWNER, provider="big", from_currency="USD", to_currency="EUR",
                                           fx_rate=1e12, **FX)
        self.assertIn("FX_CONVERSION_OUT_OF_RANGE", str(ctx.exception))
        after = (self.sql("SELECT amount, currency FROM ai_api_limits WHERE provider='big'"),
                 self.sql("SELECT converted_amount FROM ai_api_usage WHERE provider='big' AND budget_charge != 0"))
        self.assertEqual(before, after, "nenhum registro alterado")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_currency_migrations WHERE provider='big'"), [(0,)])

    def test_13_underflow_and_database_defense(self):
        self._hist("tiny", 100.0, 1e-300)
        with self.assertRaises(ValueError):
            self.g.migrate_budget_currency(principal=OWNER, provider="tiny", from_currency="USD", to_currency="EUR",
                                           fx_rate=1e-300, **FX)
        for sql in ("UPDATE ai_api_limits SET amount = 1e24 WHERE provider='tiny'",
                    "UPDATE ai_api_usage SET converted_amount = 1e13 WHERE provider='tiny'"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.sql(sql)

    def test_legacy_out_of_range_limit_never_governs(self):
        self.lim("lg", 100.0)
        c = sqlite3.connect(self.db)
        try:   # valor fora da faixa gravado por versão anterior (antes dos triggers novos)
            c.execute("DROP TRIGGER trg_money_v108_ai_api_limits_update")
            c.execute("UPDATE ai_api_limits SET amount = 1e24 WHERE provider='lg'")
            c.commit()
        finally:
            c.close()
        self.assertEqual(self.preview("lg", 1.0), "MONETARY_LIMIT_EXCEEDED")


# ------------------------------------------------ 14–21 write-off
class WriteOffTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("w", 1000.0)
        self.rid = self.rsv("w")
        self.g.settle(self.rid, outcome="NOT_EXECUTED", success=False, actual_cost=100.0, actual_currency="USD",
                      cost_confirmed=True)
        self.iid = self.g.open_incidents("w")[0]["incident_id"]

    def wo(self, source="write-off"):
        return self.g.reconcile_incident(self.iid, principal=OWNER, evidence="decisão do Owner", source=source,
                                         write_off=True)

    def test_16_pending_conflict_reservation_is_visible(self):
        self.assertIn(self.rid, [r["reservation_id"] for r in self.g.open_reservations()])

    def test_14_15_17_write_off_closes_reservation_keeps_charge_unblocks_migration(self):
        self.wo()
        self.assertEqual(self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (self.rid,)), [("WRITTEN_OFF",)])
        self.assertNotIn(self.rid, [r["reservation_id"] for r in self.g.open_reservations()])
        self.assertEqual(self.g.summary()["w/USD"]["budget_charged"], 100.0, "cobrança conservadora mantida")
        self.g.migrate_budget_currency(principal=OWNER, provider="w", from_currency="USD", to_currency="EUR", fx_rate=0.9, **FX)

    def test_18_19_21_retry_identical_idempotent_divergent_conflict_restart(self):
        self.wo()
        g2 = cost_guard.CostGuard(self.db)                                  # restart
        self.assertTrue(g2.reconcile_incident(self.iid, principal=OWNER, evidence="decisão do Owner", source="write-off",
                                              write_off=True)["already_reconciled"])
        with self.assertRaises(ReconciliationConflict):
            g2.reconcile_incident(self.iid, principal=OWNER, evidence="e", source="s", final_amount=5.0, final_currency="USD")
        self.assertEqual(self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (self.rid,)), [("WRITTEN_OFF",)])

    def test_20_crash_during_write_off_keeps_everything_consistent(self):
        self.sql("CREATE TRIGGER falha BEFORE UPDATE OF status ON ai_api_cost_incidents "
                 "BEGIN SELECT RAISE(ABORT, 'crash simulado'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.wo()
        self.assertEqual(self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (self.rid,)),
                         [("HELD_CONFLICT",)], "reserva NÃO fechada pela metade")
        self.assertEqual(self.sql("SELECT cost_status FROM ai_api_usage WHERE reservation_id=?", (self.rid,)),
                         [("SETTLEMENT_STATE_CONFLICT",)])
        self.assertEqual(len(self.g.open_incidents("w")), 1)
        self.sql("DROP TRIGGER falha")
        self.assertEqual(self.wo()["reconciliation"], "WRITE_OFF")

    def test_write_off_of_never_settled_reservation(self):
        self.lim("sf", 100.0)
        rid = self.rsv("sf", 3.0)
        inc = self.g.open_incident(provider="sf", kind="SETTLEMENT_FAILED", reservation_id=rid, detail="db travado")
        self.g.reconcile_incident(inc, principal=OWNER, evidence="e", source="write-off", write_off=True)
        self.assertEqual(self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (rid,)), [("WRITTEN_OFF",)])
        self.assertEqual(self.sql("SELECT budget_charge, cost_status FROM ai_api_usage WHERE reservation_id=?", (rid,)),
                         [(3.0, "WRITTEN_OFF")], "reserva vira cobrança; nunca some do ledger")


# ------------------------------------------- 22–30 uso sem custo
class UnknownUsageTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("u", 100.0)

    def test_22_known_cost_usage_works_without_incident(self):
        out = self.g.record_usage(usage_event_id="ev-1_0_8-219", provider="u", service="s", operation="o", success=True, cost=2.0, currency="USD")
        self.assertIsNone(out["incident_id"])
        self.assertEqual(self.g.open_incidents("u"), [])

    def test_23_to_29_unknown_usage_is_reconcilable_blocks_and_releases(self):
        out = self.g.record_usage(usage_event_id="ev-1_0_8-224", provider="u", service="s", operation="o", success=True)
        self.assertIsNotNone(out["incident_id"])
        self.assertEqual(self.sql("SELECT usage_id, kind FROM ai_api_cost_incidents WHERE incident_id=?", (out["incident_id"],)),
                         [(out["usage_id"], "USAGE_COST_UNKNOWN")], "identidade persistente vinculada ao usage_id")
        self.assertEqual(self.preview("u", 1.0), "COST_INCIDENT_REVIEW_REQUIRED")
        with self.assertRaises(ValueError):
            self.g.resolve_incident(out["incident_id"], principal=OWNER, evidence="só status")
        self.g.reconcile_usage(out["usage_id"], principal=OWNER, evidence="fatura", source="provedor", final_amount=4.0,
                               final_currency="USD")
        self.assertEqual(self.preview("u", 1.0), "AUTHORIZED")
        self.assertEqual(self.g.summary()["u/USD"]["budget_charged"], 4.0)
        self.assertTrue(self.g.reconcile_usage(out["usage_id"], principal=OWNER, evidence="fatura", source="provedor",
                                               final_amount=4.0, final_currency="USD")["already_reconciled"])
        with self.assertRaises(ReconciliationConflict):
            self.g.reconcile_usage(out["usage_id"], principal=OWNER, evidence="fatura", source="provedor",
                                   final_amount=9.0, final_currency="USD")

    def test_write_off_of_unknown_usage_requires_trusted_bound(self):
        # v1.0.9 (auditoria 1.0.8, B3): expectativa ANTIGA — teto definido DEPOIS do evento
        # valia no write-off — era o próprio defeito (política atual reescrevendo o passado).
        # NOVA: vale o teto CONGELADO no evento. Intenção preservada: write-off exige teto
        # confiável e nunca presume zero.
        out = self.g.record_usage(usage_event_id="ev-1_0_8-246", provider="u", service="s", operation="o", success=True)
        with self.assertRaises(ValueError):
            self.g.reconcile_usage(out["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        self.lim("u", 2.5, kind="unknown_cost_upper_bound")
        with self.assertRaises(ValueError):                 # teto posterior NÃO se aplica ao evento antigo
            self.g.reconcile_usage(out["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        self.g.reconcile_usage(out["usage_id"], principal=OWNER, evidence="fatura", source="provedor",
                               final_amount=1.0, final_currency="USD")
        novo = self.g.record_usage(usage_event_id="ev-1_0_8-254", provider="u", service="s", operation="o", success=True)   # evento com teto vigente
        self.g.reconcile_usage(novo["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        self.assertEqual(self.sql("SELECT budget_charge FROM ai_api_usage WHERE usage_id = ?", (novo["usage_id"],)),
                         [(2.5,)], "write-off cobra o teto vigente NO EVENTO, nunca zero")

    def test_30_upgrade_backfill_gives_every_orphan_a_reconcilable_object(self):
        self.sql("INSERT INTO ai_api_usage (provider, service, operation, created_at, cost_status, success) "
                 "VALUES ('u', 's', 'o', 0, 'COST_UNKNOWN', 1)")                   # órfão de versão anterior
        db_module.init_db(self.db)                                               # upgrade
        db_module.init_db(self.db)                                               # idempotente
        orphans = self.sql("SELECT COUNT(*) FROM ai_api_usage u WHERE budget_charge IS NULL AND NOT EXISTS "
                           "(SELECT 1 FROM ai_api_cost_incidents i WHERE i.usage_id = u.usage_id AND i.status = 'OPEN')")
        self.assertEqual(orphans, [(0,)], "nenhum COST_RECONCILIATION_REQUIRED sem objeto reconciliável")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_cost_incidents WHERE kind='USAGE_COST_UNKNOWN'"), [(1,)])


# ------------------------------------------- 31–37 regras preservadas
class PreservedRulesTests(_Base):
    def test_31_to_37(self):
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
        self.lim("h", 100.0)
        rid = self.rsv("h", 50.0)
        self.g.settle(rid, outcome="EXECUTED", success=True, actual_cost=50.0, actual_currency="USD", cost_confirmed=True)
        self.g.migrate_budget_currency(principal=OWNER, provider="h", from_currency="USD", to_currency="EUR", fx_rate=0.9, **FX)
        with self.assertRaises(ValueError):
            self.g.migrate_budget_currency(principal=OWNER, provider="h", from_currency="USD", to_currency="EUR",
                                           fx_rate=0.5, **FX)
        self.g.migrate_budget_currency(principal=OWNER, provider="h", from_currency="EUR", to_currency="GBP", fx_rate=0.8, **FX)
        self.assertEqual(self.preview("h", 36.0, "GBP"), "AUTHORIZED")
        with self.assertRaises(ValueError):
            self.lim("z", 0.0, kind="unknown_cost_upper_bound")
        self.lim("c", 1000.0)
        r2 = self.rsv("c")
        self.assertEqual(self.g.settle(r2, outcome="UNKNOWN", success=False, actual_cost=9.0, actual_currency="USD")
                         ["cost_status"], "SETTLEMENT_STATE_CONFLICT")
        self.lim("r", 1000.0)
        r3 = self.rsv("r")
        self.g.settle(r3, outcome="EXECUTED", success=True, actual_cost=1.0, actual_currency="USD")
        with self.assertRaises(SettlementConflict):
            self.g.settle(r3, outcome="EXECUTED", success=True, actual_cost=2.0, actual_currency="USD")


if __name__ == "__main__":
    unittest.main()
