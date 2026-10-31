import os, sys, tempfile, sqlite3
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
def sql(q, p=()):
    c = sqlite3.connect(db); out = c.execute(q, p).fetchall(); c.commit(); c.close(); return out
def short(r): return {k: r[k] for k in ("authorized", "code", "already_reserved") if k in r} if isinstance(r, dict) else r
print("=== B1: correlation_id reaproveitado com payload divergente (limite USD 100)")
g.set_limit(principal=OWNER, provider="c", kind="provider_total", amount=100.0, currency="USD")
g.authorize(provider="c", service="s", operation="o", estimated_cost=1.0, currency="USD", correlation_id="cid")
print("   nova USD 500:", g.authorize(provider="c", service="s", operation="o", estimated_cost=500.0, currency="USD")["code"])
print("   cid + USD 500 + outro project:", short(safe(lambda: g.authorize(provider="c", service="s", operation="o", estimated_cost=500.0, currency="USD", project="X", correlation_id="cid"))))
print("=== B2: reservation_id com service/operation/provider divergentes")
g.set_limit(principal=OWNER, provider="p", kind="provider_total", amount=100.0, currency="USD")
g.set_limit(principal=OWNER, provider="q", kind="provider_total", amount=100.0, currency="USD")
r1 = g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
print("   service/operation WRONG:", safe(lambda: g.record_usage(provider="p", service="WRONG", operation="WRONG", success=True, cost=1.0, currency="USD", reservation_id=r1)))
r2 = g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
print("   provider q com reserva de p:", safe(lambda: g.record_usage(provider="q", service="s", operation="o", success=True, cost=1.0, currency="USD", reservation_id=r2)))
print("   status r1/r2:", sql("SELECT status FROM ai_api_reservations WHERE reservation_id IN (?,?) ORDER BY created_at", (r1, r2)))
print("=== B3: mesmo uso sem reserva registrado 2x (teto 100)")
g.set_limit(principal=OWNER, provider="u", kind="provider_total", amount=1000.0, currency="USD")
g.set_limit(principal=OWNER, provider="u", kind="unknown_cost_upper_bound", amount=100.0, currency="USD")
a = safe(lambda: g.record_usage(provider="u", service="s", operation="o", success=True))
b = safe(lambda: g.record_usage(provider="u", service="s", operation="o", success=True))
print("   usos:", a if isinstance(a, str) else a.get("usage_id"), "|", b if isinstance(b, str) else b.get("usage_id"), "| incidentes abertos:", len(g.open_incidents("u")))
for x in (a, b):
    if isinstance(x, dict): g.reconcile_usage(x["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
print("   total cobrado:", g.summary().get("u/USD", {}).get("budget_charged"))
print("=== B4: retirada de ZZZ com incidente aberto que referencia ZZZ no snapshot")
sql("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) VALUES ('lz','provider_total','*',100,'ZZZ',0,'legado')")
inc = safe(lambda: g.record_usage(provider="lz", service="s", operation="o", success=True, **({"usage_event_id": "ev-lz"} if "usage_event_id" in g.record_usage.__code__.co_varnames else {})))
print("   incidente:", sql("SELECT currency, snapshot_currency, status FROM ai_api_cost_incidents WHERE provider='lz'"))
print("   retire:", safe(lambda: g.retire_unsupported_currency_limits(principal=OWNER, provider="lz", evidence="e")))
print("   limites restantes:", sql("SELECT currency FROM ai_api_limits WHERE provider='lz'"))
print("=== B5: reconcile_usage em moeda nao suportada ZZZ")
sql("INSERT INTO ai_api_limits (provider, kind, scope_id, amount, currency, updated_at, actor) VALUES ('lz2','provider_total','*',100,'ZZZ',0,'legado')")
u2 = g.record_usage(provider="lz2", service="s", operation="o", success=True, **({"usage_event_id": "ev-lz2"} if "usage_event_id" in g.record_usage.__code__.co_varnames else {}))
print("   reconcile_usage final 5 ZZZ:", safe(lambda: g.reconcile_usage(u2["usage_id"], principal=OWNER, evidence="e", source="s", final_amount=5.0, final_currency="ZZZ")))
print("   lancamento:", sql("SELECT cost, currency, budget_charge, cost_status FROM ai_api_usage WHERE usage_id=?", (u2["usage_id"],)))
print("=== LIFECYCLE: authorize com correlation_id de reserva ja COMMITTED")
rc = g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD", correlation_id="done")["reservation_id"]
g.settle(rc, outcome="EXECUTED", success=True, actual_cost=1.0, actual_currency="USD")
print("   retry:", short(safe(lambda: g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD", correlation_id="done"))))
print("=== B3 (identidade): o mesmo evento com usage_event_id, 2x")
if "usage_event_id" in g.record_usage.__code__.co_varnames:
    x1 = g.record_usage(provider="u", service="s", operation="o", success=True, usage_event_id="ev-1")
    x2 = g.record_usage(provider="u", service="s", operation="o", success=True, usage_event_id="ev-1")
    print("   mesmo usage_id?", x1["usage_id"] == x2["usage_id"], "| incidentes abertos:", len(g.open_incidents("u")))
    g.reconcile_usage(x1["usage_id"], principal=OWNER, evidence="e", source="s", write_off=True)
    print("   total cobrado:", g.summary().get("u/USD", {}).get("budget_charged"))
else:
    print("   parametro usage_event_id inexistente nesta versao — sem identidade idempotente")
print("=== B4 (saida): recuperacao legada com incidente aberto e reconciliacao do uso pela taxa registrada")
try:
    g.reconcile_unsupported_currency_history(principal=OWNER, provider="lz", legacy_currency="ZZZ", target_currency="USD", fx_rate=2.0, fx_source="BCE", fx_timestamp="t", evidence="e", declared_converted_total=0.0)
    uid = sql("SELECT usage_id FROM ai_api_cost_incidents WHERE provider='lz' AND status='OPEN'")[0][0]
    print("   reconcile_usage USD 3:", safe(lambda: g.reconcile_usage(uid, principal=OWNER, evidence="fatura", source="p", final_amount=3.0, final_currency="USD")))
    print("   incidentes abertos lz:", len(g.open_incidents("lz")))
except Exception as e:
    print("   sem saida:", type(e).__name__, str(e)[:80])
