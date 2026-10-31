import unittest

from atlas.director import AtlasDirector
from commercial.multigerenciador import MultiGerenciadorComercial
from core.capabilities.registry import Capability
from core.coordination.mission import MissionStatus
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.security.audit import AuditTrail
from core.security.rbac import AccessDenied, Principal, Role
from navigator.models import NavigatorAction
from radar_web.engine import MockStrategySource, RadarWebComercial
from radar_web.evolution import AnnualReviewEngine, StrategyHistoryStore
from radar_web.models import FunnelMaturity, FunnelMechanism

"""
Testes VISAO EVOLUTIVA (item 33) + AGENTIC ATLAS (item 30).
"""

ATLAS_PRINCIPAL = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})
OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


def make_atlas():
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    return AtlasDirector(facade=facade, principal=ATLAS_PRINCIPAL), audit


class _ScriptedSource(MockStrategySource):
    def __init__(self):
        super().__init__(mechanism=FunnelMechanism.VSL, source_name="scripted")
        self.call_count = 0
        self.script = {
            1: [("strategy_A", FunnelMaturity.HIGH_SCALE)],
            2: [("strategy_A", FunnelMaturity.DECLINING), ("strategy_B", FunnelMaturity.GROWING)],
            3: [("strategy_B", FunnelMaturity.MATURE), ("strategy_C", FunnelMaturity.EMERGING)],
        }

    def discover(self, *, niche=None):
        from core.evidence.claims import ClaimType
        from radar_web.models import FunnelStrategy
        import time

        self.call_count += 1
        entries = self.script[self.call_count]
        return [
            FunnelStrategy(
                strategy_id=sid, name=sid, niche=niche or "geral", offer="curso", channel="youtube",
                market="BR", funnel_stage="topo", mechanism=FunnelMechanism.VSL, maturity=maturity,
                claim_type=ClaimType.FACT, confidence=0.9, source="scripted", observed_at=time.time(),
            )
            for sid, maturity in entries
        ]


class ThreeYearEvolutionScenarioTests(unittest.TestCase):
    def setUp(self):
        self.source = _ScriptedSource()
        self.radar = RadarWebComercial(sources=[self.source])
        self.history = StrategyHistoryStore()
        self.engine = AnnualReviewEngine(radar=self.radar, history=self.history)

    def test_year_1_strategy_a_high_scale(self):
        self.engine.run_annual_review(year=2026)
        entry_a = self.history.latest_version("strategy_A")
        self.assertEqual(entry_a.status, FunnelMaturity.HIGH_SCALE)

    def test_year_2_strategy_a_declines_b_grows(self):
        self.engine.run_annual_review(year=2026)
        self.engine.run_annual_review(year=2027)
        self.assertEqual(self.history.latest_version("strategy_A").status, FunnelMaturity.DECLINING)
        self.assertEqual(self.history.latest_version("strategy_B").status, FunnelMaturity.GROWING)

    def test_year_3_strategy_b_matures_c_emerges(self):
        self.engine.run_annual_review(year=2026)
        self.engine.run_annual_review(year=2027)
        self.engine.run_annual_review(year=2028)
        self.assertEqual(self.history.latest_version("strategy_B").status, FunnelMaturity.MATURE)
        self.assertEqual(self.history.latest_version("strategy_C").status, FunnelMaturity.EMERGING)

    def test_history_preserved_never_erased(self):
        self.engine.run_annual_review(year=2026)
        self.engine.run_annual_review(year=2027)
        self.engine.run_annual_review(year=2028)
        full_history_a = self.history.history_for("strategy_A")
        self.assertEqual(len(full_history_a), 2)
        self.assertEqual(full_history_a[0].year, 2026)
        self.assertEqual(full_history_a[1].year, 2027)

    def test_ranking_updates_recommendation_changes(self):
        self.engine.run_annual_review(year=2026)
        r1 = self.history.latest_version("strategy_A").status
        self.engine.run_annual_review(year=2027)
        r2 = self.history.latest_version("strategy_A").status
        self.assertNotEqual(r1, r2)

    def test_temporal_validity_marked_via_year(self):
        self.engine.run_annual_review(year=2026)
        entry = self.history.latest_version("strategy_A")
        self.assertEqual(entry.year, 2026)

    def test_change_explained_with_evidence_in_changes_field(self):
        self.engine.run_annual_review(year=2026)
        self.engine.run_annual_review(year=2027)
        entry = self.history.latest_version("strategy_A")
        self.assertIn("HIGH_SCALE", entry.changes)
        self.assertIn("DECLINING", entry.changes)

    def test_longitudinal_comparison_across_all_three_years(self):
        self.engine.run_annual_review(year=2026)
        self.engine.run_annual_review(year=2027)
        comparison = self.history.compare_years("strategy_A", 2026, 2027)
        self.assertTrue(comparison["changed"])
        self.assertEqual(comparison["status_a"], "HIGH_SCALE")
        self.assertEqual(comparison["status_b"], "DECLINING")


class AtlasAgenticMissionTests(unittest.TestCase):
    def test_full_mission_lifecycle_via_reasoning_engine(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="coletar métricas do canal")
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

    def test_owner_approval_blocks_critical_action_until_granted(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="publicar vídeo")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        atlas.run_mission_with_reasoning(
            mission.mission_id, mission.task_id,
            navigator_tasks=[{"task_id": "t1", "description": "publicar", "platform": "youtube", "action": NavigatorAction.PUBLISH}],
        )
        final = atlas.missions.get(mission.mission_id, mission.task_id)
        self.assertNotEqual(final.status, MissionStatus.VERIFIED)
        self.assertEqual(len(atlas.escalation_desk.pending()), 1)

    def test_blocked_by_lack_of_authority_navigator_cannot_self_approve(self):
        atlas, _ = make_atlas()
        pending = atlas.navigator.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={})
        with self.assertRaises(AccessDenied):
            atlas.escalation_desk.approve(pending.approval_used, decision="auto-aprovado", resolved_by=atlas.navigator_principal)

    def test_specialist_agent_forge_diagnoses_independently(self):
        atlas, _ = make_atlas()
        diagnostic = atlas.forge.detect(issue_summary="latência alta detectada", evidence={"p99_ms": 900})
        self.assertIsNotNone(diagnostic.diagnostic_id)

    def test_all_rounder_collaboration_between_agents(self):
        atlas, _ = make_atlas()
        granted = atlas.request_all_rounder_assistance(
            from_agent="FORGE", to_agent=atlas.navigator_principal.subject, reason="apoio em pico de demanda",
        )
        self.assertTrue(granted)

    def test_conflict_of_responsibility_never_grants_unregistered_capability(self):
        """CORREÇÃO (item 5): alias/agente inexistente agora levanta
        `UnknownAgentAlias` explicitamente — nunca falha
        silenciosamente devolvendo `False`."""
        from core.agents.base import UnknownAgentAlias

        atlas, _ = make_atlas()
        with self.assertRaises(UnknownAgentAlias):
            atlas.request_all_rounder_assistance(
                from_agent="NAVIGATOR", to_agent="agente_fantasma", reason="teste",
            )

    def test_capability_unavailable_reported_not_faked(self):
        atlas, _ = make_atlas()
        self.assertFalse(atlas.capability_registry.is_available(agent=atlas.principal.subject, capability=Capability.MULTIMODAL_VIDEO))


if __name__ == "__main__":
    unittest.main()
