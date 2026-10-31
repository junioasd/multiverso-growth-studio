import unittest

from api.bootstrap import build_app
from atlas.advanced_planning import AdvancedPlanner
from core.security.auth import hash_password

"""
ATLAS EXPANSION 1 — confirma que `GraphExecutor` está corretamente
conectado ao `AtlasDirector`, compartilhando as MESMAS instâncias
reais de `MissionTracker`/`JobQueue`/`EscalationDesk` já existentes —
nunca sistemas paralelos desconectados.
"""


class GraphExecutorAtlasDirectorWiringTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app(
            owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), persistent=False,
        )

    def tearDown(self):
        self.app.shutdown()

    def test_graph_executor_present_and_shares_real_instances(self):
        atlas = self.app.atlas
        self.assertTrue(hasattr(atlas, "graph_executor"))
        self.assertIs(atlas.graph_executor.mission_tracker, atlas.missions)
        self.assertIs(atlas.graph_executor.job_queue, atlas.facade.job_queue)
        self.assertIs(atlas.graph_executor.escalation_desk, atlas.escalation_desk)

    def test_plan_executes_end_to_end_via_atlas_director(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="via AtlasDirector", tasks=[
            {"task_id": "a", "description": "a"}, {"task_id": "b", "description": "b", "depends_on": ["a"]},
        ])
        report = self.app.atlas.graph_executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste",
        )
        self.assertTrue(report.success)
        self.assertEqual(len(report.task_results), 2)

    def test_mission_tracked_by_atlas_missions_after_execution(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1"}])
        self.app.atlas.graph_executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_wiring",
        )
        mission = self.app.atlas.missions.get("m_wiring", "t1")
        from core.coordination.mission import MissionStatus
        self.assertEqual(mission.status, MissionStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main()
