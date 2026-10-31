import unittest

from atlas.advanced_planning import (
    AdvancedPlanner,
    CyclicDependencyError,
    PlanTask,
    UnknownDependencyError,
)
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from core.reasoning.cycle import ReasoningStage, ReasoningTrace, UnverifiedSuccessClaim
from core.reasoning.self_correction import (
    ErrorClass,
    SelfCorrectionLoop,
    classify_error,
)
from core.security.rbac import AccessDenied

"""
Testes RACIOCINIO - decomposicao de tarefa, planejamento,
replanejamento, selecao de ferramenta, validacao, autocorrecao,
escalonamento.
"""


class TaskDecompositionTests(unittest.TestCase):
    def test_objective_decomposed_into_ordered_tasks(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="Lancar produto", tasks=[
            {"task_id": "pesquisa", "description": "Pesquisar mercado"},
            {"task_id": "oferta", "description": "Selecionar oferta", "depends_on": ["pesquisa"]},
            {"task_id": "producao", "description": "Produzir conteudo", "depends_on": ["oferta"]},
        ])
        order = [t.task_id for t in plan.topological_order()]
        self.assertEqual(order, ["pesquisa", "oferta", "producao"])

    def test_unknown_dependency_rejected(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "a", "description": "a"}])
        plan.tasks["a"].depends_on = ["fantasma"]
        with self.assertRaises(UnknownDependencyError):
            plan.topological_order()


class PlanningTests(unittest.TestCase):
    def test_parallel_and_sequential_tasks_grouped_correctly(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="Campanha", tasks=[
            {"task_id": "t1", "description": "base", "priority": 1},
            {"task_id": "t2", "description": "paralela A", "depends_on": ["t1"]},
            {"task_id": "t3", "description": "paralela B", "depends_on": ["t1"]},
            {"task_id": "t4", "description": "final", "depends_on": ["t2", "t3"]},
        ])
        groups = plan.parallel_groups()
        self.assertEqual([t.task_id for t in groups[0]], ["t1"])
        self.assertEqual({t.task_id for t in groups[1]}, {"t2", "t3"})
        self.assertEqual([t.task_id for t in groups[2]], ["t4"])

    def test_priorities_deadlines_resources_tools_risks_preserved(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{
            "task_id": "a", "description": "a", "priority": 1, "deadline": 12345.0,
            "resources": ["r1"], "tools": ["radar"], "risks": ["baixo estoque"],
            "completion_criteria": ["evidencia coletada"], "fallback": "usar plano B",
            "max_retries": 5, "checkpoints": ["cp1"], "quality_gate": "video_quality_gate",
        }])
        task = plan.tasks["a"]
        self.assertEqual(task.deadline, 12345.0)
        self.assertEqual(task.resources, ["r1"])
        self.assertEqual(task.quality_gate, "video_quality_gate")

    def test_cyclic_dependency_rejected(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "a", "description": "a"}])
        plan.tasks["a"].depends_on = ["b"]
        plan.tasks["b"] = PlanTask(task_id="b", description="b", depends_on=["a"])
        with self.assertRaises(CyclicDependencyError):
            plan.topological_order()

    def test_empty_objective_rejected(self):
        planner = AdvancedPlanner()
        with self.assertRaises(ValueError):
            planner.build_plan(objective="   ", tasks=[])


class ReplanningTests(unittest.TestCase):
    def test_replan_adds_task_preserving_existing(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "inicial"}])
        planner.replan(plan, add_tasks=[{"task_id": "t2", "description": "nova", "depends_on": ["t1"]}])
        self.assertEqual(set(plan.tasks.keys()), {"t1", "t2"})
        self.assertEqual(plan.revision, 1)

    def test_replan_removes_task(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[
            {"task_id": "t1", "description": "a"}, {"task_id": "t2", "description": "b"},
        ])
        planner.replan(plan, remove_task_ids=["t2"])
        self.assertEqual(set(plan.tasks.keys()), {"t1"})

    def test_replan_revalidates_and_rejects_new_cycle(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "a", "description": "a"}])
        with self.assertRaises((CyclicDependencyError, UnknownDependencyError)):
            planner.replan(plan, add_tasks=[
                {"task_id": "b", "description": "b", "depends_on": ["a", "c"]},
                {"task_id": "c", "description": "c", "depends_on": ["b"]},
            ])


class ToolSelectionTests(unittest.TestCase):
    def test_selects_highest_priority_available_mechanism(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="instagram", connector_type=ConnectorType.WEBHOOK,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        hub.register_connector(
            platform="instagram", connector_type=ConnectorType.MCP_PLUGIN,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        best = hub.best_available_execution_path(platform="instagram")
        self.assertEqual(best.connector_type, ConnectorType.MCP_PLUGIN)

    def test_no_mechanism_available_returns_none(self):
        hub = BridgeHub()
        self.assertIsNone(hub.best_available_execution_path(platform="x"))


class ValidationTests(unittest.TestCase):
    def test_trace_never_verifies_without_evidence(self):
        trace = ReasoningTrace.start(agent="ATLAS", objective="testar")
        with self.assertRaises(UnverifiedSuccessClaim):
            trace.mark_verified(evidence={})

    def test_trace_verifies_with_evidence(self):
        trace = ReasoningTrace.start(agent="ATLAS", objective="testar")
        trace.mark_verified(evidence={"proof": "ok"})
        self.assertTrue(trace.verified)

    def test_trace_records_every_stage(self):
        trace = ReasoningTrace.start(agent="ATLAS", objective="testar")
        trace.record(ReasoningStage.PLAN, "plano criado")
        trace.record(ReasoningStage.EXECUTE, "executado")
        self.assertEqual(len(trace.steps_by_stage(ReasoningStage.PLAN)), 1)
        self.assertEqual(len(trace.steps_by_stage(ReasoningStage.EXECUTE)), 1)


class SelfCorrectionTests(unittest.TestCase):
    def test_succeeds_on_first_attempt(self):
        trace = ReasoningTrace.start(agent="ATLAS", objective="x")
        loop = SelfCorrectionLoop(max_attempts=3)
        outcome = loop.run(trace, [lambda: "ok"])
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.attempts, 1)

    def test_succeeds_on_alternative_strategy(self):
        trace = ReasoningTrace.start(agent="ATLAS", objective="x")
        loop = SelfCorrectionLoop(max_attempts=3)

        def fails():
            raise RuntimeError("falhou tecnicamente")

        outcome = loop.run(trace, [fails, lambda: "recuperou"])
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.attempts, 2)
        self.assertGreater(trace.correction_attempts, 0)

    def test_escalates_after_limit_never_loops_forever(self):
        trace = ReasoningTrace.start(agent="ATLAS", objective="x")
        loop = SelfCorrectionLoop(max_attempts=2)

        def always_fails():
            raise RuntimeError("falha persistente")

        outcome = loop.run(trace, [always_fails, always_fails, always_fails, always_fails])
        self.assertFalse(outcome.succeeded)
        self.assertTrue(outcome.escalate)
        self.assertEqual(outcome.attempts, 2)  # nunca ultrapassa max_attempts

    def test_permission_error_escalates_immediately_without_retry(self):
        trace = ReasoningTrace.start(agent="ATLAS", objective="x")
        loop = SelfCorrectionLoop(max_attempts=5)

        def denied():
            raise AccessDenied("sem permissao")

        outcome = loop.run(trace, [denied, lambda: "nunca chega aqui"])
        self.assertTrue(outcome.escalate)
        self.assertEqual(outcome.attempts, 1)  # nao gasta as tentativas restantes
        self.assertEqual(outcome.error_class, ErrorClass.PERMISSION)

    def test_classify_error_permission(self):
        self.assertEqual(classify_error(AccessDenied("x")), ErrorClass.PERMISSION)

    def test_classify_error_technical(self):
        self.assertEqual(classify_error(RuntimeError("erro tecnico")), ErrorClass.TECHNICAL)

    def test_classify_error_data(self):
        self.assertEqual(classify_error(KeyError("x")), ErrorClass.DATA)

    def test_at_least_one_strategy_required(self):
        trace = ReasoningTrace.start(agent="ATLAS", objective="x")
        loop = SelfCorrectionLoop()
        with self.assertRaises(ValueError):
            loop.run(trace, [])


if __name__ == "__main__":
    unittest.main()
