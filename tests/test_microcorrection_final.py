import os
import tempfile
import unittest

from atlas.escalation import EscalationDesk
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.adapters.base import AdapterMode, AdapterResult
from core.models import db as db_module
from core.reasoning.cycle import ReasoningStage
from core.reasoning.engine import FixedTaskPlanner, PreferredToolSelector, ReasoningEngine
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role
from forge.engineer import ForgeEngineer, FixNotAuthorized, InvalidPipelineTransition
from forge.models import BlockClassification, DiagnosticStatus, RiskLevel
from forge.repair import FixPlan, RepairExecutor
from navigator.executor import NavigatorExecutor
from navigator.mechanism_executors import OfficialApiExecutor
from navigator.models import ExecutionResult, NavigatorAction

"""
Testes MICROCORRECAO FINAL - os 4 pontos residuais.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
ATLAS_PRINCIPAL = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})


def make_navigator(*, bridge=None):
    audit = AuditTrail()
    desk = EscalationDesk(audit)
    principal = Principal(subject="navigator@system", role=Role.NAVIGATOR, scopes={"*"})
    nav = NavigatorExecutor(principal=principal, escalation_desk=desk, audit=audit, bridge=bridge)
    return nav, desk, audit


class ToolsAllowedEndToEndTests(unittest.TestCase):
    def test_official_api_unavailable_mock_available_never_falls_back(self):
        hub = BridgeHub()
        hub.register_mock_fallback(platform="youtube")
        nav, _, _ = make_navigator(bridge=hub)
        entry = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, tools_allowed=["OFFICIAL_API"],
        )
        self.assertEqual(entry.result, ExecutionResult.BLOCKED.value)
        self.assertNotEqual(entry.evidence.get("selected_mechanism"), "MOCK_FALLBACK")

    def test_evidence_records_requested_allowed_selected_rejected(self):
        hub = BridgeHub()
        hub.register_mock_fallback(platform="youtube")
        nav, _, _ = make_navigator(bridge=hub)
        entry = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, tools_allowed=["OFFICIAL_API"],
        )
        self.assertEqual(entry.evidence["requested_tools"], ["OFFICIAL_API"])
        self.assertEqual(entry.evidence["allowed_tools"], ["OFFICIAL_API"])
        self.assertIsNone(entry.evidence["selected_tool"])
        self.assertIn("MOCK_FALLBACK", entry.evidence["rejected_tools"])

    def test_allowed_mechanism_available_executes_normally(self):
        hub = BridgeHub()
        hub.register_mock_fallback(platform="youtube")
        nav, _, _ = make_navigator(bridge=hub)
        entry = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, tools_allowed=["MOCK_FALLBACK"],
        )
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)
        self.assertEqual(entry.evidence["selected_mechanism"], "MOCK_FALLBACK")

    def test_browser_not_permitted_never_used(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.BROWSER_AUTOMATION,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        nav, _, _ = make_navigator(bridge=hub)
        entry = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, tools_allowed=["OFFICIAL_API"],
        )
        self.assertEqual(entry.result, ExecutionResult.BLOCKED.value)

    def test_connector_not_permitted_never_used(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.MCP_PLUGIN,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        nav, _, _ = make_navigator(bridge=hub)
        entry = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, tools_allowed=["WEBHOOK"],
        )
        self.assertEqual(entry.result, ExecutionResult.BLOCKED.value)

    def test_no_restriction_preserves_previous_behavior(self):
        hub = BridgeHub()
        hub.register_mock_fallback(platform="youtube")
        nav, _, _ = make_navigator(bridge=hub)
        entry = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)

    def test_tools_allowed_respected_for_critical_action_too(self):
        hub = BridgeHub()
        hub.register_mock_fallback(platform="youtube")
        nav, desk, _ = make_navigator(bridge=hub)
        pending = nav.execute(
            platform="youtube", action=NavigatorAction.PUBLISH, payload={}, tools_allowed=["OFFICIAL_API"],
        )
        desk.approve(pending.approval_used, decision="ok", resolved_by=OWNER)
        consumed = nav.execute(
            platform="youtube", action=NavigatorAction.PUBLISH, payload={}, tools_allowed=["OFFICIAL_API"],
        )
        self.assertEqual(consumed.result, ExecutionResult.BLOCKED.value)


class BridgeExecutorConsistencyTests(unittest.TestCase):
    def test_api_configured_calls_api_executor(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        nav, _, _ = make_navigator(bridge=hub)

        class FakeRealApi(OfficialApiExecutor):
            def execute(self, *, action, payload):
                return AdapterResult(mode=AdapterMode.REAL, success=True, data={})

        nav.register_mechanism_executor(platform="youtube", mechanism=ConnectorType.OFFICIAL_API, executor=FakeRealApi(provider_name="youtube"))
        entry = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)
        self.assertEqual(entry.evidence["actual_executor"], "FakeRealApi")
        self.assertEqual(entry.evidence["selected_mechanism"], "OFFICIAL_API")

    def test_connector_configured_calls_connector_executor(self):
        from navigator.mechanism_executors import ConnectorMcpExecutor

        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.MCP_PLUGIN,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        nav, _, _ = make_navigator(bridge=hub)

        class FakeRealConnector(ConnectorMcpExecutor):
            def execute(self, *, action, payload):
                return AdapterResult(mode=AdapterMode.REAL, success=True, data={})

        nav.register_mechanism_executor(platform="youtube", mechanism=ConnectorType.MCP_PLUGIN, executor=FakeRealConnector(provider_name="youtube"))
        entry = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)
        self.assertEqual(entry.evidence["actual_executor"], "FakeRealConnector")

    def test_browser_permitted_calls_browser_executor(self):
        from navigator.mechanism_executors import BrowserAutomationExecutor

        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.BROWSER_AUTOMATION,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        nav, _, _ = make_navigator(bridge=hub)

        class FakeRealBrowser(BrowserAutomationExecutor):
            def execute(self, *, action, payload):
                return AdapterResult(mode=AdapterMode.REAL, success=True, data={})

        nav.register_mechanism_executor(platform="youtube", mechanism=ConnectorType.BROWSER_AUTOMATION, executor=FakeRealBrowser(provider_name="youtube"))
        entry = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)
        self.assertEqual(entry.evidence["selected_mechanism"], "BROWSER_AUTOMATION")

    def test_mock_permitted_calls_mock_executor(self):
        hub = BridgeHub()
        hub.register_mock_fallback(platform="blog")
        nav, _, _ = make_navigator(bridge=hub)
        entry = nav.execute(platform="blog", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)
        self.assertEqual(entry.evidence["actual_executor"], "MockFallbackExecutor")
        self.assertEqual(entry.evidence["mode"], "MOCK")

    def test_api_selected_but_executor_returns_mock_fails(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        nav, _, _ = make_navigator(bridge=hub)

        class MisconfiguredExecutor(OfficialApiExecutor):
            def execute(self, *, action, payload):
                return AdapterResult(mode=AdapterMode.MOCK, success=True, data={})

        nav.register_mechanism_executor(platform="youtube", mechanism=ConnectorType.OFFICIAL_API, executor=MisconfiguredExecutor(provider_name="youtube"))
        entry = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.FAILED.value)
        self.assertIn("inconsistência", entry.error)

    def test_no_real_executor_available_blocked(self):
        hub = BridgeHub()
        nav, _, _ = make_navigator(bridge=hub)
        entry = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.BLOCKED.value)

    def test_default_official_api_stub_never_fakes_success(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        nav, _, _ = make_navigator(bridge=hub)
        entry = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.FAILED.value)


class _TwoAttemptExecutor:
    def __init__(self):
        self.calls = 0

    def execute(self, *, task, tool):
        self.calls += 1
        if self.calls == 1:
            return {"ok": False}
        return {"ok": True}


class _AlwaysInvalidExecutor:
    def __init__(self):
        self.calls = 0

    def execute(self, *, task, tool):
        self.calls += 1
        return {"ok": False}


class ReplanReexecuteTests(unittest.TestCase):
    def test_invalid_then_success_replans_and_reexecutes(self):
        executor = _TwoAttemptExecutor()
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x", "completion_criteria": ["ok"]}]),
            executor=executor,
        )
        trace, results = engine.run(agent="ATLAS", objective="x")
        self.assertEqual(executor.calls, 2)
        self.assertGreater(len(trace.steps_by_stage(ReasoningStage.REPLAN)), 0)
        self.assertTrue(results[0]["verified"])
        self.assertTrue(trace.verified)

    def test_three_failures_until_max_attempts_yields_failed(self):
        executor = _AlwaysInvalidExecutor()
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x", "completion_criteria": ["ok"]}]),
            executor=executor, max_replan_attempts=3,
        )
        trace, results = engine.run(agent="ATLAS", objective="x")
        self.assertEqual(executor.calls, 3)
        self.assertFalse(results[0]["verified"])
        self.assertFalse(results[0]["escalated"])
        self.assertFalse(trace.verified)

    def test_never_infinite_loop_respects_max_replan_attempts(self):
        executor = _AlwaysInvalidExecutor()
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x", "completion_criteria": ["ok"]}]),
            executor=executor, max_replan_attempts=5,
        )
        engine.run(agent="ATLAS", objective="x")
        self.assertEqual(executor.calls, 5)

    def test_tool_switch_only_among_authorized_tools(self):
        class ToolRecorder:
            def __init__(self):
                self.tools_used = []

            def execute(self, *, task, tool):
                self.tools_used.append(tool)
                return {"ok": False}

        executor = ToolRecorder()
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x", "completion_criteria": ["ok"]}]),
            executor=executor, tool_selector=PreferredToolSelector(), max_replan_attempts=3,
        )
        engine.run(agent="ATLAS", objective="x", available_tools=["tool_a", "tool_b"])
        self.assertTrue(set(executor.tools_used).issubset({"tool_a", "tool_b"}))

    def test_each_attempt_recorded_separately_in_history(self):
        executor = _TwoAttemptExecutor()
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x", "completion_criteria": ["ok"]}]),
            executor=executor,
        )
        trace, results = engine.run(agent="ATLAS", objective="x")
        execute_steps = trace.steps_by_stage(ReasoningStage.EXECUTE)
        self.assertEqual(len(execute_steps), 2)
        self.assertIn("tentativa 1", execute_steps[0].summary)
        self.assertIn("tentativa 2", execute_steps[1].summary)

    def test_idempotency_and_critical_action_preserved_during_replan(self):
        from atlas.director import AtlasDirector
        from commercial.multigerenciador import MultiGerenciadorComercial
        from core.governance.facade import GovernedFacade
        from core.jobs.queue import JobQueue
        from core.reasoning.adapters import NavigatorResultVerifier, NavigatorTaskExecutor

        mgc = MultiGerenciadorComercial()
        audit = AuditTrail()
        facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
        atlas = AtlasDirector(facade=facade, principal=ATLAS_PRINCIPAL)

        adapter = NavigatorTaskExecutor(atlas.navigator, mission_id="m1", task_id="t1")
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "publicar", "platform": "youtube", "action": NavigatorAction.PUBLISH, "payload": {}}]),
            executor=adapter, verifier=NavigatorResultVerifier(), max_replan_attempts=3,
        )
        engine.run(agent="ATLAS", objective="x")

        pending = atlas.escalation_desk.pending()
        self.assertEqual(len(pending), 1)


def make_forge(*, db_path=None):
    audit = AuditTrail(db_path=db_path)
    principal = Principal(subject="forge@system", role=Role.FORGE, scopes={"*"})
    return ForgeEngineer(principal=principal, audit=audit, db_path=db_path), audit


class TechnicalBlockReleaseGatingTests(unittest.TestCase):
    def test_authorization_alone_never_releases_technical_block(self):
        forge, _ = make_forge()
        d = forge.block_operation(operation_name="op1", reason="risco técnico", evidence={})
        with self.assertRaises(InvalidPipelineTransition):
            forge.release_operation_block(d.diagnostic_id, authorized_by=OWNER)

    def test_fix_without_test_never_releases(self):
        forge, _ = make_forge()
        d = forge.detect(issue_summary="bug", evidence={})
        forge.diagnose(d.diagnostic_id, analysis={})
        forge.identify_root_cause(d.diagnostic_id, root_cause="x")
        forge.propose_fix(d.diagnostic_id, proposed_fix="x")
        forge.assess_risk(d.diagnostic_id, risk_level=RiskLevel.LOW)
        forge.approve_fix(d.diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(d.diagnostic_id)
        store = {"x": 1}
        executor = RepairExecutor(resource_store=store, allowed_resources={"x"})
        plan = FixPlan.new(diagnostic_id=d.diagnostic_id, executor_name="forge@system", changes={"x": 2})
        forge.execute_repair(d.diagnostic_id, plan=plan, executor=executor)
        d = forge.store.get(d.diagnostic_id)
        d.blocked_operation = "op2"
        forge.store.update(d)
        with self.assertRaises(InvalidPipelineTransition):
            forge.release_operation_block(d.diagnostic_id, authorized_by=OWNER)

    def test_test_without_validation_never_releases(self):
        forge, _ = make_forge()
        d = forge.detect(issue_summary="bug", evidence={})
        forge.diagnose(d.diagnostic_id, analysis={})
        forge.identify_root_cause(d.diagnostic_id, root_cause="x")
        forge.propose_fix(d.diagnostic_id, proposed_fix="x")
        forge.assess_risk(d.diagnostic_id, risk_level=RiskLevel.LOW)
        forge.approve_fix(d.diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(d.diagnostic_id)
        store = {"x": 1}
        executor = RepairExecutor(resource_store=store, allowed_resources={"x"})
        plan = FixPlan.new(diagnostic_id=d.diagnostic_id, executor_name="forge@system", changes={"x": 2})
        forge.execute_repair(d.diagnostic_id, plan=plan, executor=executor)
        forge.start_testing(d.diagnostic_id)
        forge.record_test_result(d.diagnostic_id, passed=True, evidence={"a": 1})
        d = forge.store.get(d.diagnostic_id)
        d.blocked_operation = "op3"
        forge.store.update(d)
        with self.assertRaises(InvalidPipelineTransition):
            forge.release_operation_block(d.diagnostic_id, authorized_by=OWNER)

    def test_full_flow_releases(self):
        forge, _ = make_forge()
        d = forge.detect(issue_summary="bug", evidence={})
        forge.diagnose(d.diagnostic_id, analysis={})
        forge.identify_root_cause(d.diagnostic_id, root_cause="x")
        forge.propose_fix(d.diagnostic_id, proposed_fix="x")
        forge.assess_risk(d.diagnostic_id, risk_level=RiskLevel.LOW)
        forge.approve_fix(d.diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(d.diagnostic_id)
        store = {"x": 1}
        executor = RepairExecutor(resource_store=store, allowed_resources={"x"})
        plan = FixPlan.new(diagnostic_id=d.diagnostic_id, executor_name="forge@system", changes={"x": 2})
        forge.execute_repair(d.diagnostic_id, plan=plan, executor=executor)
        forge.start_testing(d.diagnostic_id)
        forge.record_test_result(d.diagnostic_id, passed=True, evidence={"a": 1})
        forge.record_non_regression_result(d.diagnostic_id, passed=True, evidence={"b": 1})
        d = forge.store.get(d.diagnostic_id)
        d.blocked_operation = "op4"
        forge.store.update(d)
        released = forge.release_operation_block(d.diagnostic_id, authorized_by=OWNER)
        self.assertEqual(released.status, DiagnosticStatus.RELEASED)

    def test_administrative_block_uses_simplified_flow_with_justification(self):
        forge, _ = make_forge()
        d = forge.block_operation(
            operation_name="pause1", reason="pausa deliberada", evidence={}, classification=BlockClassification.ADMINISTRATIVE,
        )
        with self.assertRaises(ValueError):
            forge.release_operation_block(d.diagnostic_id, authorized_by=OWNER)
        released = forge.release_operation_block(d.diagnostic_id, authorized_by=OWNER, justification="operação retomada")
        self.assertEqual(released.status, DiagnosticStatus.RELEASED)


class RealRollbackTests(unittest.TestCase):
    def _apply_fix(self, forge, *, store, changes):
        d = forge.detect(issue_summary="bug", evidence={})
        forge.diagnose(d.diagnostic_id, analysis={})
        forge.identify_root_cause(d.diagnostic_id, root_cause="x")
        forge.propose_fix(d.diagnostic_id, proposed_fix="x")
        forge.assess_risk(d.diagnostic_id, risk_level=RiskLevel.LOW)
        forge.approve_fix(d.diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(d.diagnostic_id)
        executor = RepairExecutor(resource_store=store, allowed_resources=set(changes.keys()))
        plan = FixPlan.new(diagnostic_id=d.diagnostic_id, executor_name="forge@system", changes=changes)
        forge.execute_repair(d.diagnostic_id, plan=plan, executor=executor)
        return d.diagnostic_id, executor

    def test_rollback_restores_original_value(self):
        forge, _ = make_forge()
        store = {"value": "A"}
        diagnostic_id, executor = self._apply_fix(forge, store=store, changes={"value": "B"})
        self.assertEqual(store["value"], "B")
        forge.rollback(diagnostic_id, reason="regressão", executor=executor)
        self.assertEqual(store["value"], "A")

    def test_rollback_after_restart(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            forge1, _ = make_forge(db_path=db_path)
            store = {"value": "X"}
            diagnostic_id, _ = self._apply_fix(forge1, store=store, changes={"value": "Y"})

            forge2, _ = make_forge(db_path=db_path)
            executor2 = RepairExecutor(resource_store=store, allowed_resources={"value"})
            forge2.rollback(diagnostic_id, reason="rollback após restart", executor=executor2)
            self.assertEqual(store["value"], "X")
        finally:
            if os.path.exists(db_path):
                db_module.remove_db_file(db_path)

    def test_rollback_with_partial_failure_marks_rollback_failed(self):
        forge, _ = make_forge()
        store = {"value": "A"}
        diagnostic_id, _ = self._apply_fix(forge, store=store, changes={"value": "B"})

        class FailingStore(dict):
            def __setitem__(self, key, value):
                raise RuntimeError("falha ao restaurar")

        failing_executor = RepairExecutor(resource_store=FailingStore(store), allowed_resources={"value"})
        result = forge.rollback(diagnostic_id, reason="teste", executor=failing_executor)
        self.assertEqual(result.status, DiagnosticStatus.ROLLBACK_FAILED)

    def test_rollback_failed_keeps_operation_blocked(self):
        forge, _ = make_forge()
        store = {"value": "A"}
        diagnostic_id, _ = self._apply_fix(forge, store=store, changes={"value": "B"})
        d = forge.store.get(diagnostic_id)
        d.blocked_operation = "op_rollback_test"
        forge.store.update(d)

        class FailingStore(dict):
            def __setitem__(self, key, value):
                raise RuntimeError("falha ao restaurar")

        failing_executor = RepairExecutor(resource_store=FailingStore(store), allowed_resources={"value"})
        forge.rollback(diagnostic_id, reason="teste", executor=failing_executor)
        self.assertTrue(forge.is_operation_blocked("op_rollback_test"))

    def test_rollback_without_checkpoint_now_returns_no_rollback_needed(self):
        """CORREÇÃO (microcorreção finalíssima): comportamento
        atualizado — rollback sem qualquer fix aplicado nunca mais é
        ROLLED_BACK."""
        forge, _ = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        result = forge.rollback(d.diagnostic_id, reason="abortado antes de qualquer fix")
        self.assertEqual(result.status, DiagnosticStatus.NO_ROLLBACK_NEEDED)
        self.assertFalse(result.evidence.get("rollback_restored"))

    def test_multiple_fixes_rollback_restores_pre_latest_fix_state(self):
        forge, _ = make_forge()
        store = {"value": "A"}
        diagnostic_id, executor1 = self._apply_fix(forge, store=store, changes={"value": "B"})
        forge.start_testing(diagnostic_id)
        forge.record_test_result(diagnostic_id, passed=False, evidence={"reason": "regrediu"})
        forge.resume_from_block(diagnostic_id, target_status=DiagnosticStatus.FIX_APPLYING, authorized_by=ATLAS_PRINCIPAL)
        executor2 = RepairExecutor(resource_store=store, allowed_resources={"value"})
        plan2 = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="forge@system", changes={"value": "C"})
        forge.execute_repair(diagnostic_id, plan=plan2, executor=executor2)
        self.assertEqual(store["value"], "C")

        forge.rollback(diagnostic_id, reason="reverter segunda correção", executor=executor2)
        self.assertEqual(store["value"], "B")


if __name__ == "__main__":
    unittest.main()


class AtlasToExecutorEndToEndPropagationTests(unittest.TestCase):
    """Item 1 — cadeia completa ATLAS -> MULTIAGENT COORDINATION ->
    NAVIGATOR -> BRIDGE -> EXECUTION PATH -> ADAPTER/EXECUTOR REAL."""

    def test_mission_tools_allowed_propagates_to_bridge_selection(self):
        from atlas.director import AtlasDirector
        from commercial.multigerenciador import MultiGerenciadorComercial
        from core.governance.facade import GovernedFacade
        from core.jobs.queue import JobQueue

        mgc = MultiGerenciadorComercial()
        audit = AuditTrail()
        facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
        atlas = AtlasDirector(facade=facade, principal=ATLAS_PRINCIPAL)

        from core.coordination.mission import MissionStatus

        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="coletar métricas", tools_allowed=["OFFICIAL_API"])
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)

        atlas.run_mission_with_reasoning(
            mission.mission_id, mission.task_id,
            navigator_tasks=[{"task_id": "t1", "description": "x", "platform": "youtube", "action": NavigatorAction.COLLECT_METRICS}],
        )

        final = atlas.missions.get(mission.mission_id, mission.task_id)
        self.assertNotEqual(final.status, MissionStatus.VERIFIED)
        self.assertEqual(final.status, MissionStatus.FAILED)

    def test_mission_with_allowed_mock_still_verifies(self):
        from atlas.director import AtlasDirector
        from commercial.multigerenciador import MultiGerenciadorComercial
        from core.governance.facade import GovernedFacade
        from core.jobs.queue import JobQueue
        from core.coordination.mission import MissionStatus

        mgc = MultiGerenciadorComercial()
        audit = AuditTrail()
        facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
        atlas = AtlasDirector(facade=facade, principal=ATLAS_PRINCIPAL)

        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="coletar métricas", tools_allowed=["MOCK_FALLBACK"])
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)

        atlas.run_mission_with_reasoning(
            mission.mission_id, mission.task_id,
            navigator_tasks=[{"task_id": "t1", "description": "x", "platform": "youtube", "action": NavigatorAction.COLLECT_METRICS}],
        )

        final = atlas.missions.get(mission.mission_id, mission.task_id)
        # CORREÇÃO PÓS-AUDITORIA (bloqueador A): mock PERMITIDO pode RODAR
        # (não é bloqueado pela restrição de ferramentas), mas simulação NUNCA
        # é VERIFIED — antes este teste exigia VERIFIED (codificava o defeito).
        self.assertNotEqual(final.status, MissionStatus.BLOCKED, "mock permitido executa")
        self.assertEqual(len(atlas.navigator.log.by_mission(mission.mission_id)), 1)
        self.assertNotEqual(final.status, MissionStatus.VERIFIED)
        self.assertIn("SIMULATED_NOT_VERIFIED", final.error or "")
