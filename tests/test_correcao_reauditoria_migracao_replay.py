"""
CORREÇÃO FINAL PÓS-REAUDITORIA — replay de evento LEGADO após migração.

Banco legado REAL de schema: a tabela `commercial_orders` é recriada com o
DDL EXATO do MASTER anterior (sem `event_fingerprints_json`), contendo dados
processados pelo próprio sistema; a migração real (`init_db`) roda depois.
Regra: mesmo ID + conteúdo COMPROVADO igual = retry; conteúdo diferente =
conflito; conteúdo original NÃO comprovável = bloqueado
(LEGACY_EVENT_UNVERIFIABLE), mesmo para replay aparentemente idêntico.
"""
import os
import sqlite3
import tempfile
import threading
import unittest

from api.bootstrap import build_app
from commercial.journey import JourneyConflict, OrderJourney
from core.models import db as db_module
from core.security.auth import principal_from_session
from tests._auth_test_helpers import fast_hash_password

_OLD_DDL = """CREATE TABLE commercial_orders (
    order_id TEXT PRIMARY KEY,
    lead_id TEXT, product_id TEXT, offer_id TEXT, campaign_id TEXT, channel_id TEXT NOT NULL,
    traffic_source TEXT, amount REAL NOT NULL, product_cost REAL, commission REAL,
    status TEXT NOT NULL,
    history_json TEXT NOT NULL DEFAULT '[]',
    processed_events_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
)"""
_OLD_COLS = ("order_id, lead_id, product_id, offer_id, campaign_id, channel_id, traffic_source, amount, product_cost, "
             "commission, status, history_json, processed_events_json, created_at, updated_at")


class _LegacyUpgradeBase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db)
        self.app = self._app(True)
        self.j = self._journey(self.app)
        # dados legados processados pelo PRÓPRIO sistema
        self.j.start_checkout(order_id="legacy_real", channel_id="c1", amount=100.0)
        self.j.confirm_payment(order_id="legacy_real", gateway_event_id="g_old", paid_amount=100.0,
                               evidence="gateway-original")
        self.j.start_checkout(order_id="legacy_ref", channel_id="c1", amount=200.0)
        self.j.confirm_payment(order_id="legacy_ref", gateway_event_id="g_ref", paid_amount=200.0, evidence="gw")
        self.j.confirm_delivery(order_id="legacy_ref", delivery_event_id="d_old", evidence="acesso #1")
        self.j.refund(order_id="legacy_ref", gateway_event_id="r_old", amount=80.0, reason="parcial")
        self._downgrade_to_legacy_schema()
        db_module.init_db(self.db)                      # MIGRAÇÃO REAL
        self.app = self._app(False)
        self.j = self._journey(self.app)
        self.baseline = self.money()

    def tearDown(self):
        db_module.remove_db_file(self.db)

    def _app(self, init):
        return build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"),
                         db_path=self.db, init_database=init)

    def _journey(self, app):
        ow = principal_from_session(app.auth.current_session(app.auth.login("o@t.com", "TestPassword123")))
        return OrderJourney(facade=app.facade, principal=ow, db_path=self.db)

    def _downgrade_to_legacy_schema(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("ALTER TABLE commercial_orders RENAME TO _co_new")
            conn.execute(_OLD_DDL)
            conn.execute(f"INSERT INTO commercial_orders ({_OLD_COLS}) SELECT {_OLD_COLS} FROM _co_new")
            conn.execute("DROP TABLE _co_new")
            conn.commit()
            cols = [r[1] for r in conn.execute("PRAGMA table_info(commercial_orders)")]
        finally:
            conn.close()
        assert "event_fingerprints_json" not in cols, "pré-condição: schema legado sem a coluna"

    def money(self):
        conn = sqlite3.connect(self.db)
        try:
            fin = conn.execute("SELECT entry_type, COUNT(*), SUM(amount) FROM finance_entries GROUP BY entry_type "
                               "ORDER BY entry_type").fetchall()
            ev = conn.execute("SELECT event_type, COUNT(*) FROM sales_events GROUP BY event_type ORDER BY event_type").fetchall()
            orders = conn.execute("SELECT order_id, status, processed_events_json FROM commercial_orders "
                                  "ORDER BY order_id").fetchall()
        finally:
            conn.close()
        return fin, ev, orders

    def fingerprints_map(self, order_id):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT event_fingerprints_json FROM commercial_orders WHERE order_id=?",
                                (order_id,)).fetchone()[0]
        finally:
            conn.close()


class LegacyReplayAfterMigrationTests(_LegacyUpgradeBase):
    def test_migration_does_not_fabricate_fingerprints(self):
        self.assertEqual(self.fingerprints_map("legacy_real"), "{}")

    def test_exactly_equivalent_legacy_replay_is_idempotent(self):
        o = self.j.confirm_payment(order_id="legacy_real", gateway_event_id="g_old", paid_amount=100.0,
                                   evidence="gateway-original")
        self.assertEqual(o["status"], "PAID")
        self.assertEqual(self.money(), self.baseline, "nada duplicado")
        self.assertEqual(self.fingerprints_map("legacy_real"), "{}", "replay não define retroativamente o original")

    def test_legacy_replay_with_different_amount_is_conflict(self):
        for amount in (1.0, 999.0, 100.01):
            with self.assertRaises(JourneyConflict, msg=amount):
                self.j.confirm_payment(order_id="legacy_real", gateway_event_id="g_old", paid_amount=amount,
                                       evidence="gateway-original")
        self.assertEqual(self.money(), self.baseline)

    def test_legacy_replay_with_other_divergent_fields_is_conflict(self):
        with self.assertRaises(JourneyConflict):
            self.j.confirm_payment(order_id="legacy_real", gateway_event_id="g_old", paid_amount=100.0,
                                   evidence="tampered")
        with self.assertRaises(JourneyConflict):
            self.j.refund(order_id="legacy_ref", gateway_event_id="r_old", amount=40.0, reason="parcial")
        with self.assertRaises(JourneyConflict):
            self.j.refund(order_id="legacy_ref", gateway_event_id="r_old", amount=80.0, reason="outro motivo")
        with self.assertRaises(JourneyConflict):
            self.j.confirm_delivery(order_id="legacy_ref", delivery_event_id="d_old", evidence="acesso #2 forjado")
        self.assertEqual(self.j.refund(order_id="legacy_ref", gateway_event_id="r_old", amount=80.0,
                                       reason="parcial")["status"], "REFUNDED", "equivalente comprovado = retry")
        self.assertEqual(self.money(), self.baseline)

    def test_unverifiable_legacy_event_fails_closed_even_if_identical(self):
        conn = sqlite3.connect(self.db)
        try:   # histórico insuficiente: o lançamento original não é mais comprovável
            conn.execute("DELETE FROM idempotency_records WHERE idempotency_key = 'legacy_real:revenue'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(JourneyConflict) as ctx:
            self.j.confirm_payment(order_id="legacy_real", gateway_event_id="g_old", paid_amount=100.0,
                                   evidence="gateway-original")
        self.assertIn("LEGACY_EVENT_UNVERIFIABLE", str(ctx.exception))
        self.assertEqual(self.j.get("legacy_real")["status"], "PAID", "pedido preservado")

    def test_contradictory_history_fails_closed(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE idempotency_records SET fingerprint = '[\"c1\", \"sale\", \"REAL\", 55.0, null, null, "
                         "null, null, null, null, null]' WHERE idempotency_key = 'legacy_real:sale'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(JourneyConflict) as ctx:
            self.j.confirm_payment(order_id="legacy_real", gateway_event_id="g_old", paid_amount=100.0,
                                   evidence="gateway-original")
        self.assertIn("LEGACY_EVENT_UNVERIFIABLE", str(ctx.exception))

    def test_new_events_after_migration_keep_strict_semantics(self):
        self.j.start_checkout(order_id="novo", channel_id="c1", amount=50.0)
        self.j.confirm_payment(order_id="novo", gateway_event_id="g_new", paid_amount=50.0, evidence="gw")
        self.assertEqual(self.j.confirm_payment(order_id="novo", gateway_event_id="g_new", paid_amount=50.0,
                                                evidence="gw")["status"], "PAID")
        with self.assertRaises(JourneyConflict):
            self.j.confirm_payment(order_id="novo", gateway_event_id="g_new", paid_amount=5.0, evidence="gw")
        self.assertNotEqual(self.fingerprints_map("novo"), "{}", "evento novo tem fingerprint persistido")

    def test_restart_and_repeated_migration(self):
        db_module.init_db(self.db)
        db_module.init_db(self.db)
        app3 = self._app(False)
        j3 = self._journey(app3)
        with self.assertRaises(JourneyConflict):
            j3.confirm_payment(order_id="legacy_real", gateway_event_id="g_old", paid_amount=1.0,
                               evidence="gateway-original")
        self.assertEqual(j3.confirm_payment(order_id="legacy_real", gateway_event_id="g_old", paid_amount=100.0,
                                            evidence="gateway-original")["status"], "PAID")
        self.assertEqual(self.fingerprints_map("legacy_real"), "{}")
        self.assertEqual(self.money(), self.baseline)

    def test_concurrent_legacy_replays_after_migration(self):
        results = {"ok": 0, "conflict": 0, "other": []}
        lock = threading.Lock()

        def go(amount):
            try:
                self.j.confirm_payment(order_id="legacy_real", gateway_event_id="g_old", paid_amount=amount,
                                       evidence="gateway-original")
                with lock:
                    results["ok"] += 1
            except JourneyConflict:
                with lock:
                    results["conflict"] += 1
            except Exception as exc:
                with lock:
                    results["other"].append(repr(exc))
        ts = [threading.Thread(target=go, args=(100.0 if i % 2 == 0 else 1.0,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual((results["ok"], results["conflict"], results["other"]), (4, 4, []))
        self.assertEqual(self.money(), self.baseline)


if __name__ == "__main__":
    unittest.main()
