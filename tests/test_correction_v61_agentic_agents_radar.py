import os
import tempfile
import time
import unittest

from atlas.director import AtlasDirector
from commercial.multigerenciador import MultiGerenciadorComercial
from core.agents.base import AgentNotImplemented, ImplementationStatus
from core.agents.default_registry import build_default_agent_registry
from core.coordination.delegation import DelegationRegistry, InvalidExecutorError, resolve_authorized_executor
from core.coordination.mission import MissionStatus
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role
from navigator.models import NavigatorAction
from radar_web.engine import RadarWebComercial
from radar_web.scheduler import RadarWebScheduler
from radar_web.worker import RadarWebWorker

"""
Testes CORRECAO V6.1 - itens 1 (agentic-first executor), 2 (25
agentes), 3 (RADAR WEB COMERCIAL execucao continua).
"""

ATLAS_PRINCIPAL = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})


def make_atlas():
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    return AtlasDirector(facade=facade, principal=ATLAS_PRINCIPAL), audit


class DesignatedAgentEnforcementTests(unittest.TestCase):
    def test_forge_mission_via_navigator_without_delegation_blocked(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="FORGE", objective="corrigir bug")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        with self.assertRaises(InvalidExecutorError):
            atlas.run_mission_with_reasoning(
                mission.mission_id, mission.task_id,
                navigator_tasks=[{"task_id": "t1", "description": "x", "platform": "youtube", "action": NavigatorAction.COLLECT_METRICS}],
            )
        final = atlas.missions.get(mission.mission_id, mission.task_id)
        self.assertEqual(final.status, MissionStatus.BLOCKED)
        self.assertNotEqual(final.status, MissionStatus.VERIFIED)

    def test_valid_delegation_allows_execution(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="FORGE", objective="corrigir bug via navigator")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        atlas.delegate_task(
            mission_id=mission.mission_id, task_id=mission.task_id, delegated_agent="NAVIGATOR",
            reason="FORGE precisa de dado operacional externo",
        )
        atlas.run_mission_with_reasoning(
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

    def test_direct_execution_by_designated_agent_never_needs_delegation(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="coletar metricas")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        atlas.run_mission_with_reasoning(
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

    def test_delegation_requires_explicit_reason(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="FORGE", objective="x")
        with self.assertRaises(ValueError):
            atlas.delegate_task(mission_id=mission.mission_id, task_id=mission.task_id, delegated_agent="NAVIGATOR", reason="")

    def test_delegation_audit_trail_complete(self):
        atlas, audit = make_atlas()
        mission = atlas.delegate_mission(agent="FORGE", objective="x")
        atlas.delegate_task(
            mission_id=mission.mission_id, task_id=mission.task_id, delegated_agent="NAVIGATOR",
            reason="teste de auditoria", permissions=["EXECUTE"], capabilities=["AGENTIC_EXECUTION"],
            task_scope="subtarefa delegada",
        )
        events = [e for e in audit.all() if e.action == "atlas_delegate_task"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details["original_agent"], "FORGE")
        self.assertEqual(events[0].details["delegated_agent"], "NAVIGATOR")

    def test_original_agent_remains_mission_owner_after_delegation(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="FORGE", objective="x")
        atlas.delegate_task(
            mission_id=mission.mission_id, task_id=mission.task_id, delegated_agent="NAVIGATOR", reason="apoio",
        )
        current = atlas.missions.get(mission.mission_id, mission.task_id)
        self.assertEqual(current.agent, "FORGE")

    def test_resolve_authorized_executor_direct(self):
        registry = DelegationRegistry()
        result = resolve_authorized_executor(
            designated_agent="NAVIGATOR", executing_agent="NAVIGATOR", mission_id="m1", task_id="t1",
            delegation_registry=registry,
        )
        self.assertEqual(result, "DIRECT")

    def test_resolve_authorized_executor_delegated(self):
        registry = DelegationRegistry()
        registry.delegate(mission_id="m1", task_id="t1", original_agent="FORGE", delegated_agent="NAVIGATOR", reason="x")
        result = resolve_authorized_executor(
            designated_agent="FORGE", executing_agent="NAVIGATOR", mission_id="m1", task_id="t1",
            delegation_registry=registry,
        )
        self.assertEqual(result, "DELEGATED")

    def test_resolve_authorized_executor_invalid(self):
        registry = DelegationRegistry()
        with self.assertRaises(InvalidExecutorError):
            resolve_authorized_executor(
                designated_agent="FORGE", executing_agent="NAVIGATOR", mission_id="m1", task_id="t1",
                delegation_registry=registry,
            )

    def test_evidence_of_real_executor_recorded(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        atlas.run_mission_with_reasoning(
            mission.mission_id, mission.task_id,
            navigator_tasks=[{"task_id": "t1", "description": "x", "platform": "youtube", "action": NavigatorAction.COLLECT_METRICS}],
        )
        log = atlas.navigator.log.by_mission(mission.mission_id)
        self.assertEqual(len(log), 1)
        self.assertTrue(log[0].executor)


class TwentyFiveAgentsTests(unittest.TestCase):
    def test_all_25_agents_registered(self):
        registry = build_default_agent_registry()
        self.assertEqual(len(registry.all()), 25)

    def test_only_three_active(self):
        """MICROCORREÇÃO (eliminar todos os PARTIAL): agora os 25
        agentes são genuinamente `ACTIVE` — 0 SCAFFOLD e 0 PARTIAL.
        SENTINEL/BRIDGE ganharam runtime real (wrappers sobre
        QualitySupervisor/BridgeHub já existentes); os 9 agentes
        dependentes de provider externo (ORION/SAGE/VENTURE/PULSE/
        AURORA/FRAME/VOX/SONORA/PIXEL) tiveram `NullProvider`
        substituído por adapters HTTP reais
        (`HttpSearchProvider`/`HttpGenerationProvider`) — o runtime é
        genuinamente real mesmo quando a chamada de domínio específica
        reporta `UNAVAILABLE` por falta de credencial/rede neste
        ambiente."""
        registry = build_default_agent_registry()
        active = registry.by_implementation_status(ImplementationStatus.ACTIVE)
        self.assertEqual(len(active), 25)
        scaffold = registry.by_implementation_status(ImplementationStatus.SCAFFOLD)
        self.assertEqual(scaffold, [], "nenhum agente deve permanecer em SCAFFOLD")
        partial = registry.by_implementation_status(ImplementationStatus.PARTIAL)
        self.assertEqual(partial, [], "nenhum agente deve permanecer em PARTIAL")

    def test_specialization_never_erased_by_all_rounder(self):
        registry = build_default_agent_registry()
        for agent in registry.all():
            self.assertTrue(agent.specialization)

    def test_capabilities_registered_per_agent(self):
        registry = build_default_agent_registry()
        nexus = registry.get("NEXUS")
        self.assertGreater(len(nexus.capabilities), 0)

    def test_agentic_status_field_present(self):
        registry = build_default_agent_registry()
        for agent in registry.all():
            self.assertIsInstance(agent.agentic_enabled, bool)

    def test_multimodal_status_field_present(self):
        registry = build_default_agent_registry()
        prisma = registry.get("PRISMA")
        self.assertGreater(len(prisma.multimodal_capabilities), 0)

    def test_all_rounder_flag_present(self):
        """CORREÇÃO (BLOCO FINAL 1): NEXUS agora tem runtime real —
        `all_rounder` (all_rounder_runtime_available) é `True`, assim
        como para todo agente que saiu de SCAFFOLD nesta rodada. Um
        agente ainda PARTIAL dependente de provider externo (ex:
        PIXEL) continua com `all_rounder_runtime_available=True`
        também (o runtime em si é real — o que falta é o provider)."""
        registry = build_default_agent_registry()
        atlas_reg = registry.get("ATLAS")
        self.assertTrue(atlas_reg.all_rounder)
        nexus = registry.get("NEXUS")
        self.assertTrue(nexus.all_rounder)

    def test_implementation_status_field_present_for_all(self):
        registry = build_default_agent_registry()
        for agent in registry.all():
            self.assertIsInstance(agent.implementation_status, ImplementationStatus)

    def test_scaffold_agent_never_fakes_execution(self):
        registry = build_default_agent_registry()
        for agent in registry.by_implementation_status(ImplementationStatus.SCAFFOLD):
            self.assertFalse(agent.contract.can_handle(task_type="qualquer_tarefa"))
            with self.assertRaises(AgentNotImplemented):
                agent.contract.execute()

    def test_active_requires_real_contract(self):
        from core.agents.base import AgentRegistration, AgentRegistry
        from core.capabilities.registry import CompetencyLevel

        registry = AgentRegistry()
        with self.assertRaises(ValueError):
            registry.register(AgentRegistration(
                agent_id="FAKE", display_name="Fake", role="FAKE", specialization="x", level=CompetencyLevel.MASTER,
                implementation_status=ImplementationStatus.ACTIVE,
            ))


def make_radar_web(*, db_path=None, poll_interval=0.05):
    radar = RadarWebComercial()
    scheduler = RadarWebScheduler(job_queue=JobQueue(db_path=db_path), radar=radar, db_path=db_path)
    worker = RadarWebWorker(scheduler=scheduler, poll_interval_seconds=poll_interval)
    return scheduler, worker


class RadarWebContinuousExecutionTests(unittest.TestCase):
    """CORREÇÃO (item 14 — isolamento entre testes): TODO worker criado
    neste teste é rastreado e parado em `tearDown`, mesmo quando o
    teste falha no meio (assertion error) — nunca deixa thread órfã
    entre testes, mesmo na suíte monolítica completa."""

    def setUp(self):
        self._workers: list = []

    def tearDown(self):
        for worker in self._workers:
            worker.stop(timeout=5.0)

    def _make_radar_web(self, *, db_path=None, poll_interval=0.05):
        scheduler, worker = make_radar_web(db_path=db_path, poll_interval=poll_interval)
        self._workers.append(worker)
        return scheduler, worker

    def test_automatic_execution_without_manual_call(self):
        scheduler, worker = self._make_radar_web()
        scheduler.enable(interval_seconds=0.2)
        self.assertEqual(scheduler.status().last_run_status, "NEVER_RUN")
        worker.start()
        try:
            deadline = time.time() + 3.0
            while scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
                time.sleep(0.05)
            status = scheduler.status()
        finally:
            worker.stop()
        self.assertEqual(status.last_run_status, "SUCCESS")
        self.assertGreater(status.last_run_discoveries, 0)
        self.assertIsNotNone(status.last_run_duration)

    def test_two_cycles(self):
        scheduler, worker = self._make_radar_web()
        scheduler.enable(interval_seconds=0.15)
        worker.start()
        try:
            seen = set()
            deadline = time.time() + 3.0
            while len(seen) < 2 and time.time() < deadline:
                status = scheduler.status()
                if status.last_run_at is not None:
                    seen.add(status.last_run_at)
                time.sleep(0.05)
        finally:
            worker.stop()
        self.assertGreaterEqual(len(seen), 2)

    def test_disable_stops_execution(self):
        scheduler, worker = self._make_radar_web()
        scheduler.enable(interval_seconds=0.15)
        worker.start()
        deadline = time.time() + 3.0
        while scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
            time.sleep(0.05)
        scheduler.disable()
        last_run = scheduler.status().last_run_at
        time.sleep(0.4)
        worker.stop()
        self.assertEqual(scheduler.status().last_run_at, last_run)

    def test_re_enable_resumes(self):
        scheduler, worker = self._make_radar_web()
        scheduler.enable(interval_seconds=0.15)
        worker.start()
        deadline = time.time() + 3.0
        while scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
            time.sleep(0.05)
        scheduler.disable()
        prior = scheduler.status().last_run_at
        time.sleep(0.2)
        scheduler.enable(interval_seconds=0.15)
        deadline = time.time() + 3.0
        while scheduler.status().last_run_at == prior and time.time() < deadline:
            time.sleep(0.05)
        worker.stop()
        self.assertNotEqual(scheduler.status().last_run_at, prior)

    def test_restart_preserves_schedule(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            scheduler1, worker1 = self._make_radar_web(db_path=db_path)
            scheduler1.enable(interval_seconds=3600)
            worker1.start()
            deadline = time.time() + 3.0
            while scheduler1.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
                time.sleep(0.05)
            worker1.stop()

            scheduler2, worker2 = self._make_radar_web(db_path=db_path)
            status2 = scheduler2.status()
            self.assertTrue(status2.enabled)
            self.assertEqual(status2.last_run_status, "SUCCESS")
        finally:
            if os.path.exists(db_path):
                db_module.remove_db_file(db_path)

    def test_concurrency_multiple_workers_no_duplicate(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            workers = []
            for _ in range(3):
                s, w = self._make_radar_web(db_path=db_path, poll_interval=0.03)
                workers.append((s, w))
            workers[0][0].enable(interval_seconds=0.3)
            for _, w in workers:
                w.start()
            time.sleep(1.0)
            for _, w in workers:
                w.stop()
            with db_module.connect(db_path) as conn:
                rows = conn.execute("SELECT attempts FROM jobs WHERE job_type='radar_web_scan'").fetchall()
            self.assertGreater(len(rows), 0)
            for row in rows:
                self.assertEqual(row["attempts"], 1)
        finally:
            if os.path.exists(db_path):
                db_module.remove_db_file(db_path)

    def test_temporary_failure_does_not_kill_worker(self):
        class FailingRadar(RadarWebComercial):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def discover(self, **kwargs):
                self.calls += 1
                if self.calls <= 2:
                    raise RuntimeError("falha simulada")
                return super().discover(**kwargs)

        radar = FailingRadar()
        scheduler = RadarWebScheduler(job_queue=JobQueue(), radar=radar)
        worker = RadarWebWorker(scheduler=scheduler, poll_interval_seconds=0.05, max_backoff_seconds=1.0)
        self._workers.append(worker)
        scheduler.enable(interval_seconds=0.05)
        worker.start()
        deadline = time.time() + 3.0
        while radar.calls < 3 and time.time() < deadline:
            time.sleep(0.05)
        still_alive = worker.is_running()
        while scheduler.status().last_run_status != "SUCCESS" and time.time() < deadline:
            time.sleep(0.05)
        worker.stop()
        self.assertTrue(still_alive)
        self.assertEqual(scheduler.status().last_run_status, "SUCCESS")

    def test_recovery_after_failure_recorded(self):
        class OnceFailingRadar(RadarWebComercial):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def discover(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("falha unica")
                return super().discover(**kwargs)

        radar = OnceFailingRadar()
        scheduler = RadarWebScheduler(job_queue=JobQueue(), radar=radar)
        worker = RadarWebWorker(scheduler=scheduler, poll_interval_seconds=0.05)
        self._workers.append(worker)
        scheduler.enable(interval_seconds=0.05)
        worker.start()
        deadline = time.time() + 3.0
        while scheduler.status().last_run_status != "SUCCESS" and time.time() < deadline:
            time.sleep(0.05)
        worker.stop()
        self.assertEqual(scheduler.status().last_run_status, "SUCCESS")

    def test_shutdown_no_orphan_thread(self):
        import threading

        scheduler, worker = self._make_radar_web()
        scheduler.enable(interval_seconds=1.0)
        worker.start()
        time.sleep(0.1)
        self.assertIn("RadarWebWorker", {t.name for t in threading.enumerate()})
        worker.stop()
        self.assertNotIn("RadarWebWorker", {t.name for t in threading.enumerate()})

    def test_manual_execution_while_automatic_active_never_duplicates(self):
        scheduler, worker = self._make_radar_web()
        scheduler.enable(interval_seconds=3600)
        worker.start()
        deadline = time.time() + 3.0
        while scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
            time.sleep(0.05)
        first_run = scheduler.status().last_run_at
        manual_result = scheduler.run_manual()
        worker.stop()
        self.assertEqual(scheduler.status().last_run_at, first_run)
        self.assertIsNotNone(manual_result)

    def test_separate_from_opportunities_radar_state(self):
        atlas, _ = make_atlas()
        self.assertIsNot(atlas.radar_scheduler, atlas.radar_web_scheduler)


if __name__ == "__main__":
    unittest.main()


class AllRounderConflictResolutionTests(unittest.TestCase):
    """Item 9 (prioridade especialista->secundário->all-rounder->
    escalation) + Item 10 (ATLAS usa agent_registry+capability_registry
    antes de delegar)."""

    def test_priority_1_domain_specialist(self):
        atlas, _ = make_atlas()
        result = atlas.resolve_agent_for_task(task_type="navigator_execution", preferred_agent="NAVIGATOR")
        self.assertEqual(result["selected_agent"], "NAVIGATOR")
        self.assertEqual(result["tier"], "DOMAIN_SPECIALIST")

    def test_priority_2_secondary_specialist(self):
        atlas, _ = make_atlas()
        result = atlas.resolve_agent_for_task(task_type="forge_repair", preferred_agent="NAVIGATOR")
        self.assertEqual(result["selected_agent"], "FORGE")
        self.assertEqual(result["tier"], "SECONDARY_SPECIALIST")

    def test_priority_3_all_rounder_fallback(self):
        """CORREÇÃO (item 13): ALL-ROUNDER só é escolhido quando
        `can_handle(task_type)` é genuinamente `True` — uma tarefa
        registrada no escopo all-rounder de um agente ('generic_coordination')."""
        atlas, _ = make_atlas()
        result = atlas.resolve_agent_for_task(task_type="generic_coordination")
        self.assertEqual(result["tier"], "ALL_ROUNDER")
        self.assertIn(result["selected_agent"], ("ATLAS", "NAVIGATOR", "FORGE"))

    def test_unknown_task_never_falls_automatically_to_all_rounder(self):
        """CORREÇÃO (item 13, cenário exato exigido): tarefa
        genuinamente desconhecida — ninguém tem `can_handle=True`, nem
        como especialista nem como all-rounder — NUNCA cai
        automaticamente em ATLAS (ou qualquer outro) só por
        `all_rounder_runtime_available=True`. Deve escalar."""
        atlas, _ = make_atlas()
        result = atlas.resolve_agent_for_task(task_type="tarefa_totalmente_desconhecida_de_ninguem")
        self.assertIsNone(result["selected_agent"])
        self.assertEqual(result["tier"], "ESCALATION")

    def test_priority_4_escalation_when_nobody_qualifies(self):
        from core.capabilities.registry import Capability

        atlas, _ = make_atlas()
        result = atlas.resolve_agent_for_task(
            task_type="tarefa_generica", required_capability=Capability.MULTIMODAL_VIDEO,
        )
        self.assertIsNone(result["selected_agent"])
        self.assertEqual(result["tier"], "ESCALATION")

    def test_rationale_is_summarized_not_verbose(self):
        atlas, _ = make_atlas()
        result = atlas.resolve_agent_for_task(task_type="navigator_execution", preferred_agent="NAVIGATOR")
        self.assertLess(len(result["rationale"]), 300)

    def test_every_resolution_audited(self):
        atlas, audit = make_atlas()
        atlas.resolve_agent_for_task(task_type="navigator_execution", preferred_agent="NAVIGATOR")
        events = [e for e in audit.all() if e.action == "atlas_resolve_agent_for_task"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details["selected_agent"], "NAVIGATOR")

    def test_atlas_uses_agent_registry_not_name_fallback(self):
        """Item 10: um preferred_agent inexistente no registro nunca
        é selecionado por 'fallback genérico' — cai para especialista
        secundário/all-rounder/escalation normalmente."""
        atlas, _ = make_atlas()
        result = atlas.resolve_agent_for_task(task_type="forge_repair", preferred_agent="AGENTE_INEXISTENTE")
        self.assertNotEqual(result["selected_agent"], "AGENTE_INEXISTENTE")
        self.assertEqual(result["selected_agent"], "FORGE")

    def test_scaffold_agent_never_selected_as_specialist(self):
        atlas, _ = make_atlas()
        # NEXUS é SCAFFOLD -- can_handle sempre False -- nunca selecionado como especialista
        result = atlas.resolve_agent_for_task(task_type="funnel_architecture", preferred_agent="NEXUS")
        self.assertNotEqual(result["selected_agent"], "NEXUS")
