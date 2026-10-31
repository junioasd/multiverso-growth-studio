import os, sys, tempfile
sys.path.insert(0, os.getcwd())
from core.models import db as dbm
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
d = tempfile.mkdtemp(); db = os.path.join(d, "a.db"); dbm.init_db(db); kill_switch.configure(db); cost_guard.configure(db)
g = cost_guard.CostGuard(db)
def safe(f):
    try: r = f(); return r if r is not None else "ACEITO"
    except Exception as e: return f"REJEITADO {type(e).__name__}: {str(e)[:70]}"
print("=== B1: moeda inexistente ZZZ")
print("   valid_currency('ZZZ') =", cost_guard.valid_currency("ZZZ"))
print("   set_limit 100 ZZZ:", safe(lambda: g.set_limit(principal=OWNER, provider="z", kind="provider_total", amount=100.0, currency="ZZZ")))
print("   preview 1 ZZZ:", g.preview(provider="z", service="s", operation="o", estimated_cost=1.0, currency="ZZZ")["code"])
print("=== B2: migracao FX acima de MAX_MONEY")
g.set_limit(principal=OWNER, provider="big", kind="provider_total", amount=1e12, currency="USD")
print("   migracao USD->EUR fx=1e12:", safe(lambda: g.migrate_budget_currency(principal=OWNER, provider="big", from_currency="USD", to_currency="EUR", fx_rate=1e12, fx_source="BCE", fx_timestamp="t", evidence="e")))
with dbm.connect(db) as c: print("   limite persistido:", c.execute("SELECT amount, currency FROM ai_api_limits WHERE provider='big'").fetchone()[:])
print("   preview 1e12 EUR:", g.preview(provider="big", service="s", operation="o", estimated_cost=1e12, currency="EUR")["code"])
print("=== B3: write-off de SETTLEMENT_STATE_CONFLICT")
g.set_limit(principal=OWNER, provider="w", kind="provider_total", amount=1000.0, currency="USD")
rid = g.authorize(provider="w", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
g.settle(rid, outcome="NOT_EXECUTED", success=False, actual_cost=100.0, actual_currency="USD", cost_confirmed=True)
iid = g.open_incidents("w")[0]["incident_id"]
print("   write-off:", safe(lambda: g.reconcile_incident(iid, principal=OWNER, evidence="decisao", source="write-off", write_off=True)))
with dbm.connect(db) as c: print("   reserva apos write-off:", c.execute("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (rid,)).fetchone()[0], "| open_reservations lista?", any(r["reservation_id"] == rid for r in g.open_reservations()))
print("   migracao USD->EUR depois:", safe(lambda: g.migrate_budget_currency(principal=OWNER, provider="w", from_currency="USD", to_currency="EUR", fx_rate=0.9, fx_source="BCE", fx_timestamp="t", evidence="e")))
print("=== B4: record_usage sem custo e sem reserva")
g.set_limit(principal=OWNER, provider="u", kind="provider_total", amount=100.0, currency="USD")
print("   record_usage:", g.record_usage(provider="u", service="s", operation="o", success=True))
print("   incidentes criados:", len(g.open_incidents("u")), "| proxima autorizacao:", g.preview(provider="u", service="s", operation="o", estimated_cost=1.0, currency="USD")["code"])
print("   existe metodo para reconciliar usage_id?", hasattr(g, "reconcile_usage"))
