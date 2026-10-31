import os
import tempfile
import unittest

from atlas.director import AtlasDirector
from commercial.multigerenciador import MultiGerenciadorComercial
from core.coordination.mission import (
    DependencyNotSatisfied,
    InvalidMissionTransition,
    MissionAssignment,
    MissionNotFound,
    MissionStatus,
    MissionTracker,
    PermissionMissing,
    RestrictionViolated,
    ToolNotAllowed,
)
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role
from navigator.models import NavigatorAction

"""
Testes MULTIAGENTE (CORRIGIDO) - state machine correta.
"""

ATLAS_PRINCIPAL = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})


def make_atlas():
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    return AtlasDirector(facade=facade, principal=ATLAS_PRINCIPAL), audit


def advance_to_completed(tracker: MissionTracker, mission_id: str, task_id: str) -> None:
    tracker.transition(mission_id, task_id, status=MissionStatus.PLANNED)
    tracker.transition(mission_id, task_id, status=MissionStatus.RUNNING)
    tracker.transition(mission_id, task_id, status=MissionStatus.COMPLETED)


class StateMachineAdversarialTests(unittest.TestCase):
    def test_queued_to_verified_direct_is_impossible(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", mission_id="m1", task_id="t1")
        tracker.create(m)
        with self.assertRaises(InvalidMissionTransition):
            tracker.transition("m1", "t1", status=MissionStatus.VERIFIED)

    def test_queued_to_completed_direct_is_impossible(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", mission_id="m1", task_id="t1")
        tracker.create(m)
        with self.assertRaises(InvalidMissionTransition):
            tracker.transition("m1", "t1", status=MissionStatus.COMPLETED)

    def test_completed_never_blocks_verified(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", mission_id="m1", task_id="t1")
        tracker.create(m)
        advance_to_completed(tracker, "m1", "t1")
        verified = tracker.verify("m1", "t1", evidence={"proof": "ok"})
        self.assertEqual(verified.status, MissionStatus.VERIFIED)

    def test_completed_can_become_failed_on_failed_verification_attempt(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", mission_id="m1", task_id="t1")
        tracker.create(m)
        advance_to_completed(tracker, "m1", "t1")
        failed = tracker.transition("m1", "t1", status=MissionStatus.FAILED, error="verificação reprovada")
        self.assertEqual(failed.status, MissionStatus.FAILED)

    def test_verified_failed_cancelled_are_terminal(self):
        tracker = MissionTracker()
        for status in (MissionStatus.VERIFIED, MissionStatus.FAILED, MissionStatus.CANCELLED):
            m = MissionAssignment.new(agent="NAVIGATOR", objective="x")
            tracker.create(m)
            if status == MissionStatus.VERIFIED:
                advance_to_completed(tracker, m.mission_id, m.task_id)
                tracker.verify(m.mission_id, m.task_id, evidence={"proof": "ok"})
            else:
                tracker.transition(m.mission_id, m.task_id, status=status)
            with self.assertRaises(InvalidMissionTransition):
                tracker.transition(m.mission_id, m.task_id, status=MissionStatus.RUNNING)

    def test_standard_flow_queued_planned_running_completed_verified(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x")
        tracker.create(m)
        self.assertEqual(tracker.get(m.mission_id, m.task_id).status, MissionStatus.QUEUED)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.RUNNING)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.COMPLETED)
        verified = tracker.verify(m.mission_id, m.task_id, evidence={"proof": "ok"})
        self.assertEqual(verified.status, MissionStatus.VERIFIED)

    def test_auxiliary_states_reachable(self):
        tracker = MissionTracker()
        for aux_status in (MissionStatus.WAITING_DEPENDENCY, MissionStatus.WAITING_OWNER, MissionStatus.BLOCKED):
            m = MissionAssignment.new(agent="NAVIGATOR", objective="x")
            tracker.create(m)
            result = tracker.transition(m.mission_id, m.task_id, status=aux_status)
            self.assertEqual(result.status, aux_status)

    def test_retrying_only_reachable_from_running(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x")
        tracker.create(m)
        with self.assertRaises(InvalidMissionTransition):
            tracker.transition(m.mission_id, m.task_id, status=MissionStatus.RETRYING)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.RUNNING)
        retrying = tracker.transition(m.mission_id, m.task_id, status=MissionStatus.RETRYING)
        self.assertEqual(retrying.status, MissionStatus.RETRYING)


class VerificationRequirementsTests(unittest.TestCase):
    def test_verify_requires_completed_state(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x")
        tracker.create(m)
        with self.assertRaises(InvalidMissionTransition):
            tracker.verify(m.mission_id, m.task_id, evidence={"proof": "ok"})

    def test_verify_requires_non_empty_evidence(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x")
        tracker.create(m)
        advance_to_completed(tracker, m.mission_id, m.task_id)
        with self.assertRaises(ValueError):
            tracker.verify(m.mission_id, m.task_id, evidence={})

    def test_verify_requires_success_criteria_confirmation_when_defined(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", success_criteria=["metricas coletadas"])
        tracker.create(m)
        advance_to_completed(tracker, m.mission_id, m.task_id)
        with self.assertRaises(ValueError):
            tracker.verify(m.mission_id, m.task_id, evidence={"some": "data"})
        verified = tracker.verify(m.mission_id, m.task_id, evidence={"some": "data", "success_criteria_met": True})
        self.assertEqual(verified.status, MissionStatus.VERIFIED)

    def test_verify_rejects_when_quality_gate_not_approved(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x")
        tracker.create(m)
        advance_to_completed(tracker, m.mission_id, m.task_id)
        with self.assertRaises(InvalidMissionTransition):
            tracker.verify(m.mission_id, m.task_id, evidence={"proof": "ok"}, quality_gate_verdict="BLOCK")

    def test_verify_accepts_when_quality_gate_approved(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x")
        tracker.create(m)
        advance_to_completed(tracker, m.mission_id, m.task_id)
        verified = tracker.verify(m.mission_id, m.task_id, evidence={"proof": "ok"}, quality_gate_verdict="APPROVED")
        self.assertEqual(verified.status, MissionStatus.VERIFIED)


class EnforcementTests(unittest.TestCase):
    def test_cannot_start_with_unsatisfied_dependency(self):
        tracker = MissionTracker()
        dep = MissionAssignment.new(agent="NAVIGATOR", objective="dependencia", mission_id="dep1")
        tracker.create(dep)
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", dependencies=["dep1"])
        tracker.create(m)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        with self.assertRaises(DependencyNotSatisfied):
            tracker.start_running(m.mission_id, m.task_id)

    def test_can_start_once_dependency_verified(self):
        tracker = MissionTracker()
        dep = MissionAssignment.new(agent="NAVIGATOR", objective="dependencia", mission_id="dep1")
        tracker.create(dep)
        advance_to_completed(tracker, "dep1", dep.task_id)
        tracker.verify("dep1", dep.task_id, evidence={"proof": "ok"})

        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", dependencies=["dep1"])
        tracker.create(m)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        started = tracker.start_running(m.mission_id, m.task_id)
        self.assertEqual(started.status, MissionStatus.RUNNING)

    def test_disallowed_tool_blocks_start(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", tools_allowed=["youtube_api"])
        tracker.create(m)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        with self.assertRaises(ToolNotAllowed):
            tracker.start_running(m.mission_id, m.task_id, requested_tools=["browser_automation"])

    def test_allowed_tool_permits_start(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", tools_allowed=["youtube_api"])
        tracker.create(m)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        started = tracker.start_running(m.mission_id, m.task_id, requested_tools=["youtube_api"])
        self.assertEqual(started.status, MissionStatus.RUNNING)

    def test_missing_permission_blocks_start(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", permissions=["MANAGE_CREDENTIALS"])
        tracker.create(m)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        navigator_principal = Principal(subject="navigator@system", role=Role.NAVIGATOR, scopes={"*"})
        with self.assertRaises(PermissionMissing):
            tracker.start_running(m.mission_id, m.task_id, agent_principal=navigator_principal)

    def test_present_permission_allows_start(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", permissions=["READ_CATALOG"])
        tracker.create(m)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        navigator_principal = Principal(subject="navigator@system", role=Role.NAVIGATOR, scopes={"*"})
        started = tracker.start_running(m.mission_id, m.task_id, agent_principal=navigator_principal)
        self.assertEqual(started.status, MissionStatus.RUNNING)

    def test_restriction_violation_blocks_start(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", restrictions=["no_paid_ads"])
        tracker.create(m)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        with self.assertRaises(RestrictionViolated):
            tracker.start_running(m.mission_id, m.task_id, context_flags={"no_paid_ads"})

    def test_no_restriction_violation_allows_start(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", restrictions=["no_paid_ads"])
        tracker.create(m)
        tracker.transition(m.mission_id, m.task_id, status=MissionStatus.PLANNED)
        started = tracker.start_running(m.mission_id, m.task_id, context_flags={"organic_only"})
        self.assertEqual(started.status, MissionStatus.RUNNING)


class AtlasDelegatingTests(unittest.TestCase):
    def test_atlas_delegates_to_navigator(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="coletar metricas", success_criteria=["dados coletados"])
        self.assertEqual(mission.agent, "NAVIGATOR")
        self.assertEqual(mission.status, MissionStatus.QUEUED)

    def test_mission_tracked_through_full_lifecycle(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.RUNNING)
        completed = atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.COMPLETED)
        self.assertEqual(completed.status, MissionStatus.COMPLETED)
        verified = atlas.missions.verify(mission.mission_id, mission.task_id, evidence={"proof": "ok"})
        self.assertEqual(verified.status, MissionStatus.VERIFIED)


class TwoAgentsCollaboratingTests(unittest.TestCase):
    def test_navigator_execution_and_forge_diagnosis_share_mission_id(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="publicar e monitorar")
        nav_entry = atlas.navigator.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id=mission.mission_id, task_id=mission.task_id,
        )
        diag = atlas.forge.detect(
            issue_summary=f"acompanhando missao {mission.mission_id}",
            evidence={"mission_id": mission.mission_id, "nav_result": nav_entry.result},
        )
        self.assertEqual(nav_entry.mission_id, mission.mission_id)
        self.assertEqual(diag.evidence["mission_id"], mission.mission_id)


class DependencyTests(unittest.TestCase):
    def test_mission_with_dependency_recorded(self):
        atlas, _ = make_atlas()
        first = atlas.delegate_mission(agent="NAVIGATOR", objective="preparar conteudo")
        second = atlas.delegate_mission(agent="NAVIGATOR", objective="publicar conteudo", dependencies=[first.mission_id])
        self.assertEqual(second.dependencies, [first.mission_id])

    def test_waiting_dependency_status(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x", dependencies=["outra_missao"])
        waiting = atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.WAITING_DEPENDENCY)
        self.assertEqual(waiting.status, MissionStatus.WAITING_DEPENDENCY)


class PartialFailureAndRetryTests(unittest.TestCase):
    def test_mission_failure_then_error_recorded(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.RUNNING)
        failed = atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.FAILED, error="timeout na plataforma")
        self.assertEqual(failed.status, MissionStatus.FAILED)
        self.assertEqual(failed.error, "timeout na plataforma")

    def test_retrying_status_reachable_from_running(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.PLANNED)
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.RUNNING)
        retrying = atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.RETRYING)
        self.assertEqual(retrying.status, MissionStatus.RETRYING)

    def test_cannot_transition_terminal_mission_again(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.CANCELLED)
        with self.assertRaises(InvalidMissionTransition):
            atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.RUNNING)


class CancellationTests(unittest.TestCase):
    def test_mission_cancelled(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x")
        cancelled = atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.CANCELLED)
        self.assertEqual(cancelled.status, MissionStatus.CANCELLED)

    def test_cancelled_mission_is_terminal(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x")
        atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.CANCELLED)
        with self.assertRaises(InvalidMissionTransition):
            atlas.update_mission_status(mission.mission_id, mission.task_id, status=MissionStatus.RUNNING)


class TraceabilityTests(unittest.TestCase):
    def test_mission_and_task_id_correlate_navigator_log(self):
        atlas, _ = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x")
        atlas.navigator.execute(
            platform="youtube", action=NavigatorAction.CHECK_STATUS, payload={},
            mission_id=mission.mission_id, task_id=mission.task_id,
        )
        log_entries = atlas.navigator.log.by_mission(mission.mission_id)
        self.assertEqual(len(log_entries), 1)
        self.assertEqual(log_entries[0].task_id, mission.task_id)

    def test_audit_trail_correlates_by_mission_id_in_resource(self):
        atlas, audit = make_atlas()
        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="x")
        events = [e for e in audit.all() if e.action == "atlas_delegate_mission"]
        self.assertEqual(len(events), 1)
        self.assertIn(mission.mission_id, events[0].resource)


class MissionTrackerDirectTests(unittest.TestCase):
    def test_get_nonexistent_mission_raises(self):
        tracker = MissionTracker()
        with self.assertRaises(MissionNotFound):
            tracker.get("nope", "nope")

    def test_idempotent_create_same_mission_task(self):
        tracker = MissionTracker()
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", mission_id="m1", task_id="t1")
        created1 = tracker.create(m)
        created2 = tracker.create(m)
        self.assertEqual(created1.mission_id, created2.mission_id)


class MultiagentRestartTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def test_mission_survives_restart(self):
        tracker1 = MissionTracker(db_path=self.db_path)
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", mission_id="m1", task_id="t1")
        tracker1.create(m)
        tracker1.transition("m1", "t1", status=MissionStatus.PLANNED)
        tracker1.transition("m1", "t1", status=MissionStatus.RUNNING)

        tracker2 = MissionTracker(db_path=self.db_path)
        recovered = tracker2.get("m1", "t1")
        self.assertEqual(recovered.status, MissionStatus.RUNNING)

    def test_verified_state_survives_restart(self):
        tracker1 = MissionTracker(db_path=self.db_path)
        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", mission_id="m1", task_id="t1")
        tracker1.create(m)
        advance_to_completed(tracker1, "m1", "t1")
        tracker1.verify("m1", "t1", evidence={"proof": "ok"})

        tracker2 = MissionTracker(db_path=self.db_path)
        recovered = tracker2.get("m1", "t1")
        self.assertEqual(recovered.status, MissionStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main()
