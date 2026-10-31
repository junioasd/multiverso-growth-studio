import unittest

from atlas.director import AtlasDirector
from atlas.planning import InvalidObjective
from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.security.audit import AuditTrail
from core.security.rbac import AccessDenied, Principal, Role

"""
Testes ATLAS - LEARNING LOOP e PLANEJAMENTO.
"""


def make_atlas(scopes=None):
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes=scopes or {"*"})
    return AtlasDirector(facade=facade, principal=principal), facade, audit


OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


class PlanningTests(unittest.TestCase):
    def test_owner_objective_produces_operational_plan(self):
        atlas, _, _ = make_atlas()
        plan = atlas.receive_objective(OWNER, objective="Crescer vendas de suplementos", channel_id="chan_1")
        self.assertEqual(plan.objective, "Crescer vendas de suplementos")
        self.assertEqual(plan.channel_id, "chan_1")
        self.assertGreater(len(plan.steps), 0)

    def test_empty_objective_rejected(self):
        atlas, _, _ = make_atlas()
        with self.assertRaises(InvalidObjective):
            atlas.receive_objective(OWNER, objective="   ", channel_id="chan_1")

    def test_missing_channel_rejected(self):
        atlas, _, _ = make_atlas()
        with self.assertRaises(InvalidObjective):
            atlas.receive_objective(OWNER, objective="Crescer vendas", channel_id="")

    def test_unknown_channel_rejected(self):
        atlas, _, _ = make_atlas()
        with self.assertRaises(InvalidObjective):
            atlas.receive_objective(OWNER, objective="Crescer vendas", channel_id="UNKNOWN")

    def test_only_owner_can_assign_objective(self):
        atlas, _, _ = make_atlas()
        agent = Principal(subject="agent-1", role=Role.AGENT, scopes={"*"})
        with self.assertRaises(AccessDenied):
            atlas.receive_objective(agent, objective="Crescer vendas", channel_id="chan_1")

    def test_atlas_itself_cannot_self_assign_objective(self):
        atlas, _, _ = make_atlas()
        with self.assertRaises(AccessDenied):
            atlas.receive_objective(atlas.principal, objective="Crescer vendas", channel_id="chan_1")

    def test_plan_never_invents_market_data(self):
        atlas, _, _ = make_atlas()
        plan = atlas.receive_objective(OWNER, objective="Vender mais", channel_id="chan_1")
        for step in plan.steps:
            self.assertNotRegex(step, r"R\$\s*\d")
            self.assertNotRegex(step, r"\d+%")


class LearningLoopIntegrationTests(unittest.TestCase):
    def test_record_worked_observation(self):
        atlas, _, _ = make_atlas()
        record = atlas.record_learning_observation(
            scope_key="chan_1|product_x|short_form", outcome="worked",
            context={"hook": "pergunta_direta"},
        )
        self.assertEqual(record.outcome, "worked")
        self.assertEqual(record.scope_key, "chan_1|product_x|short_form")

    def test_record_failed_observation(self):
        atlas, _, _ = make_atlas()
        record = atlas.record_learning_observation(
            scope_key="chan_1|product_y", outcome="failed", context={}, limitations="amostra pequena"
        )
        self.assertEqual(record.outcome, "failed")

    def test_invalid_outcome_rejected(self):
        atlas, _, _ = make_atlas()
        with self.assertRaises(ValueError):
            atlas.record_learning_observation(scope_key="chan_1|x", outcome="totally_amazing", context={})

    def test_pattern_confidence_never_certain(self):
        atlas, _, _ = make_atlas()
        for _ in range(20):
            atlas.record_learning_observation(scope_key="chan_1|x", outcome="worked", context={})
        pattern = atlas.learning.pattern_for("chan_1|x")
        self.assertIn(pattern.confidence, ("LOW", "MEDIUM"))
        self.assertNotEqual(pattern.confidence, "CERTAIN")

    def test_pattern_for_unknown_scope_has_zero_observations(self):
        atlas, _, _ = make_atlas()
        pattern = atlas.learning.pattern_for("never-seen-before")
        self.assertEqual(pattern.observations, 0)

    def test_learning_never_touches_rbac_or_quality_gates(self):
        atlas, _, _ = make_atlas()
        public_methods = {
            name for name in dir(atlas.learning)
            if not name.startswith("_") and callable(getattr(atlas.learning, name))
        }
        self.assertEqual(public_methods, {"record_observation", "pattern_for"})

    def test_learning_shared_with_multigerenciador_same_underlying_loop(self):
        atlas, facade, _ = make_atlas()
        atlas.record_learning_observation(scope_key="chan_1|shared", outcome="worked", context={})
        direct_pattern = facade.mgc.learning_loop.pattern_for("chan_1|shared")
        self.assertEqual(direct_pattern.observations, 1)


if __name__ == "__main__":
    unittest.main()
