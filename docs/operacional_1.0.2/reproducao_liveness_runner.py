import os, sys, tempfile, json, threading
sys.path.insert(0, os.getcwd())
from api.bootstrap import build_app
from core.models import db as dbm
from core.operations import kill_switch
from core.security.auth import principal_from_session
from bridge.hub import ConnectorType, HealthStatus
from navigator.planning import OperationPlanner, OperationRunner, StepSpec
from tests._auth_test_helpers import fast_hash_password
d = tempfile.mkdtemp(); db = os.path.join(d, "l.db")
app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"), db_path=db, init_database=True)
ow = principal_from_session(app.auth.current_session(app.auth.login("o@t.com", "TestPassword123")))
app.atlas.bridge.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True)
kill_switch.activate(db, principal=ow, reason="ativo ANTES do primeiro advance")
r = OperationRunner(db_path=db, planner=OperationPlanner(bridge_hub=app.atlas.bridge), navigator=app.atlas.navigator_real, escalation_desk=app.atlas.escalation_desk, actor="T")
run = r.create_run(objective="o", requested_by="T", steps=[StepSpec(step_id="p", platform="youtube", action="PUBLISH_VIDEO", adapter_method="publish", params={"title": "t"})])
out = {}
t = threading.Thread(target=lambda: out.update(status=r.advance(run)), daemon=True); t.start(); t.join(8)
if t.is_alive():
    _, st, _ = r.load(run); print("TRAVADO: advance() nao retornou em 8s | passo:", json.dumps(st["p"], ensure_ascii=False)[:300])
else:
    _, st, _ = r.load(run); print("retornou:", out, "| passo:", json.dumps(st["p"], ensure_ascii=False)[:300])
os._exit(0)
