import os, sys, threading
sys.path.insert(0, '.')
from atlas.escalation import EscalationDesk
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.execution.vault import TokenVault
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.security.audit import AuditTrail
db, port, esc, ref = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
class A(PlatformApiAdapter):
    platform = "youtube"; base_url = f"http://127.0.0.1:{port}"
    def publish(self, **kw):
        # morre DEPOIS de o servico ter recebido/executado, ANTES de registrar o resultado
        threading.Timer(0.2, lambda: os._exit(9)).start()
        return self._request(action="PUBLISH", method="POST", path="/publish", body=kw)
hub = BridgeHub(); hub.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True)
nav = NavigatorReal(bridge_hub=hub, escalation_desk=EscalationDesk(AuditTrail(), db_path=db), adapters_by_platform={"youtube": A(vault=TokenVault(db_path=db), account_reference_id=ref, timeout_seconds=5)}, db_path=db, audit=AuditTrail())
nav.execute_action(platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL, params={"title": "v"}, actor="navigator", idempotency_key="pub-crash", adapter_method="publish", approved_escalation_id=esc)
