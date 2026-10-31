"""
CORREÇÃO PÓS-AUDITORIA INDEPENDENTE (2026-09-24) — regressão dos 4
bloqueadores reproduzidos pela auditoria, com as MESMAS provas do relatório.
"""
import os
import tempfile
import types
import unittest

from api.bootstrap import build_app
from atlas.escalation import EscalationDesk
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from commercial.journey import JourneyConflict, OrderJourney
from core.coordination.mission import MissionStatus
from core.models import db as db_module
from core.reasoning.adapters import NavigatorResultVerifier
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role, principal_from_session
from navigator.models import NavigatorAction
from tests._auth_test_helpers import fast_hash_password
from tests.test_visao_evolutiva_e_agentic import make_atlas

OWNER = Principal(subject="owner", role=Role.OWNER, scopes={"*"})


# ------------------------------------------------------------------ A
class BlockerA_MockNeverVerified(unittest.TestCase):
    def test_mission_through_mock_fallback_is_never_verified(self):
        atlas, _ = make_atlas()
        m = atlas.delegate_mission(agent="NAVIGATOR", objective="coletar métricas")
        atlas.update_mission_status(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        trace, results = atlas.run_mission_with_reasoning(m.mission_id, m.task_id, navigator_tasks=[
            {"task_id": "t1", "description": "x", "platform": "youtube", "action": NavigatorAction.COLLECT_METRICS}])
        final = atlas.missions.get(m.mission_id, m.task_id)
        self.assertEqual(results[0]["result"]["evidence"]["mode"], "MOCK", "pré-condição: o caminho é MOCK")
        self.assertFalse(results[0]["verified"])
        self.assertFalse(trace.verified)
        self.assertNotEqual(final.status, MissionStatus.VERIFIED)
        self.assertIn("SIMULATED_NOT_VERIFIED", final.error)
        self.assertEqual(results[0]["attempts"], 1, "simulação não é re-executada")
        self.assertEqual(len(atlas.navigator.log.by_mission(m.mission_id)), 1)

    def test_verifier_contract(self):
        v = NavigatorResultVerifier()
        ok_real = {"execution_result": "SUCCESS", "evidence": {"mode": "REAL", "selected_mechanism": "OFFICIAL_API"}}
        self.assertTrue(v.verify(task={}, result=ok_real), "sucesso REAL continua verificável")
        self.assertFalse(v.verify(task={}, result={"execution_result": "SUCCESS", "evidence": {"mode": "MOCK"}}))
        self.assertFalse(v.verify(task={}, result={"execution_result": "SUCCESS",
                                                   "evidence": {"selected_mechanism": "MOCK_FALLBACK"}}))
        self.assertFalse(v.verify(task={}, result={"execution_result": "FAILED", "evidence": {"mode": "REAL"}}))

    def test_engine_never_repeats_reported_success(self):
        from core.reasoning.engine import FixedTaskPlanner, ReasoningEngine
        calls = []

        class Exec:
            def execute(self, *, task, tool):
                calls.append(1)
                return {"execution_result": "SUCCESS", "evidence": {"mode": "MOCK"}}
        engine = ReasoningEngine(planner=FixedTaskPlanner([{"task_id": "t", "description": "d"}]), executor=Exec(),
                                 verifier=NavigatorResultVerifier())
        _, results = engine.run(agent="a", objective="o", context={}, available_tools=[])
        self.assertEqual((len(calls), results[0]["verified"]), (1, False))


# ------------------------------------------------------------------ B
class _NavigatingOnlyBrowser:
    def __init__(self):
        self.visits = []

    def navigate_and_extract(self, *, url):
        self.visits.append(url)
        return types.SimpleNamespace(success=True, final_url=url, page_title="ok", reason="")


class BlockerB_BrowserNeverCompletesMutation(unittest.TestCase):
    def _nav(self):
        hub = BridgeHub()
        hub.register_connector(platform="x", connector_type=ConnectorType.BROWSER_AUTOMATION, is_configured=True,
                               authenticated=True, health=HealthStatus.OK, permissions_ok=True)
        self.desk, self.browser = EscalationDesk(AuditTrail()), _NavigatingOnlyBrowser()
        return NavigatorReal(bridge_hub=hub, escalation_desk=self.desk, browser_executor=self.browser, audit=AuditTrail())

    def test_publish_via_navigation_is_refused_and_approval_not_consumed(self):
        nav = self._nav()
        for action in ("PUBLISH_POST", "SEND_MESSAGE", "UPDATE_PRICE", "CREATE_CAMPAIGN", "BUY_ADS", "DELETE_VIDEO"):
            kw = dict(platform="x", action=action, criticality=ActionCriticality.CRITICAL, params={"t": 1}, actor="n",
                      idempotency_key=f"k-{action}")
            esc = nav.execute_action(**kw).escalation_id
            self.desk.approve(esc, decision="ok", resolved_by=OWNER)
            r = nav.execute_action(**kw, approved_escalation_id=esc, url="https://example.invalid/post")
            self.assertFalse(r.success, action)
            self.assertEqual(r.delivery_state, "NOT_SENT", action)
            self.assertIn("MUTÁVEL", r.reason)
        self.assertEqual(self.browser.visits, [], "nenhuma navegação usada como prova de mutação")

    def test_read_only_action_still_uses_browser(self):
        nav = self._nav()
        r = nav.execute_action(platform="x", action="GET_PAGE_STATUS", criticality=ActionCriticality.ROUTINE,
                               params={}, actor="n", url="https://example.invalid/p")
        self.assertTrue(r.success)
        self.assertEqual(r.delivery_state, "EXECUTED")
        self.assertEqual(len(self.browser.visits), 1)


# -------------------------------------------------------------- C / D
class _JourneyBase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db)
        app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"),
                        db_path=self.db, init_database=True)
        ow = principal_from_session(app.auth.current_session(app.auth.login("o@t.com", "TestPassword123")))
        self.j = OrderJourney(facade=app.facade, principal=ow, db_path=self.db)

    def tearDown(self):
        db_module.remove_db_file(self.db)

    def counts(self):
        with db_module.connect(self.db) as conn:
            return {r[0]: r[1] for r in conn.execute("SELECT entry_type, COUNT(*) FROM finance_entries GROUP BY entry_type")}


class BlockerC_PaymentReplayBoundToContent(_JourneyBase):
    def test_same_event_different_amount_is_conflict(self):
        self.j.start_checkout(order_id="o1", channel_id="c1", amount=100.0)
        self.assertEqual(self.j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=100.0,
                                                evidence="gw")["status"], "PAID")
        for tampered in (1.0, 999.0):
            with self.assertRaises(JourneyConflict):
                self.j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=tampered, evidence="gw")
        with self.assertRaises(JourneyConflict):
            self.j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=100.0, evidence="OUTRA evidência")
        self.assertEqual(self.j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=100.0,
                                                evidence="gw")["status"], "PAID", "mesmo ID + mesmo conteúdo = retry")
        self.assertEqual(self.counts(), {"revenue": 1})

    def test_refund_and_delivery_replays_are_bound_too(self):
        self.j.start_checkout(order_id="o1", channel_id="c1", amount=100.0)
        self.j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=100.0, evidence="gw")
        self.j.confirm_delivery(order_id="o1", delivery_event_id="d1", evidence="acesso #1")
        with self.assertRaises(JourneyConflict):
            self.j.confirm_delivery(order_id="o1", delivery_event_id="d1", evidence="acesso #2 (forjado)")
        self.j.refund(order_id="o1", gateway_event_id="r1", amount=50.0, reason="parcial")
        with self.assertRaises(JourneyConflict):
            self.j.refund(order_id="o1", gateway_event_id="r1", amount=100.0, reason="parcial")
        self.assertEqual(self.counts(), {"revenue": 1, "refund": 1})

    def test_fingerprints_survive_restart(self):
        self.j.start_checkout(order_id="o1", channel_id="c1", amount=100.0)
        self.j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=100.0, evidence="gw")
        j2 = OrderJourney(facade=self.j.facade, principal=self.j.principal, db_path=self.db)
        with self.assertRaises(JourneyConflict):
            j2.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=1.0, evidence="gw")


class BlockerD_OrderIdBoundToCheckoutPayload(_JourneyBase):
    def test_reused_order_id_with_different_payload_is_conflict(self):
        self.j.start_checkout(order_id="o2", channel_id="c1", amount=100.0, offer_id="of1")
        for changed in ({"channel_id": "other"}, {"amount": 1.0}, {"offer_id": "of2"}, {"campaign_id": "x"}):
            args = {"order_id": "o2", "channel_id": "c1", "amount": 100.0, "offer_id": "of1", **changed}
            with self.assertRaises(JourneyConflict, msg=str(changed)):
                self.j.start_checkout(**args)
        same = self.j.start_checkout(order_id="o2", channel_id="c1", amount=100.0, offer_id="of1")
        self.assertEqual((same["channel_id"], same["amount"]), ("c1", 100.0), "mesmo payload = idempotente")
        with db_module.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sales_events WHERE event_type='checkout'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
