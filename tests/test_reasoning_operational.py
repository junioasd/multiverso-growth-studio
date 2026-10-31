import unittest

from atlas.director import AtlasDirector
from commercial.multigerenciador import MultiGerenciadorComercial
from core.coordination.mission import MissionStatus
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.reasoning.adapters import (
    ForgeDetectionVerifier,
    ForgeDiagnosticExecutor,
    NavigatorResultVerifier,
    NavigatorTaskExecutor,
)
from core.reasoning.cycle import ReasoningStage
from core.reasoning.engine import FixedTaskPlanner, NullExecutor, PreferredToolSelector, ReasoningEngine
from core.reasoning.self_correction import SelfCorrectionLoop
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role
from forge.models import DiagnosticStatus
from navigator.models import ExecutionResult, NavigatorAction

"""
Testes RACIOCINIO OPERACIONAL (item 4) - ponta a ponta.
"""

ATLAS_PRINCIPAL = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})


def make_atlas():
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    return AtlasDirector(facade=facade, principal=ATLAS_PRINCIPAL), audit


class _StubExecutor:
    def __init__(self, result: dict):
        self.result = result

    def execute(self, *, task, tool):
        return dict(self.result)


class _FlakyExecutor:
    def __init__(self, *, fail_times: int, final_result: dict):
        self.fail_times = fail_times
        self.calls = 0
        self.final_result = final_result

    def execute(self, *, task, tool):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"falha simulada #{self.calls}")
        return dict(self.final_result)


class _AlwaysFailingExecutor:
    def execute(self, *, task, tool):
        raise RuntimeError("falha persistente")


class _AlwaysFalseVerifier:
    def verify(self, *, task, result):
        return False


class DefaultComponentsAreDeterministicTests(unittest.TestCase):
    def test_fixed_task_planner_never_invents_tasks(self):
        planner = FixedTaskPlanner([{"task_id": "t1", "description": "x"}])
        tasks = planner.decompose(objective="qualquer coisa", context={})
        self.assertEqual(tasks, [{"task_id": "t1", "description": "x"}])

    def test_fixed_task_planner_fallback_is_minimal_and_honest(self):
        planner = FixedTaskPlanner()
        tasks = planner.decompose(objective="fazer algo", context={})
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["description"], "fazer algo")

    def test_null_executor_clearly_labeled_mock(self):
        executor = NullExecutor()
        result = executor.execute(task={}, tool=None)
        self.assertEqual(result["mode"], "MOCK")

    def test_tool_selector_never_invents_unavailable_tool(self):
        selector = PreferredToolSelector()
        selected = selector.select(task={"preferred_tool": "inexistente"}, available_tools=["real_tool"])
        self.assertEqual(selected, "real_tool")


class EndToEndReasoningCycleTests(unittest.TestCase):
    def test_full_cycle_all_stages_present_on_success(self):
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x", "completion_criteria": ["ok"]}]),
            executor=_StubExecutor({"ok": True}),
        )
        trace, results = engine.run(agent="ATLAS", objective="testar ciclo", available_tools=["tool_a"])
        stages_seen = {s.stage for s in trace.steps}
        for required in (
            ReasoningStage.OBSERVE, ReasoningStage.UNDERSTAND, ReasoningStage.PLAN, ReasoningStage.DECOMPOSE,
            ReasoningStage.SELECT_TOOLS, ReasoningStage.EXECUTE, ReasoningStage.VERIFY, ReasoningStage.CRITIQUE,
            ReasoningStage.LEARN, ReasoningStage.REPORT,
        ):
            self.assertIn(required, stages_seen)
        self.assertTrue(trace.verified)
        self.assertTrue(results[0]["verified"])

    def test_unverified_result_triggers_replan_stage(self):
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x", "completion_criteria": ["ok"]}]),
            executor=_StubExecutor({"ok": False}),
        )
        trace, results = engine.run(agent="ATLAS", objective="x")
        stages_seen = {s.stage for s in trace.steps}
        self.assertIn(ReasoningStage.REPLAN, stages_seen)
        self.assertFalse(results[0]["verified"])
        self.assertFalse(trace.verified)

    def test_transient_failure_triggers_correct_stage_then_succeeds(self):
        flaky = _FlakyExecutor(fail_times=1, final_result={"ok": True})
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x", "completion_criteria": ["ok"]}]),
            executor=flaky, self_correction=SelfCorrectionLoop(max_attempts=3),
        )
        trace, results = engine.run(agent="ATLAS", objective="x")
        self.assertTrue(results[0]["verified"])
        self.assertGreater(trace.correction_attempts, 0)

    def test_persistent_failure_escalates_never_verifies(self):
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x"}]), executor=_AlwaysFailingExecutor(),
        )
        trace, results = engine.run(agent="ATLAS", objective="x")
        self.assertTrue(results[0]["escalated"])
        self.assertFalse(results[0]["verified"])
        self.assertFalse(trace.verified)

    def test_never_declares_success_without_evidence(self):
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x"}]), executor=_StubExecutor({}),
            verifier=_AlwaysFalseVerifier(),
        )
        trace, results = engine.run(agent="ATLAS", objective="x")
        self.assertFalse(trace.verified)
        self.assertEqual(len(trace.steps_by_stage(ReasoningStage.VERIFY)), 0)

    def test_multiple_subtasks_processed_independently(self):
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([
                {"task_id": "t1", "description": "a", "completion_criteria": ["ok"]},
                {"task_id": "t2", "description": "b", "completion_criteria": ["ok"]},
            ]),
            executor=_StubExecutor({"ok": True}),
        )
        trace, results = engine.run(agent="ATLAS", objective="x")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["verified"] for r in results))


class NavigatorIntegrationTests(unittest.TestCase):
    def test_navigator_executor_adapter_runs_real_execution(self):
        atlas, _ = make_atlas()
        adapter = NavigatorTaskExecutor(atlas.navigator, mission_id="m1", task_id="t1")
        result = adapter.execute(task={"platform": "youtube", "action": NavigatorAction.COLLECT_METRICS}, tool=None)
        self.assertEqual(result["execution_result"], ExecutionResult.SUCCESS.value)

    def test_navigator_verifier_never_verifies_escalated(self):
        atlas, _ = make_atlas()
        adapter = NavigatorTaskExecutor(atlas.navigator, mission_id="m2", task_id="t2")
        result = adapter.execute(task={"platform": "youtube", "action": NavigatorAction.PUBLISH}, tool=None)
        verifier = NavigatorResultVerifier()
        self.assertFalse(verifier.verify(task={}, result=result))
        self.assertEqual(result["execution_result"], ExecutionResult.ESCALATED.value)

    def test_atlas_mission_reaches_verified_only_with_real_success(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="coletar metricas")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        trace, results = atlas.run_mission_with_reasoning(
            mission.mission_id, mission.task_id,
            navigator_tasks=[{"task_id": "t1", "description": "x", "platform": "youtube", "action": NavigatorAction.COLLECT_METRICS}],
        )
        final = atlas.missions.get(mission.mission_id, mission.task_id)
        # CORREÇÃO PÓS-AUDITORIA (bloqueador A): o único mecanismo aqui é
        # MOCK_FALLBACK ("nenhuma chamada real"). Antes este teste exigia
        # VERIFIED — era a codificação do defeito. A intenção principal
        # (execução AUTORIZADA e ciclo de vida) é mantida; simulação nunca
        # é VERIFIED.
        self.assertNotEqual(final.status, MissionStatus.BLOCKED, "execução autorizada, não bloqueada")
        self.assertEqual(final.status, MissionStatus.FAILED)
        self.assertIn("SIMULATED_NOT_VERIFIED", final.error or "")
        self.assertNotEqual(final.status, MissionStatus.VERIFIED)
        self.assertFalse(trace.verified, "simulação não verifica o trace")

    def test_atlas_mission_never_verified_when_critical_action_pending(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="publicar video")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        atlas.run_mission_with_reasoning(
            mission.mission_id, mission.task_id,
            navigator_tasks=[{"task_id": "t1", "description": "publicar", "platform": "youtube", "action": NavigatorAction.PUBLISH}],
        )
        final = atlas.missions.get(mission.mission_id, mission.task_id)
        self.assertEqual(final.status, MissionStatus.FAILED)
        self.assertNotEqual(final.status, MissionStatus.VERIFIED)

    def test_mission_reasoning_correlates_by_mission_task_id(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        atlas.run_mission_with_reasoning(
            mission.mission_id, mission.task_id,
            navigator_tasks=[{"task_id": "t1", "description": "x", "platform": "blog", "action": NavigatorAction.CHECK_STATUS}],
        )
        log_entries = atlas.navigator.log.by_mission(mission.mission_id)
        self.assertEqual(len(log_entries), 1)
        self.assertEqual(log_entries[0].task_id, mission.task_id)


class ForgeIntegrationTests(unittest.TestCase):
    def test_forge_executor_adapter_creates_real_diagnostic(self):
        atlas, _ = make_atlas()
        adapter = ForgeDiagnosticExecutor(atlas.forge)
        result = adapter.execute(task={"issue_summary": "bug via reasoning"}, tool=None)
        self.assertEqual(result["status"], DiagnosticStatus.DETECTED.value)
        recovered = atlas.forge.store.get(result["diagnostic_id"])
        self.assertEqual(recovered.issue_summary, "bug via reasoning")

    def test_forge_verifier_confirms_detection(self):
        verifier = ForgeDetectionVerifier()
        self.assertTrue(verifier.verify(task={}, result={"status": "DETECTED"}))
        self.assertFalse(verifier.verify(task={}, result={"status": "BLOCKED"}))


if __name__ == "__main__":
    unittest.main()
