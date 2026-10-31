import os, sys, tempfile, sqlite3
sys.path.insert(0, os.getcwd())
from unittest import mock
from core.models import db as dbm
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
d = tempfile.mkdtemp(); db = os.path.join(d, "a.db"); dbm.init_db(db); kill_switch.configure(db); cost_guard.configure(db)
g = cost_guard.CostGuard(db)
def safe(f):
    try: r = f(); return r if r is not None else "ACEITO"
    except Exception as e: return f"REJEITADO {type(e).__name__}: {str(e)[:75]}"
def sql(q, p=()):
    c = sqlite3.connect(db); out = c.execute(q, p).fetchall(); c.commit(); c.close(); return out
print("=== B1: moeda legada ZZZ com historico")
sql("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) VALUES ('lz','provider_total','*',100,'ZZZ',0,'legado')")
sql("INSERT INTO ai_api_usage (provider, service, operation, created_at, cost, cost_status, currency, success, budget_charge) VALUES ('lz','s','o',0,5,'CONFIRMED','ZZZ',1,5)")
print("   preview:", g.preview(provider="lz", service="s", operation="o", estimated_cost=1.0, currency="ZZZ")["code"])
print("   retire:", safe(lambda: g.retire_unsupported_currency_limits(principal=OWNER, provider="lz", evidence="e")))
print("   migrate ZZZ->USD:", safe(lambda: g.migrate_budget_currency(principal=OWNER, provider="lz", from_currency="ZZZ", to_currency="USD", fx_rate=1.0, fx_source="BCE", fx_timestamp="t", evidence="e")))
print("   fluxo publico de recuperacao existe?", hasattr(g, "reconcile_unsupported_currency_history"))
print("=== B2: USD->JPY e depois JPY retirado da politica")
with mock.patch.dict(os.environ, {"MGS_EXTRA_CURRENCIES": "JPY"}):
    g.set_limit(principal=OWNER, provider="j", kind="provider_total", amount=100.0, currency="USD")
    rid = g.authorize(provider="j", service="s", operation="o", estimated_cost=10.0, currency="USD")["reservation_id"]
    g.settle(rid, outcome="EXECUTED", success=True, actual_cost=10.0, actual_currency="USD", cost_confirmed=True)
    print("   migracao USD->JPY:", safe(lambda: g.migrate_budget_currency(principal=OWNER, provider="j", from_currency="USD", to_currency="JPY", fx_rate=150.0, fx_source="BCE", fx_timestamp="t", evidence="e")))
print("   retire (JPY fora da politica):", safe(lambda: g.retire_unsupported_currency_limits(principal=OWNER, provider="j", evidence="e")))
print("   limites restantes:", sql("SELECT kind, currency FROM ai_api_limits WHERE provider='j'"), "| historico efetivo:", sql("SELECT converted_amount, converted_currency FROM ai_api_usage WHERE provider='j' AND budget_charge != 0"))
print("=== B3: teto alterado depois do uso desconhecido")
g.set_limit(principal=OWNER, provider="u", kind="provider_total", amount=1000.0, currency="USD")
g.set_limit(principal=OWNER, provider="u", kind="unknown_cost_upper_bound", amount=100.0, currency="USD")
out = g.record_usage(provider="u", service="s", operation="o", success=True)
g.set_limit(principal=OWNER, provider="u", kind="unknown_cost_upper_bound", amount=1.0, currency="USD")
print("   write-off:", safe(lambda: g.reconcile_usage(out["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)).get("reconciliation") if True else "")
print("   cobranca do evento (teto vigente era 100):", sql("SELECT budget_charge FROM ai_api_usage WHERE usage_id=?", (out["usage_id"],)))
print("=== B4: duas reservas iguais; resultado de B chega primeiro")
g.set_limit(principal=OWNER, provider="p", kind="provider_total", amount=1000.0, currency="USD")
ra = g.authorize(provider="p", service="s", operation="o", estimated_cost=10.0, currency="USD")["reservation_id"]
rb = g.authorize(provider="p", service="s", operation="o", estimated_cost=100.0, currency="USD")["reservation_id"]
print("   record_usage(custo 100, sem reservation_id):", safe(lambda: g.record_usage(provider="p", service="s", operation="o", success=True, cost=100.0, currency="USD")))
print("   A:", sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (ra,)), "| B:", sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (rb,)), "| incidentes:", [i["kind"] + "@" + ("A" if i["reservation_id"] == ra else "B") for i in g.open_incidents("p")])
print("=== B1 (recuperacao): reconcile_unsupported_currency_history ZZZ->USD (5 x 2 = 10)")
if hasattr(g, "reconcile_unsupported_currency_history"):
    print("   ", safe(lambda: g.reconcile_unsupported_currency_history(principal=OWNER, provider="lz", legacy_currency="ZZZ", target_currency="USD", fx_rate=2.0, fx_source="BCE", fx_timestamp="t", evidence="e", declared_converted_total=10.0)))
    print("   preview USD 1:", g.preview(provider="lz", service="s", operation="o", estimated_cost=1.0, currency="USD")["code"])
else:
    print("   funcao inexistente nesta versao — sem caminho de recuperacao")
