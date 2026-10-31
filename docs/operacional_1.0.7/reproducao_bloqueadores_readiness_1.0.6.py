import os, sys, tempfile, threading
sys.path.insert(0, os.getcwd())
from core.models import db as dbm
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
d = tempfile.mkdtemp(); db = os.path.join(d, "a.db"); dbm.init_db(db); kill_switch.configure(db); cost_guard.configure(db)
g = cost_guard.CostGuard(db)
def safe(f):
    try: r = f(); return r if r is not None else "ACEITO"
    except Exception as e: return f"REJEITADO {type(e).__name__}: {str(e)[:60]}"
def mig(p, fr, to, rate):
    return safe(lambda: g.migrate_budget_currency(principal=OWNER, provider=p, from_currency=fr, to_currency=to, fx_rate=rate, fx_source="BCE", fx_timestamp="2026-09-25T10:00Z", evidence="e"))
def hist(p, amount=50.0):
    g.set_limit(principal=OWNER, provider=p, kind="provider_total", amount=100.0, currency="USD")
    rid = g.authorize(provider=p, service="s", operation="o", estimated_cost=amount, currency="USD")["reservation_id"]
    g.settle(rid, outcome="EXECUTED", success=True, actual_cost=amount, actual_currency="USD", cost_confirmed=True)
print("=== B1: set_limit concorrente em moedas diferentes")
bar = threading.Barrier(2); out = {}
def sl(kind, cur):
    bar.wait(); out[kind] = safe(lambda: g.set_limit(principal=OWNER, provider="conc", kind=kind, amount=100.0, currency=cur))
ts = [threading.Thread(target=sl, args=a) for a in (("provider_total", "USD"), ("per_call", "EUR"))]; [t.start() for t in ts]; [t.join() for t in ts]
with dbm.connect(db) as c: cur = sorted({r[0] for r in c.execute("SELECT currency FROM ai_api_limits WHERE provider='conc'")})
print("   resultados:", out, "| moedas persistidas:", cur)
print("=== B2: repetir USD->EUR com outra taxa")
hist("fx2"); print("   migracao 0.9:", mig("fx2", "USD", "EUR", 0.9))
print("   EUR 45.01:", g.preview(provider="fx2", service="s", operation="o", estimated_cost=45.01, currency="EUR")["code"])
print("   migracao 0.5 (repetida):", mig("fx2", "USD", "EUR", 0.5))
print("   EUR 65:", g.preview(provider="fx2", service="s", operation="o", estimated_cost=65.0, currency="EUR")["code"], "(antes o saldo era EUR 45)")
print("=== B3: cadeia USD->EUR->GBP")
hist("fx3"); print("   USD->EUR:", mig("fx3", "USD", "EUR", 0.9), "| EUR->GBP:", mig("fx3", "EUR", "GBP", 0.8))
print("   preview GBP 10:", g.preview(provider="fx3", service="s", operation="o", estimated_cost=10.0, currency="GBP")["code"])
print("=== B4: reconciliacao FX incoerente (EUR 100 x 0.5 != USD 1)")
g.set_limit(principal=OWNER, provider="m", kind="provider_total", amount=1000.0, currency="USD")
rid = g.authorize(provider="m", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
g.settle(rid, outcome="EXECUTED", success=True, actual_cost=100.0, actual_currency="EUR")
iid = g.open_incidents("m")[0]["incident_id"]
print("   reconcile final=USD 1 fx=0.5:", safe(lambda: g.reconcile_incident(iid, principal=OWNER, evidence="e", source="s", final_amount=1.0, final_currency="USD", fx_rate=0.5, fx_source="BCE", fx_timestamp="t")))
print("   ledger m =", g.summary()["m/USD"]["budget_charged"])
