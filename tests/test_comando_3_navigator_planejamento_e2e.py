"""
COMANDO MESTRE 3/6 — NAVIGATOR (planejamento/execução de planos) + FORGE
(preparação) + fluxo ATLAS → NAVIGATOR → FORGE → EXECUTION HUB → ATLAS.

Caminho REAL: `api.bootstrap.build_app` (o MESMO `NavigatorReal`,
`EscalationDesk`, `TokenVault`, `BridgeHub`, `ExecutionObservability` e
Gêmeo do diretor) contra um SERVIDOR HTTP LOCAL REAL. Nenhum mock de rede.
"""
import http.server
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest
import urllib.parse

from api.bootstrap import build_app
from atlas.learning import Recommendation, StrategyStatus, TestDesign
from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.execution.vault import TokenType
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.models import db as db_module
from core.models.entities import ValueType
from core.security.auth import principal_from_session
from forge.preparation import ExecutionPreparer
from navigator.planning import OperationPlanner, OperationRunner, PlanValidationError, StepSpec
from tests._auth_test_helpers import fast_hash_password

_PWD = "TestPassword123"
SECRET = "TOKEN-REAL-NAO-EXPOR-91c2"


class _Svc(http.server.BaseHTTPRequestHandler):
    posts: list = []
    script: list = []          # status HTTP por requisição POST (consumidos em ordem); vazio => 200
    status_value = "published"
    lock = threading.Lock()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with self.lock:
            code = self.script.pop(0) if self.script else 200
            if code in (200, 201, 202):
                self.posts.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
            ext = f"ext_{len(self.posts)}"
        out = json.dumps({"id": ext} if code < 400 else {"error": "x"}).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        out = json.dumps({"id": q.get("id", [""])[0], "status": self.status_value}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class _LocalYouTube(PlatformApiAdapter):
    platform = "youtube"

    def create_asset(self, **kw):
        return self._request(action="CREATE_ASSET", method="POST", path="/assets", body=kw)

    def publish(self, **kw):
        return self._request(action="PUBLISH", method="POST", path="/publish", body=kw)

    def publication_status(self, **kw):
        return self._request(action="STATUS", method="GET", path="/status", params=kw)


class _AppBase(unittest.TestCase):
    def setUp(self):
        _Svc.posts, _Svc.script, _Svc.status_value = [], [], "published"
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Svc)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db)
        self._old = os.environ.get("MGS_VAULT_MASTER_KEY")
        os.environ["MGS_VAULT_MASTER_KEY"] = "chave-mestra-teste-c3"
        self.app = self._build(True)
        self.owner = principal_from_session(self.app.auth.current_session(self.app.auth.login("o@t.com", _PWD)))
        self.ref = self.app.atlas.token_vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN,
                                                          value=SECRET)
        self._wire(self.app)
        self._k = 0

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        db_module.remove_db_file(self.db)
        if self._old is None:
            os.environ.pop("MGS_VAULT_MASTER_KEY", None)
        else:
            os.environ["MGS_VAULT_MASTER_KEY"] = self._old

    def _build(self, init):
        return build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password(_PWD), db_path=self.db,
                         init_database=init)

    def _wire(self, app):
        _LocalYouTube.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        app.atlas.bridge.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
                                            is_configured=True, authenticated=True, health=HealthStatus.OK,
                                            permissions_ok=True)
        app.atlas.navigator_real.register_adapter(platform="youtube", adapter=_LocalYouTube(
            vault=app.atlas.token_vault, account_reference_id=self.ref, timeout_seconds=2.0))

    def runner(self, app=None):
        app = app or self.app
        planner = OperationPlanner(bridge_hub=app.atlas.bridge, evidence_gate=app.atlas.evidence_gate)
        from bridge.execution.webhooks import RetryPolicy
        return OperationRunner(db_path=self.db, planner=planner, navigator=app.atlas.navigator_real,
                               escalation_desk=app.atlas.escalation_desk, actor="NAVIGATOR",
                               observability=app.atlas.execution_observability,
                               retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.2, max_delay_seconds=1.0))

    def two_steps(self, title="lançamento"):
        return [
            StepSpec(step_id="asset", platform="youtube", action="CREATE_ASSET", adapter_method="create_asset",
                     params={"title": title, "kind": "thumbnail"}, required_params=["title"],
                     success_result_keys=["id"], estimated_cost=0.0),
            StepSpec(step_id="publish", platform="youtube", action="PUBLISH_VIDEO", adapter_method="publish",
                     params={"title": title, "asset_ref": "asset"}, depends_on=["asset"], required_params=["title"],
                     success_result_keys=["id"], verify_method="publication_status",
                     verify_param_from_result={"id": "id"}, estimated_cost=0.0),
        ]

    def approve_all(self, run, run_id):
        _, state, _ = run.load(run_id)
        for st in state.values():
            if st["status"] == "WAITING_APPROVAL":
                self.app.atlas.escalation_desk.approve(st["escalation_id"], decision="ok", resolved_by=self.owner)

    def drive(self, run, run_id, rounds=6):
        status = None
        for _ in range(rounds):
            status = run.advance(run_id)
            self.approve_all(run, run_id)
        return status


class PlanningTests(_AppBase):
    def test_decomposition_order_dependencies_and_cycle(self):
        steps = list(reversed(self.two_steps()))
        plan = OperationPlanner(bridge_hub=self.app.atlas.bridge).plan(objective="o", steps=steps)
        self.assertEqual([p.spec.step_id for p in plan["steps"]], ["asset", "publish"])
        self.assertEqual(plan["requires_owner_approval"], ["asset", "publish"])
        self.assertEqual(plan["irreversible"], ["publish"])
        self.assertEqual(plan["estimated_cost"], 0.0)
        cyc = self.two_steps()
        cyc[0].depends_on = ["publish"]
        with self.assertRaises(PlanValidationError):
            OperationPlanner(bridge_hub=self.app.atlas.bridge).plan(objective="o", steps=cyc)

    def test_blocks_before_executing_missing_precondition_param_or_path(self):
        steps = self.two_steps(title="")
        steps[0].preconditions = [("channel", "chan_x", "margin")]
        steps.append(StepSpec(step_id="tiktok", platform="plataforma_sem_conector", action="PUBLISH_VIDEO",
                              adapter_method="publish", params={"title": "x"}))
        plan = OperationPlanner(bridge_hub=BridgeHub(), evidence_gate=self.app.atlas.evidence_gate).plan(
            objective="o", steps=steps)
        reasons = {p.spec.step_id: " ".join(p.blocked_reasons) for p in plan["steps"]}
        self.assertIn("faltam campos obrigatórios: title", reasons["asset"])
        self.assertIn("margin = UNKNOWN", reasons["asset"])
        self.assertIn("nenhum caminho legítimo", reasons["tiktok"])
        run = self.runner()
        run_id = run.create_run(objective="o", steps=self.two_steps(title=""), requested_by="ATLAS")
        self.assertEqual(run.advance(run_id), "BLOCKED")
        self.assertEqual(_Svc.posts, [])

    def test_routing_prefers_official_api_over_browser(self):
        for ct in (ConnectorType.BROWSER_AUTOMATION, ConnectorType.COMPUTER_USE_RPA):
            self.app.atlas.bridge.register_connector(platform="youtube", connector_type=ct, is_configured=True,
                                                     authenticated=True, health=HealthStatus.OK, permissions_ok=True)
        plan = OperationPlanner(bridge_hub=self.app.atlas.bridge).plan(objective="o", steps=self.two_steps())
        self.assertEqual({p.mechanism for p in plan["steps"]}, {ConnectorType.OFFICIAL_API.value})

    def test_forge_refuses_credentials_in_payload(self):
        prep = ExecutionPreparer().prepare(step_id="s", platform="youtube", action="PUBLISH_VIDEO",
                                           adapter_method="publish", params={"title": "x", "api_key": "k"})
        self.assertFalse(prep.ready)
        self.assertIn("TokenVault", prep.reason())


class ExecutionTests(_AppBase):
    def test_approval_required_then_executed_and_confirmed(self):
        run = self.runner()
        run_id = run.create_run(objective="lançar vídeo", steps=self.two_steps(), requested_by="ATLAS")
        self.assertEqual(run.advance(run_id), "WAITING_APPROVAL")
        self.assertEqual(_Svc.posts, [], "nada executa sem aprovação do Owner")
        self.assertEqual(self.drive(run, run_id), "SUCCEEDED")
        _, state, _ = run.load(run_id)
        self.assertEqual(state["asset"]["status"], "EXECUTED")
        self.assertEqual(state["publish"]["status"], "CONFIRMED")
        ev = state["publish"]["evidence"]
        for k in ("planned_at", "approval_requested_at", "approved_at", "sent_at", "executed_at", "confirmed_at"):
            self.assertIn(k, ev)
        self.assertEqual(ev["approved_by"], self.owner.subject)
        self.assertEqual(len(_Svc.posts), 2)

    def test_not_confirmed_is_not_success(self):
        _Svc.status_value = "processing"
        run = self.runner()
        run_id = run.create_run(objective="o", steps=self.two_steps(), requested_by="ATLAS")
        self.drive(run, run_id)
        _, state, _ = run.load(run_id)
        self.assertEqual(state["publish"]["status"], "EXECUTED", "executado, mas NÃO confirmado")

    def test_payload_changed_after_approval_is_refused(self):
        run = self.runner()
        run_id = run.create_run(objective="o", steps=self.two_steps(), requested_by="ATLAS")
        run.advance(run_id)
        self.approve_all(run, run_id)
        with db_module.connect(self.db) as conn:
            row = conn.execute("SELECT plan_json FROM navigator_operation_runs WHERE run_id=?", (run_id,)).fetchone()
            plan = json.loads(row["plan_json"])
            plan["steps"][0]["params"]["title"] = "ALTERADO após a aprovação"
            conn.execute("UPDATE navigator_operation_runs SET plan_json=? WHERE run_id=?", (json.dumps(plan), run_id))
        run.advance(run_id)
        _, state, _ = run.load(run_id)
        self.assertEqual(state["asset"]["status"], "BLOCKED")
        self.assertIn("ALTERADO", " ".join(state["asset"]["reasons"]))
        self.assertEqual(_Svc.posts, [])

    def test_restart_resumes_without_repeating_completed_steps(self):
        run = self.runner()
        run_id = run.create_run(objective="o", steps=self.two_steps(), requested_by="ATLAS")
        run.advance(run_id)
        self.approve_all(run, run_id)
        run.advance(run_id)                              # executa 'asset'; 'publish' pede aprovação
        self.assertEqual(len(_Svc.posts), 1)
        app2 = self._build(False)                        # RESTART da aplicação
        self._wire(app2)
        run2 = self.runner(app2)
        _, state, _ = run2.load(run_id)
        self.assertEqual((state["asset"]["status"], state["publish"]["status"]), ("EXECUTED", "WAITING_APPROVAL"))
        app2.atlas.escalation_desk.approve(state["publish"]["escalation_id"], decision="ok", resolved_by=self.owner)
        self.assertEqual(run2.advance(run_id), "SUCCEEDED")
        self.assertEqual([p["path"] for p in _Svc.posts], ["/assets", "/publish"], "nenhum passo repetido")
        run2.advance(run_id)
        self.assertEqual(len(_Svc.posts), 2, "avançar de novo é idempotente")

    def test_transient_retry_with_backoff_then_success(self):
        _Svc.script = [503]
        run = self.runner()
        steps = self.two_steps()[:1]
        run_id = run.create_run(objective="o", steps=steps, requested_by="ATLAS")
        run.advance(run_id)
        self.approve_all(run, run_id)
        run.advance(run_id)
        _, state, _ = run.load(run_id)
        self.assertEqual(state["asset"]["status"], "RETRYING")
        self.assertGreater(state["asset"]["next_attempt_at"], time.time())
        run.advance(run_id)                              # antes do backoff: não reenvia
        self.assertEqual(len(_Svc.posts), 0)
        time.sleep(0.35)
        self.assertEqual(run.advance(run_id), "SUCCEEDED")
        self.assertEqual(len(_Svc.posts), 1)

    def test_permanent_error_partial_plan_and_dependent_blocked(self):
        _Svc.script = [200, 400]
        run = self.runner()
        steps = self.two_steps()
        steps.append(StepSpec(step_id="after", platform="youtube", action="CREATE_ASSET", adapter_method="create_asset",
                              params={"title": "t2"}, depends_on=["publish"]))
        run_id = run.create_run(objective="o", steps=steps, requested_by="ATLAS")
        self.assertEqual(self.drive(run, run_id), "PARTIAL")
        _, state, _ = run.load(run_id)
        self.assertEqual((state["asset"]["status"], state["publish"]["status"], state["after"]["status"]),
                         ("EXECUTED", "FAILED", "BLOCKED"))

    def test_accepted_is_not_done_until_confirmed(self):
        _Svc.script = [202]
        _Svc.status_value = "processing"
        run = self.runner()
        steps = self.two_steps()[1:]
        steps[0].depends_on = []
        run_id = run.create_run(objective="o", steps=steps, requested_by="ATLAS")
        self.drive(run, run_id)
        _, state, _ = run.load(run_id)
        self.assertEqual(state["publish"]["status"], "ACCEPTED")
        self.assertNotEqual(run.advance(run_id), "SUCCEEDED")
        self.assertIn("last_verification", state["publish"]["evidence"])
        _Svc.status_value = "published"
        _, state, _ = run.load(run_id)
        self.assertNotEqual(run.advance(run_id), "SUCCEEDED", "dentro do backoff de verificação: não consulta")
        time.sleep(max(0.0, state["publish"]["next_verify_at"] - time.time()) + 0.05)
        self.assertEqual(run.advance(run_id), "SUCCEEDED")
        self.assertEqual(len(_Svc.posts), 1, "confirmar nunca reenvia")

    def test_cancel_stops_pending_and_flags_irreversible(self):
        run = self.runner()
        run_id = run.create_run(objective="o", steps=self.two_steps(), requested_by="ATLAS")
        self.assertEqual(run.cancel(run_id, reason="Owner cancelou"), "CANCELLED")
        run.advance(run_id)
        self.assertEqual(_Svc.posts, [])

    def test_secret_never_in_run_state_plan_or_observability(self):
        run = self.runner()
        run_id = run.create_run(objective="o", steps=self.two_steps(), requested_by="ATLAS")
        self.drive(run, run_id)
        self.assertTrue(all(p["auth"] == f"Bearer {SECRET}" for p in _Svc.posts))
        with db_module.connect(self.db) as conn:
            dump = json.dumps([dict(r) for t in ("navigator_operation_runs", "execution_observability", "audit_events",
                                                 "owner_approval_requests", "navigator_execution_claims")
                               for r in conn.execute(f"SELECT * FROM {t}").fetchall()], default=str)
        self.assertNotIn(SECRET, dump)
        self.assertIn("PUBLISH_VIDEO", dump)


class EndToEndAtlasFlowTests(_AppBase):
    def _ev(self, camp, clicks, sales):
        for _ in range(clicks):
            self._k += 1
            self.app.facade.record_sales_event(self.owner, channel_id="chan_1", event_type="click",
                                               value_type=ValueType.REAL, campaign_id=camp, idempotency_key=f"e{self._k}")
        for _ in range(sales):
            self._k += 1
            self.app.facade.record_sales_event(self.owner, channel_id="chan_1", event_type="sale",
                                               value_type=ValueType.REAL, amount=100.0, campaign_id=camp,
                                               idempotency_key=f"e{self._k}")

    def test_atlas_to_navigator_to_forge_to_hub_to_real_result_to_atlas(self):
        atlas, cycle = self.app.atlas, self.app.atlas.adaptive_cycle
        # 1) ATLAS: estratégia baseada em evidência (teste controlado com dados reais)
        self._ev("camp_t", 100, 2)
        self._ev("camp_c", 100, 2)
        atlas.sync_digital_twin()
        s = cycle.define_hypothesis(target_type="campaign", target_id="camp_t", metric_name="conversion_rate",
                                    hypothesis="novo gancho eleva conversão", expected_value=0.05,
                                    design=TestDesign.CONTROLLED, control_id="camp_c")
        for i in range(5):
            if i:
                cycle.start_round(s.strategy_id)
            self._ev("camp_t", 100, 5)
            self._ev("camp_c", 100, 2)
            atlas.sync_digital_twin()
            cycle.measure_round(s.strategy_id)
        rec = cycle.recommend(target_type="campaign", target_id="camp_t", metric_name="conversion_rate")
        self.assertEqual(rec["action"], Recommendation.SCALE)
        # 2) NAVIGATOR: plano com pré-condição factual do Gêmeo
        steps = self.two_steps(title="escalar gancho vencedor")
        steps[0].preconditions = [("campaign", "camp_t", "conversion_rate")]
        run = self.runner()
        run_id = run.create_run(objective=f"executar {rec['action'].value} de {rec['strategy_id']}", steps=steps,
                                requested_by="ATLAS")
        # 3) Owner aprova; 4) FORGE prepara; 5) Hub executa contra serviço real
        self.assertEqual(self.drive(run, run_id), "SUCCEEDED")
        _, state, _ = run.load(run_id)
        self.assertEqual(state["publish"]["status"], "CONFIRMED")
        # 6) resultado da execução volta ao ATLAS (observabilidade → Gêmeo)
        atlas.sync_digital_twin()
        integ = atlas.twin_state("integration", "youtube", "last_execution_success")
        self.assertEqual((integ.status.value, integ.fact_value), ("CURRENT", True))
        # 7) resultado de NEGÓCIO real alimenta o ciclo adaptativo
        cycle.start_round(s.strategy_id)
        self._ev("camp_t", 100, 6)
        self._ev("camp_c", 100, 2)
        atlas.sync_digital_twin()
        record = cycle.measure_round(s.strategy_id)
        self.assertTrue(record.direction_correct)
        self.assertEqual(cycle.get(s.strategy_id).status, StrategyStatus.VALIDATED)
        self.assertEqual(len(cycle.get(s.strategy_id).rounds), 6)

    def test_external_failure_never_becomes_success_for_atlas(self):
        _Svc.script = [400, 400]
        run = self.runner()
        run_id = run.create_run(objective="o", steps=self.two_steps()[:1], requested_by="ATLAS")
        self.assertEqual(self.drive(run, run_id), "FAILED")
        self.app.atlas.sync_digital_twin()
        integ = self.app.atlas.twin_state("integration", "youtube", "last_execution_success")
        self.assertEqual(integ.fact_value, False)


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------------
# Lacuna 7.1 — conector WEBHOOK (servidor real) com as mesmas garantias
# ----------------------------------------------------------------------
class _Hook(http.server.BaseHTTPRequestHandler):
    received: list = []
    delay = 0.0
    code = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Hook.received.append({"sig": self.headers.get("X-Webhook-Signature"), "body": json.loads(body)})
        time.sleep(_Hook.delay)
        try:
            self.send_response(_Hook.code)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def log_message(self, *a):
        pass


class WebhookConnectorTests(_AppBase):
    def setUp(self):
        super().setUp()
        _Hook.received, _Hook.delay, _Hook.code = [], 0.0, 200
        self.hook = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Hook)
        threading.Thread(target=self.hook.serve_forever, daemon=True).start()
        atlas = self.app.atlas
        atlas.bridge.register_connector(platform="crm_webhook", connector_type=ConnectorType.WEBHOOK,
                                        is_configured=True, authenticated=True, health=HealthStatus.OK,
                                        permissions_ok=True)
        self.hsecret = "HMAC-SEGREDO-webhook-55"
        ref = atlas.token_vault.store_token(platform="crm_webhook", token_type=TokenType.WEBHOOK_SECRET,
                                            value=self.hsecret)
        atlas.navigator_real.register_webhook(platform="crm_webhook",
                                              url=f"http://127.0.0.1:{self.hook.server_address[1]}/lead",
                                              vault=atlas.token_vault, secret_reference_id=ref, timeout_seconds=0.3)

    def tearDown(self):
        self.hook.shutdown()
        self.hook.server_close()
        super().tearDown()

    def _lead_step(self):
        return [StepSpec(step_id="lead", platform="crm_webhook", action="CREATE_LEAD", adapter_method="webhook",
                         params={"email": "joao@x.com"}, required_params=["email"])]

    def test_webhook_step_executes_once_signed_and_secret_not_leaked(self):
        run = self.runner()
        run_id = run.create_run(objective="registrar lead", steps=self._lead_step(), requested_by="ATLAS")
        self.assertEqual(self.drive(run, run_id), "SUCCEEDED")
        _, state, _ = run.load(run_id)
        self.assertEqual(state["lead"]["evidence"]["mechanism"], "WEBHOOK")
        self.assertEqual(len(_Hook.received), 1)
        self.assertTrue(_Hook.received[0]["sig"], "entrega assinada (HMAC)")
        with db_module.connect(self.db) as conn:
            dump = json.dumps([dict(r) for t in ("navigator_operation_runs", "audit_events", "execution_observability")
                               for r in conn.execute(f"SELECT * FROM {t}").fetchall()], default=str)
        self.assertNotIn(self.hsecret, dump)

    def test_webhook_ambiguous_timeout_never_redelivers(self):
        _Hook.delay = 0.8
        run = self.runner()
        run_id = run.create_run(objective="registrar lead", steps=self._lead_step(), requested_by="ATLAS")
        self.drive(run, run_id)
        time.sleep(0.9)
        _Hook.delay = 0.0
        for _ in range(3):
            run.advance(run_id)
        _, state, _ = run.load(run_id)
        self.assertEqual(state["lead"]["status"], "UNKNOWN")
        self.assertEqual(len(_Hook.received), 1, "lead NUNCA duplicado após timeout ambíguo")

    def test_dispatcher_does_not_retry_after_receiver_may_have_processed(self):
        from bridge.execution.webhooks import RetryPolicy, WebhookDispatcher
        _Hook.delay = 0.6
        r = WebhookDispatcher(retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01), timeout_seconds=0.2,
                              sleep_fn=lambda s: None).deliver(
            url=f"http://127.0.0.1:{self.hook.server_address[1]}/lead", payload={"x": 1})
        time.sleep(0.7)
        self.assertEqual((r.success, r.delivery, r.attempts), (False, "UNKNOWN", 1))
        self.assertEqual(len(_Hook.received), 1, "antes: 3 entregas para 1 envio lógico")

    def test_dispatcher_retries_only_when_certainly_not_processed(self):
        from bridge.execution.webhooks import RetryPolicy, WebhookDispatcher
        _Hook.code = 503
        r = WebhookDispatcher(retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01),
                              sleep_fn=lambda s: None).deliver(
            url=f"http://127.0.0.1:{self.hook.server_address[1]}/lead", payload={"x": 1})
        self.assertEqual((r.success, r.delivery, r.attempts), (False, "TRANSIENT", 3))
        _Hook.code = 400
        r2 = WebhookDispatcher(retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01),
                               sleep_fn=lambda s: None).deliver(
            url=f"http://127.0.0.1:{self.hook.server_address[1]}/lead", payload={"x": 1})
        self.assertEqual((r2.delivery, r2.attempts), ("REJECTED", 1))


# ----------------------------------------------------------------------
# Lacuna 7.2 — acionamento por FILA/WORKER com crash REAL do worker
# ----------------------------------------------------------------------
_WORKER = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
from api.bootstrap import build_app
from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.hub import ConnectorType, HealthStatus
from core.jobs.queue import JobQueue
from navigator.planning import OperationPlanner, OperationRunner, process_advance_jobs
from tests._auth_test_helpers import fast_hash_password
db, port, ref = sys.argv[2], sys.argv[3], sys.argv[4]
app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"), db_path=db,
                init_database=False)
class A(PlatformApiAdapter):
    platform = "youtube"; base_url = "http://127.0.0.1:" + port
    def create_asset(self, **kw): return self._request(action="CREATE_ASSET", method="POST", path="/assets", body=kw)
app.atlas.bridge.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True,
                                    authenticated=True, health=HealthStatus.OK, permissions_ok=True)
app.atlas.navigator_real.register_adapter(platform="youtube", adapter=A(vault=app.atlas.token_vault,
                                                                        account_reference_id=ref))
class CrashAfterDispatch:
    """morre DEPOIS de o serviço executar e o NavigatorReal consumir o claim,
    ANTES de o runner gravar o checkpoint EXECUTED e o job ser concluído."""
    def record_navigator_result(self, **kw): os._exit(9)
runner = OperationRunner(db_path=db, planner=OperationPlanner(bridge_hub=app.atlas.bridge),
                         navigator=app.atlas.navigator_real, escalation_desk=app.atlas.escalation_desk,
                         actor="NAVIGATOR", observability=CrashAfterDispatch())
process_advance_jobs(JobQueue(db_path=db), runner)
'''


class QueueWorkerCrashTests(_AppBase):
    def test_worker_crash_mid_advance_is_recovered_without_duplication(self):
        import subprocess
        import sys as _sys
        from core.jobs.queue import JobQueue
        from navigator.planning import process_advance_jobs, schedule_advance
        run = self.runner()
        run_id = run.create_run(objective="o", steps=self.two_steps()[:1], requested_by="ATLAS")
        run.advance(run_id)
        self.approve_all(run, run_id)
        schedule_advance(JobQueue(db_path=self.db), run_id, tick=1)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run([_sys.executable, "-c", _WORKER, root, self.db, str(self.server.server_address[1]),
                               self.ref], capture_output=True, timeout=90, env=dict(os.environ))
        self.assertEqual(proc.returncode, 9, proc.stderr.decode()[-800:])
        self.assertEqual(len(_Svc.posts), 1, "o serviço executou antes do crash do worker")
        _, state, _ = run.load(run_id)
        self.assertEqual(state["asset"]["status"], "RUNNING", "checkpoint mostra o passo interrompido")
        queue = JobQueue(db_path=self.db)                     # RESTART do worker
        recovered = queue.recover_stuck_jobs(timeout_seconds=0.0)
        self.assertEqual(len(recovered), 1, "job travado em RUNNING é recuperado após o restart")
        self.assertEqual(process_advance_jobs(queue, run), [(run_id, "SUCCEEDED")])
        _, state, _ = run.load(run_id)
        self.assertEqual(state["asset"]["status"], "EXECUTED")
        self.assertEqual(len(_Svc.posts), 1, "retomada NÃO reenviou (claim CONSUMED devolve o resultado)")
        self.assertEqual(process_advance_jobs(queue, run), [], "plano terminal não agenda novos ticks")


class QueueSchedulingTests(_AppBase):
    def test_active_plan_keeps_being_scheduled_until_terminal(self):
        from core.jobs.queue import JobQueue
        from navigator.planning import NAVIGATOR_ADVANCE_JOB, process_advance_jobs, schedule_advance
        run = self.runner()
        run_id = run.create_run(objective="o", steps=self.two_steps()[:1], requested_by="ATLAS")
        queue = JobQueue(db_path=self.db)
        schedule_advance(queue, run_id, tick=1)
        self.assertEqual(process_advance_jobs(queue, run), [(run_id, "WAITING_APPROVAL")])
        pending = [j for j in queue.all() if j.job_type == NAVIGATOR_ADVANCE_JOB and j.state.value == "QUEUED"]
        self.assertEqual([j.payload["tick"] for j in pending], [2], "plano ativo reagendado (tick 2)")
        self.assertEqual(_Svc.posts, [], "aguardando o Owner: nada executa")
        self.approve_all(run, run_id)
        self.assertEqual(process_advance_jobs(queue, run), [(run_id, "SUCCEEDED")])
        self.assertEqual(len(_Svc.posts), 1)
        self.assertEqual(process_advance_jobs(queue, run), [], "terminal: nenhum tick novo")
