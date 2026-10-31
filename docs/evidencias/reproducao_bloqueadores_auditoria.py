"""Reproduz os 4 bloqueadores da auditoria independente. Rodar na raiz do projeto:
    python docs/evidencias/reproducao_bloqueadores_auditoria.py
No MASTER FINAL anterior: A=VERIFIED, B=True, C=replays aceitos, D=aceito. Corrigido: todos rejeitados."""
import os, sys, tempfile
sys.path.insert(0, os.getcwd())
from tests.test_visao_evolutiva_e_agentic import make_atlas
from navigator.models import NavigatorAction
from core.coordination.mission import MissionStatus
print("=== A) MOCK_FALLBACK -> missao VERIFIED?")
atlas, _ = make_atlas()
m = atlas.delegate_mission(agent="NAVIGATOR", objective="coletar métricas")
atlas.update_mission_status(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
trace, results = atlas.run_mission_with_reasoning(m.mission_id, m.task_id, navigator_tasks=[
    {"task_id": "t1", "description": "x", "platform": "youtube", "action": NavigatorAction.COLLECT_METRICS}])
ev = results[0].get("result", results[0]).get("execution_evidence") if isinstance(results[0].get("result", results[0]), dict) else None
print("   status:", atlas.missions.get(m.mission_id, m.task_id).status.value, "| verified:", results[0]["verified"], "| evidencia:", ev or results[0])

print("=== B) navegador marca PUBLISH como sucesso so por navegar?")
from atlas.escalation import EscalationDesk
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role
class FakeBrowser:
    def navigate_and_extract(self, *, url):
        print("   FAKE_BROWSER_ONLY_NAVIGATED", url)
        from types import SimpleNamespace
        return SimpleNamespace(success=True, final_url=url, page_title="ok", reason="")
hub = BridgeHub(); hub.register_connector(platform="x", connector_type=ConnectorType.BROWSER_AUTOMATION, is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True)
desk = EscalationDesk(AuditTrail()); nav = NavigatorReal(bridge_hub=hub, escalation_desk=desk, browser_executor=FakeBrowser(), audit=AuditTrail())
kw = dict(platform="x", action="PUBLISH_POST", criticality=ActionCriticality.CRITICAL, params={"t": 1}, actor="n", idempotency_key="k")
esc = nav.execute_action(**kw).escalation_id; desk.approve(esc, decision="ok", resolved_by=Principal(subject="o", role=Role.OWNER, scopes={"*"}))
r = nav.execute_action(**kw, approved_escalation_id=esc, url="https://example.invalid/post")
print("   RESULT", r.success, r.mechanism_used)

print("=== C/D) replay de pagamento e order_id com payload diferente")
from api.bootstrap import build_app
from core.models import db as dbm
from core.security.auth import principal_from_session
from tests._auth_test_helpers import fast_hash_password
from commercial.journey import OrderJourney
fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd); dbm.remove_db_file(p)
app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"), db_path=p, init_database=True)
ow = principal_from_session(app.auth.current_session(app.auth.login("o@t.com", "TestPassword123")))
j = OrderJourney(facade=app.facade, principal=ow, db_path=p)
j.start_checkout(order_id="o1", channel_id="c1", amount=100.0)
print("   first pay", j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=100.0, evidence="gw")["status"])
for amt in (1.0, 999.0):
    try: print("   TAMPERED_REPLAY_ACCEPTED", amt, j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=amt, evidence="gw")["status"])
    except Exception as e: print("   replay", amt, "rejeitado:", type(e).__name__)
j.start_checkout(order_id="o2", channel_id="c1", amount=100.0)
try:
    o = j.start_checkout(order_id="o2", channel_id="other", amount=1.0)
    print("   order_id reutilizado com payload diferente ACEITO; devolvido:", o["channel_id"], o["amount"])
except Exception as e: print("   order_id reutilizado rejeitado:", type(e).__name__)
dbm.remove_db_file(p)
