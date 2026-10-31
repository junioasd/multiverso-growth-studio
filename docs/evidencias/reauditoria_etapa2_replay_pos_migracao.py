"""ETAPA 2 — executada com o código CORRIGIDO/ATUAL sobre o MESMO banco (migração real)."""
import os, sys, sqlite3
sys.path.insert(0, os.getcwd())
from api.bootstrap import build_app
from commercial.journey import OrderJourney
from core.security.auth import principal_from_session
from tests._auth_test_helpers import fast_hash_password
db = sys.argv[1]
app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"), db_path=db, init_database=True)
ow = principal_from_session(app.auth.current_session(app.auth.login("o@t.com", "TestPassword123")))
j = OrderJourney(facade=app.facade, principal=ow, db_path=db)
c = sqlite3.connect(db)
print("apos migracao:", c.execute("SELECT processed_events_json, event_fingerprints_json FROM commercial_orders WHERE order_id='legacy_real'").fetchone())
def attempt(label, **kw):
    try: print(f"  {label:48s} -> ACEITO ({j.confirm_payment(order_id='legacy_real', gateway_event_id='g_old', **kw)['status']})")
    except Exception as e: print(f"  {label:48s} -> REJEITADO {type(e).__name__}: {str(e)[:70]}")
attempt("replay divergente valor=1.0 evidence=tampered", paid_amount=1.0, evidence="tampered")
attempt("replay divergente valor=999 evidence=original", paid_amount=999.0, evidence="gateway-original")
attempt("replay EQUIVALENTE valor=100 evidence=original", paid_amount=100.0, evidence="gateway-original")
try: print("  refund legado divergente (40)                     -> ACEITO", j.refund(order_id="legacy_ref", gateway_event_id="r_old", amount=40.0, reason="parcial")["status"])
except Exception as e: print("  refund legado divergente (40)                     -> REJEITADO", type(e).__name__)
print("receitas:", c.execute("SELECT COUNT(*), SUM(amount) FROM finance_entries WHERE entry_type='revenue'").fetchone(), "| reembolsos:", c.execute("SELECT COUNT(*), SUM(amount) FROM finance_entries WHERE entry_type='refund'").fetchone(), "| vendas:", c.execute("SELECT COUNT(*) FROM sales_events WHERE event_type='sale'").fetchone()[0])
