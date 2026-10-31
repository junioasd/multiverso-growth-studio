"""
v1.0.10-OPERATIONAL-CANDIDATE — bloqueadores da auditoria de readiness 1.0.9:
(1) correlation_id vinculado ao payload completo + lifecycle terminal; (2) identidade
da reserva validada em record_usage; (3) uso sem reserva com usage_event_id
idempotente; (4) retirada considera todas as referências monetárias do incidente;
(5) reconciliação normal só em moeda suportada. SQLite real; threads reais.
"""
import os
import sqlite3
import tempfile
import threading
import unittest

from core.models import db as db_module
from core.operations import cost_guard, kill_switch
from core.operations.cost_guard import SettlementConflict
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

    def auth(self, p="c", amount=1.0, cur="USD", **kw):
        return self.g.authorize(provider=p, service="s", operation="o", estimated_cost=amount, currency=cur, **kw)

    def state(self):
        return (self.sql("SELECT reservation_id, status FROM ai_api_reservations ORDER BY reservation_id"),
                self.sql("SELECT COUNT(*), COALESCE(SUM(budget_charge),0) FROM ai_api_usage"),
                self.sql("SELECT COUNT(*) FROM ai_api_cost_incidents"))


# ------------------------------------------------ 1–7 correlation_id
class CorrelationPayloadTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("c", 100.0)
        self.first = self.auth(amount=1.0, correlation_id="cid", project="P", campaign="C", job_id="J")

    def test_1_6_identical_retry_is_idempotent_and_does_not_reexecute_budget(self):
        before = self.state()
        again = self.auth(amount=1.0, correlation_id="cid", project="P", campaign="C", job_id="J")
        self.assertEqual((again["code"], again["reservation_id"]), ("ALREADY_RESERVED", self.first["reservation_id"]))
        self.assertEqual(self.state(), before, "retry idêntico não cria nova reserva")

    def test_2_to_5_divergent_payload_is_conflict(self):
        before = self.state()
        for kw in ({"amount": 500.0}, {"cur": "EUR"}, {"project": "X"}, {"campaign": "X"}, {"job_id": "X"}):
            args = dict(amount=1.0, correlation_id="cid", project="P", campaign="C", job_id="J")
            args.update(kw)
            with self.assertRaises(ValueError, msg=str(kw)) as ctx:
                self.auth(**args)
            self.assertIn("CORRELATION_ID_CONFLICT", str(ctx.exception))
        self.assertEqual(self.state(), before)

    def test_7_terminal_reservation_is_never_a_new_authorization(self):
        self.g.settle(self.first["reservation_id"], outcome="EXECUTED", success=True, actual_cost=1.0, actual_currency="USD")
        out = self.auth(amount=1.0, correlation_id="cid", project="P", campaign="C", job_id="J")
        self.assertEqual((out["authorized"], out["code"], out["status"]), (False, "ALREADY_COMPLETED", "COMMITTED"))

    def test_30_31_correlation_and_fingerprint_survive_restart_and_legacy_fails_safe(self):
        g2 = cost_guard.CostGuard(self.db)
        self.assertEqual(g2.authorize(provider="c", service="s", operation="o", estimated_cost=1.0, currency="USD",
                                      correlation_id="cid", project="P", campaign="C", job_id="J")["code"], "ALREADY_RESERVED")
        with self.assertRaises(ValueError):
            g2.authorize(provider="c", service="s", operation="o", estimated_cost=2.0, currency="USD", correlation_id="cid",
                         project="P", campaign="C", job_id="J")
        self.sql("UPDATE ai_api_reservations SET correlation_fingerprint = NULL")       # reserva legada (1.0.9)
        with self.assertRaises(ValueError) as ctx:
            g2.authorize(provider="c", service="s", operation="o", estimated_cost=1.0, currency="USD", correlation_id="cid",
                         project="P", campaign="C", job_id="J")
        self.assertIn("não comprovável", str(ctx.exception))


# ---------------------------------------- 8–12 identidade da reserva no uso
class ReservationIdentityTests(_Base):
    def setUp(self):
        super().setUp()
        for p in ("p", "q"):
            self.lim(p, 100.0)
        self.rid = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD",
                                    project="P", correlation_id="k")["reservation_id"]

    def test_9_to_12_any_identity_mismatch_is_conflict_with_no_changes(self):
        before = self.state()
        for kw in ({"provider": "q"}, {"service": "WRONG"}, {"operation": "WRONG"}, {"project": "OUTRO"}):
            args = dict(provider="p", service="s", operation="o", success=True, cost=1.0, currency="USD",
                        reservation_id=self.rid)
            args.update(kw)
            with self.assertRaises(ValueError, msg=str(kw)) as ctx:
                self.g.record_usage(**args)
            self.assertIn("RESERVATION_IDENTITY_MISMATCH", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.g.record_usage(provider="p", service="WRONG", operation="o", success=True, cost=1.0, currency="USD",
                                correlation_id="k")
        self.assertIn("CORRELATION_IDENTITY_MISMATCH", str(ctx.exception))
        self.assertEqual(self.state(), before, "nenhuma reserva, uso, cobrança ou incidente alterado")

    def test_8_correct_identity_settles(self):
        self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=1.0, currency="USD",
                            reservation_id=self.rid, project="P")
        self.assertEqual(self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (self.rid,)),
                         [("COMMITTED",)])


# ------------------------------------------ 13–20 uso sem reserva
class UsageEventIdentityTests(_Base):
    def setUp(self):
        super().setUp()
        self.lim("u", 1000.0)
        self.lim("u", 100.0, kind="unknown_cost_upper_bound")

    def rec(self, ev, **kw):
        args = dict(provider="u", service="s", operation="o", success=True, usage_event_id=ev)
        args.update(kw)
        return self.g.record_usage(**args)

    def test_identity_is_required(self):
        with self.assertRaises(ValueError) as ctx:
            self.g.record_usage(provider="u", service="s", operation="o", success=True)
        self.assertIn("USAGE_EVENT_ID_REQUIRED", str(ctx.exception))
        self.assertEqual(self.state()[1], [(0, 0)])

    def test_13_to_16_identical_retry_returns_same_usage_no_new_incident_no_double_charge(self):
        a = self.rec("ev-1")
        b = self.rec("ev-1")
        self.assertEqual((b["usage_id"], b["incident_id"], b["already_recorded"]), (a["usage_id"], a["incident_id"], True))
        self.assertEqual(len(self.g.open_incidents("u")), 1)
        self.g.reconcile_usage(a["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        self.assertEqual(self.g.summary()["u/USD"]["budget_charged"], 100.0, "cobrado UMA vez")

    def test_17_divergent_payload_is_conflict(self):
        self.rec("ev-2")
        before = self.state()
        for kw in ({"success": False}, {"cost": 3.0, "currency": "USD"}, {"service": "x"}, {"project": "P"}):
            with self.assertRaises(ValueError, msg=str(kw)) as ctx:
                self.rec("ev-2", **kw)
            self.assertIn("USAGE_EVENT_CONFLICT", str(ctx.exception))
        self.assertEqual(self.state(), before)

    def test_18_idempotency_survives_restart(self):
        a = self.rec("ev-3")
        g2 = cost_guard.CostGuard(self.db)
        self.assertEqual(g2.record_usage(provider="u", service="s", operation="o", success=True,
                                         usage_event_id="ev-3")["usage_id"], a["usage_id"])

    def test_19_20_concurrent_same_id_creates_exactly_one_distinct_ids_independent(self):
        results, barrier = [], threading.Barrier(8)

        def go(ev):
            barrier.wait()
            results.append(self.rec(ev)["usage_id"])
        ts = [threading.Thread(target=go, args=("ev-same",)) for _ in range(6)] + \
             [threading.Thread(target=go, args=(f"ev-{i}",)) for i in ("x", "y")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_usage WHERE usage_event_id='ev-same'"), [(1,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_usage"), [(3,)])
        self.assertEqual(len(self.g.open_incidents("u")), 3)


# ------------------------------------- 21–27 moeda legada e retirada
class LegacyCurrencyTests(_Base):
    def legacy_limit(self, p):
        self.sql("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) "
                 "VALUES (?, 'provider_total', '*', 100, 'ZZZ', 0, 'legado')", (p,))

    def test_21_snapshot_currency_blocks_retirement_and_full_recovery_path_exists(self):
        self.legacy_limit("lz")
        self.sql("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) "
                 "VALUES ('lz', 'unknown_cost_upper_bound', '*', 4, 'ZZZ', 0, 'legado')")
        u = self.g.record_usage(provider="lz", service="s", operation="o", success=True, usage_event_id="ev-lz")
        with self.assertRaises(ValueError) as ctx:
            self.g.retire_unsupported_currency_limits(principal=OWNER, provider="lz", evidence="e")
        self.assertIn("CURRENCY_RETIREMENT_REQUIRES_RECONCILIATION", str(ctx.exception))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_limits WHERE provider='lz'"), [(2,)])
        # saída: recuperação legada (sem histórico com valor: total 0) e depois o uso pela taxa REGISTRADA
        self.g.reconcile_unsupported_currency_history(principal=OWNER, provider="lz", legacy_currency="ZZZ",
                                                      target_currency="USD", fx_rate=2.0, declared_converted_total=0.0, **FX)
        self.g.reconcile_usage(u["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        self.assertEqual(self.sql("SELECT budget_charge, currency FROM ai_api_usage WHERE usage_id=?", (u["usage_id"],)),
                         [(8.0, "USD")], "teto do evento (4 ZZZ) × taxa registrada (2) — nunca 1:1")
        self.assertEqual(self.g.open_incidents("lz"), [])

    def test_22_original_currency_blocks_retirement(self):
        self.legacy_limit("lo")
        self.sql("INSERT INTO ai_api_cost_incidents (incident_id, provider, kind, status, created_at, original_currency) "
                 "VALUES ('inc-o', 'lo', 'CURRENCY_MISMATCH_INCIDENT', 'OPEN', 0, 'ZZZ')")
        with self.assertRaises(ValueError):
            self.g.retire_unsupported_currency_limits(principal=OWNER, provider="lo", evidence="e")

    def test_24_retirement_after_valid_reconciliation_works(self):
        self.legacy_limit("lr")
        u = self.g.record_usage(provider="lr", service="s", operation="o", success=True, usage_event_id="ev-lr")
        self.sql("UPDATE ai_api_cost_incidents SET status = 'RECONCILED' WHERE usage_id = ?", (u["usage_id"],))
        self.sql("UPDATE ai_api_usage SET budget_charge = 0 WHERE usage_id = ?", (u["usage_id"],))
        self.assertEqual(self.g.retire_unsupported_currency_limits(principal=OWNER, provider="lr", evidence="e")["retired"],
                         ["ZZZ"])

    def test_25_27_normal_reconciliation_never_writes_unsupported_currency(self):
        self.legacy_limit("lz2")
        u = self.g.record_usage(provider="lz2", service="s", operation="o", success=True, usage_event_id="ev-lz2")
        before = self.state()
        for kw in ({"final_amount": 5.0, "final_currency": "ZZZ"}, {"write_off": True}):
            with self.assertRaises(ValueError, msg=str(kw)) as ctx:
                self.g.reconcile_usage(u["usage_id"], principal=OWNER, evidence="e", source="s", **kw)
            self.assertIn("UNSUPPORTED_CURRENCY", str(ctx.exception))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ai_api_usage WHERE currency='ZZZ'"), [(0,)])

    def test_26_legacy_flow_still_works_for_history_with_value(self):
        self.legacy_limit("lh")
        self.sql("INSERT INTO ai_api_usage (provider, service, operation, created_at, cost, cost_status, currency, success, "
                 "budget_charge) VALUES ('lh', 's', 'o', 0, 5, 'CONFIRMED', 'ZZZ', 1, 5)")
        self.g.reconcile_unsupported_currency_history(principal=OWNER, provider="lh", legacy_currency="ZZZ",
                                                      target_currency="USD", fx_rate=2.0, declared_converted_total=10.0, **FX)
        self.assertEqual(self.g.preview(provider="lh", service="s", operation="o", estimated_cost=1.0,
                                        currency="USD")["code"], "AUTHORIZED")


# ---------------------------------------------- 28–40 regras preservadas
class PreservedRulesTests(_Base):
    def test_28_29_33_ambiguous_out_of_order_and_divergent_replay(self):
        self.lim("p", 1000.0)
        a = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=10.0, currency="USD")["reservation_id"]
        b = self.g.authorize(provider="p", service="s", operation="o", estimated_cost=100.0, currency="USD")["reservation_id"]
        with self.assertRaises(ValueError):
            self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=100.0, currency="USD")
        self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=100.0, currency="USD",
                            reservation_id=b)
        self.assertEqual(self.sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (a,)), [("RESERVED",)])
        with self.assertRaises(SettlementConflict):
            self.g.record_usage(provider="p", service="s", operation="o", success=True, cost=5.0, currency="USD",
                                reservation_id=b)

    def test_34_to_40_previous_invariants(self):
        self.lim("u", 1000.0)
        self.lim("u", 50.0, kind="unknown_cost_upper_bound")
        old = self.g.record_usage(provider="u", service="s", operation="o", success=True, usage_event_id="ev-old")
        self.lim("u", 1.0, kind="unknown_cost_upper_bound")
        self.g.reconcile_usage(old["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
        self.assertEqual(self.sql("SELECT budget_charge FROM ai_api_usage WHERE usage_id=?", (old["usage_id"],)), [(50.0,)])
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
