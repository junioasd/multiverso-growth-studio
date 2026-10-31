import os, sys, tempfile, sqlite3
sys.path.insert(0, os.getcwd())
from core.models import db as dbm
from core.operations import cost_guard, kill_switch
from core.security.auth import Principal, Role
OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})
d = tempfile.mkdtemp(); db = os.path.join(d, "a.db"); dbm.init_db(db); kill_switch.configure(db); cost_guard.configure(db)
g = cost_guard.CostGuard(db)
def sql(q, p=()):
    c = sqlite3.connect(db); out = c.execute(q, p).fetchall(); c.commit(); c.close(); return out
def safe(f):
    try: r = f(); return r if r is not None else "ACEITO"
    except Exception as e: return f"REJEITADO {type(e).__name__}: {str(e)[:70]}"
class Resp:
    def __init__(self, ok, reason=""): self.available, self.reason = ok, reason
print("=== PARTE A: run_metered_call 2x com o mesmo correlation_id")
g.set_limit(principal=OWNER, provider="paid", kind="provider_total", amount=100.0, currency="USD")
calls = []
def perform():
    calls.append(1); return (Resp(True), "EXECUTED", 1.0, "USD")
params = {"estimated_cost": 1.0, "currency": "USD", "correlation_id": "logical-1"}
for _ in range(2):
    r = cost_guard.run_metered_call(provider="paid", classification="PAID", operation="gen", params=params, perform=perform, blocked=lambda why: Resp(False, why))
    print("   resposta: available =", r.available, "|", (r.reason or "")[:60])
print("   perform() executado:", len(calls), "vez(es) | reservas:", sql("SELECT correlation_id, status FROM ai_api_reservations WHERE provider='paid'"), "| usos:", sql("SELECT COUNT(*), SUM(budget_charge) FROM ai_api_usage WHERE provider='paid'"))
print("=== PARTE B: o mesmo usage_event_id entregue 2x, com uma nova reserva criada entre as entregas")
g.set_limit(principal=OWNER, provider="p", kind="provider_total", amount=100.0, currency="USD")
ra = g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
print("   entrega 1:", safe(lambda: g.record_usage(provider="p", service="s", operation="o", success=True, cost=1.0, currency="USD", usage_event_id="evt-dup")))
rb = g.authorize(provider="p", service="s", operation="o", estimated_cost=1.0, currency="USD")["reservation_id"]
print("   entrega 2 (mesmo evento):", safe(lambda: g.record_usage(provider="p", service="s", operation="o", success=True, cost=1.0, currency="USD", usage_event_id="evt-dup")))
print("   A:", sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (ra,)), "| B:", sql("SELECT status FROM ai_api_reservations WHERE reservation_id=?", (rb,)), "| lancamentos:", sql("SELECT COUNT(*), SUM(budget_charge) FROM ai_api_usage WHERE provider='p'"), "| usage_event_id gravado:", sql("SELECT usage_event_id FROM ai_api_usage WHERE provider='p'"))
