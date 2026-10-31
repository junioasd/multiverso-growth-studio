"""ETAPA 1 — executada com o código do MASTER ANTERIOR (sem event_fingerprints_json)."""
import os, sys
sys.path.insert(0, os.getcwd())
from api.bootstrap import build_app
from commercial.journey import OrderJourney
from core.security.auth import principal_from_session
from tests._auth_test_helpers import fast_hash_password
db = sys.argv[1]
app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"), db_path=db, init_database=True)
ow = principal_from_session(app.auth.current_session(app.auth.login("o@t.com", "TestPassword123")))
j = OrderJourney(facade=app.facade, principal=ow, db_path=db)
j.start_checkout(order_id="legacy_real", channel_id="c1", amount=100.0)
j.confirm_payment(order_id="legacy_real", gateway_event_id="g_old", paid_amount=100.0, evidence="gateway-original")
j.start_checkout(order_id="legacy_ref", channel_id="c1", amount=200.0)
j.confirm_payment(order_id="legacy_ref", gateway_event_id="g_ref_pay", paid_amount=200.0, evidence="gw")
j.refund(order_id="legacy_ref", gateway_event_id="r_old", amount=80.0, reason="parcial")
import sqlite3
c = sqlite3.connect(db); cols = [r[1] for r in c.execute("PRAGMA table_info(commercial_orders)")]
print("schema ANTIGO tem event_fingerprints_json?", "event_fingerprints_json" in cols); c.close()
