import os
import tempfile
import unittest

from atlas.escalation import EscalationDesk
from bridge.hub import BridgeHub, ConnectorType
from core.adapters.base import AdapterMode, AdapterResult
from core.governance.idempotency import IdempotencyConflict
from core.models import db as db_module
from core.reasoning.adapters import NavigatorResultVerifier, NavigatorTaskExecutor
from core.reasoning.cycle import ReasoningStage
from core.reasoning.engine import FixedTaskPlanner, ReasoningEngine
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role
from forge.engineer import ForgeEngineer
from forge.models import DiagnosticStatus, RiskLevel
from forge.repair import FixPlan, RepairExecutor
from navigator.executor import NavigatorExecutor
from navigator.mechanism_executors import MockFallbackExecutor
from navigator.models import ExecutionResult, NavigatorAction

"""
Testes MICROCORRECAO V4 - os 2 residuos finais.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
ATLAS_PRINCIPAL = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})


def make_navigator(*, db_path=None):
    audit = AuditTrail(db_path=db_path)
    desk = EscalationDesk(audit, db_path=db_path)
    principal = Principal(subject="navigator@system", role=Role.NAVIGATOR, scopes={"*"})
    hub = BridgeHub()
    hub.register_mock_fallback(platform="youtube")
    nav = NavigatorExecutor(principal=principal, escalation_desk=desk, audit=audit, db_path=db_path, bridge=hub)
    return nav, desk, audit


class TwoAttemptExecutor(MockFallbackExecutor):
    def __init__(self):
        super().__init__(provider_name="youtube")
        self.calls = 0

    def execute(self, *, action, payload):
        self.calls += 1
        if self.calls == 1:
            return AdapterResult(mode=AdapterMode.MOCK, success=False, data={}, error="falha na tentativa 1")
        return super().execute(action=action, payload=payload)


class AlwaysFailExecutor(MockFallbackExecutor):
    def execute(self, *, action, payload):
        return AdapterResult(mode=AdapterMode.MOCK, success=False, data={}, error="falha")


class UnstableExecutor(MockFallbackExecutor):
    def execute(self, *, action, payload):
        raise ConnectionError("conexão perdida — resultado desconhecido")


class NavigatorRealRetryTests(unittest.TestCase):
    def test_failed_then_success_executor_called_twice_new_attempt_id(self):
        nav, _, _ = make_navigator()
        executor = TwoAttemptExecutor()
        nav.register_mechanism_executor(platform="youtube", mechanism=ConnectorType.MOCK_FALLBACK, executor=executor)

        e1 = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id="m1", task_id="t1", attempt_id="attempt_1",
        )
        self.assertEqual(e1.result, ExecutionResult.FAILED.value)

        e2 = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id="m1", task_id="t1", attempt_id="attempt_2",
        )
        self.assertEqual(e2.result, ExecutionResult.SUCCESS.value)
        self.assertEqual(executor.calls, 2)
        self.assertNotEqual(e1.attempt_id, e2.attempt_id)

    def test_same_attempt_id_repeated_never_reexecutes(self):
        nav, _, _ = make_navigator()
        executor = TwoAttemptExecutor()
        nav.register_mechanism_executor(platform="youtube", mechanism=ConnectorType.MOCK_FALLBACK, executor=executor)

        e1 = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id="m1", task_id="t1", attempt_id="attempt_1",
        )
        e1b = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id="m1", task_id="t1", attempt_id="attempt_1",
        )
        self.assertEqual(executor.calls, 1)
        self.assertEqual(e1.entry_id, e1b.entry_id)

    def test_critical_action_uncertain_result_needs_verification_never_auto_retried(self):
        nav, desk, _ = make_navigator()
        nav.register_mechanism_executor(
            platform="youtube", mechanism=ConnectorType.MOCK_FALLBACK, executor=UnstableExecutor(provider_name="youtube"),
        )
        pending = nav.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={}, mission_id="m3", task_id="t3")
        desk.approve(pending.approval_used, decision="ok", resolved_by=OWNER)

        consumed = nav.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={}, mission_id="m3", task_id="t3")
        self.assertEqual(consumed.result, ExecutionResult.NEEDS_VERIFICATION.value)

        retry = nav.execute(
            platform="youtube", action=NavigatorAction.PUBLISH, payload={}, mission_id="m3", task_id="t3",
            attempt_id="nova_tentativa",
        )
        self.assertEqual(retry.entry_id, consumed.entry_id)
        self.assertEqual(retry.result, ExecutionResult.NEEDS_VERIFICATION.value)

    def test_restart_between_attempts_preserves_state_and_retry_works(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            nav1, _, _ = make_navigator(db_path=db_path)
            nav1.register_mechanism_executor(
                platform="youtube", mechanism=ConnectorType.MOCK_FALLBACK,
                executor=AlwaysFailExecutor(provider_name="youtube"),
            )
            e1 = nav1.execute(
                platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
                mission_id="m4", task_id="t4", attempt_id="attempt_1",
            )
            self.assertEqual(e1.result, ExecutionResult.FAILED.value)

            nav2, _, _ = make_navigator(db_path=db_path)
            e2 = nav2.execute(
                platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
                mission_id="m4", task_id="t4", attempt_id="attempt_2",
            )
            self.assertEqual(e2.result, ExecutionResult.SUCCESS.value)
        finally:
            if os.path.exists(db_path):
                db_module.remove_db_file(db_path)

    def test_non_retry_safe_action_blocks_new_attempt_without_override(self):
        nav, _, _ = make_navigator()
        nav.register_mechanism_executor(
            platform="youtube", mechanism=ConnectorType.MOCK_FALLBACK, executor=AlwaysFailExecutor(provider_name="youtube"),
        )
        nav.execute(platform="youtube", action=NavigatorAction.FILL_FORM, payload={}, mission_id="m5", task_id="t5", attempt_id="a1")
        e2 = nav.execute(platform="youtube", action=NavigatorAction.FILL_FORM, payload={}, mission_id="m5", task_id="t5", attempt_id="a2")
        self.assertEqual(e2.result, ExecutionResult.BLOCKED.value)

    def test_non_retry_safe_action_allowed_with_explicit_override(self):
        nav, _, _ = make_navigator()
        nav.register_mechanism_executor(
            platform="youtube", mechanism=ConnectorType.MOCK_FALLBACK, executor=AlwaysFailExecutor(provider_name="youtube"),
        )
        nav.execute(platform="youtube", action=NavigatorAction.FILL_FORM, payload={}, mission_id="m6", task_id="t6", attempt_id="a1")
        e2 = nav.execute(
            platform="youtube", action=NavigatorAction.FILL_FORM, payload={}, mission_id="m6", task_id="t6",
            attempt_id="a2", retry_safe_override=True,
        )
        self.assertNotEqual(e2.result, ExecutionResult.BLOCKED.value)

    def test_critical_action_never_duplicated_regardless_of_attempt_id(self):
        nav, desk, _ = make_navigator()
        e1 = nav.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={}, mission_id="m7", task_id="t7", attempt_id="a1")
        e2 = nav.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={}, mission_id="m7", task_id="t7", attempt_id="a2")
        self.assertEqual(e1.approval_used, e2.approval_used)
        self.assertEqual(len(desk.pending()), 1)

    def test_attempt_id_conflict_with_different_payload_raises(self):
        nav, _, _ = make_navigator()
        nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={"a": 1},
            mission_id="m8", task_id="t8", idempotency_key="fixed-key",
        )
        with self.assertRaises(IdempotencyConflict):
            nav.execute(
                platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={"a": 2},
                mission_id="m8", task_id="t8", idempotency_key="fixed-key",
            )


class ReasoningEngineNavigatorIntegrationTests(unittest.TestCase):
    def test_full_chain_replan_navigator_idempotency_executor_real(self):
        nav, _, _ = make_navigator()
        executor = TwoAttemptExecutor()
        nav.register_mechanism_executor(platform="youtube", mechanism=ConnectorType.MOCK_FALLBACK, executor=executor)

        adapter = NavigatorTaskExecutor(nav, mission_id="m1", task_id="t1")
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t1", "description": "x", "platform": "youtube", "action": NavigatorAction.COLLECT_METRICS, "payload": {}}]),
            executor=adapter, verifier=NavigatorResultVerifier(), max_replan_attempts=3,
        )
        trace, results = engine.run(agent="ATLAS", objective="x")

        self.assertEqual(executor.calls, 2)
        self.assertGreater(len(trace.steps_by_stage(ReasoningStage.REPLAN)), 0)
        # CORREÇÃO PÓS-AUDITORIA (bloqueador A): TwoAttemptExecutor É um
        # MockFallbackExecutor (mode=MOCK). O replanejamento após a falha
        # continua provado acima; o sucesso SIMULADO da 2ª tentativa nunca é
        # verificado e não é re-executado (exatamente 2 chamadas).
        self.assertEqual(results[0]["result"]["evidence"]["mode"], "MOCK")
        self.assertFalse(results[0]["verified"])
        self.assertFalse(trace.verified)

    def test_idempotency_preserved_for_critical_action_across_replan(self):
        nav, desk, _ = make_navigator()
        adapter = NavigatorTaskExecutor(nav, mission_id="m2", task_id="t2")
        engine = ReasoningEngine(
            planner=FixedTaskPlanner([{"task_id": "t2", "description": "publicar", "platform": "youtube", "action": NavigatorAction.PUBLISH, "payload": {}}]),
            executor=adapter, verifier=NavigatorResultVerifier(), max_replan_attempts=3,
        )
        engine.run(agent="ATLAS", objective="x")
        self.assertEqual(len(desk.pending()), 1)


def make_forge(*, db_path=None):
    audit = AuditTrail(db_path=db_path)
    principal = Principal(subject="forge@system", role=Role.FORGE, scopes={"*"})
    return ForgeEngineer(principal=principal, audit=audit, db_path=db_path)


def apply_fix(forge, *, store, changes):
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
    return d.diagnostic_id


class RollbackMustActuallyRestoreTests(unittest.TestCase):
    def test_happy_path_rollback_restores_and_marks_rolled_back(self):
        forge = make_forge()
        store = {"value": "A"}
        diagnostic_id = apply_fix(forge, store=store, changes={"value": "B"})
        executor = RepairExecutor(resource_store=store, allowed_resources={"value"})
        result = forge.rollback(diagnostic_id, reason="regressão", executor=executor)
        self.assertEqual(store["value"], "A")
        self.assertEqual(result.status, DiagnosticStatus.ROLLED_BACK)

    def test_restart_without_restorer_never_rolled_back(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            forge1 = make_forge(db_path=db_path)
            store = {"value": "A"}
            diagnostic_id = apply_fix(forge1, store=store, changes={"value": "B"})

            forge2 = make_forge(db_path=db_path)
            result = forge2.rollback(diagnostic_id, reason="sem restorer")
            self.assertEqual(result.status, DiagnosticStatus.ROLLBACK_FAILED)
            self.assertNotEqual(result.status, DiagnosticStatus.ROLLED_BACK)
            self.assertFalse(result.evidence.get("rollback_restored"))
            self.assertEqual(store["value"], "B")

            forge3 = make_forge(db_path=db_path)
            executor3 = RepairExecutor(resource_store=store, allowed_resources={"value"})
            result2 = forge3.rollback(diagnostic_id, reason="com restorer", executor=executor3)
            self.assertEqual(result2.status, DiagnosticStatus.ROLLED_BACK)
            self.assertTrue(result2.evidence.get("rollback_restored"))
            self.assertEqual(store["value"], "A")
        finally:
            if os.path.exists(db_path):
                db_module.remove_db_file(db_path)

    def test_rollback_failed_operation_stays_blocked(self):
        forge = make_forge()
        store = {"value": "A"}
        diagnostic_id = apply_fix(forge, store=store, changes={"value": "B"})
        d = forge.store.get(diagnostic_id)
        d.blocked_operation = "op_test"
        forge.store.update(d)
        forge.rollback(diagnostic_id, reason="sem restorer")
        self.assertTrue(forge.is_operation_blocked("op_test"))

    def test_nonexistent_checkpoint_now_returns_no_rollback_needed(self):
        """CORREÇÃO (microcorreção finalíssima): comportamento
        atualizado — nunca mais ROLLED_BACK sem fix aplicado."""
        forge = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        result = forge.rollback(d.diagnostic_id, reason="sem fix aplicado")
        self.assertEqual(result.status, DiagnosticStatus.NO_ROLLBACK_NEEDED)
        self.assertFalse(result.evidence.get("rollback_restored"))

    def test_corrupted_checkpoint_never_rolled_back(self):
        forge = make_forge()
        store = {"value": "A"}
        diagnostic_id = apply_fix(forge, store=store, changes={"value": "B"})
        d = forge.store.get(diagnostic_id)
        d.evidence["repair_backup"] = {"value": "CORROMPIDO"}
        forge.store.update(d)
        executor = RepairExecutor(resource_store=store, allowed_resources={"value"})
        result = forge.rollback(diagnostic_id, reason="teste", executor=executor)
        self.assertEqual(result.status, DiagnosticStatus.ROLLBACK_FAILED)
        self.assertEqual(store["value"], "B")

    def test_hash_divergent_detected_before_restoration_attempt(self):
        forge = make_forge()
        store = {"value": "A"}
        diagnostic_id = apply_fix(forge, store=store, changes={"value": "B"})
        d = forge.store.get(diagnostic_id)
        d.evidence["repair_backup_hash"] = "hash-invalido-de-proposito"
        forge.store.update(d)
        executor = RepairExecutor(resource_store=store, allowed_resources={"value"})
        result = forge.rollback(diagnostic_id, reason="teste", executor=executor)
        self.assertEqual(result.status, DiagnosticStatus.ROLLBACK_FAILED)
        self.assertIn("checkpoint corrompido", result.evidence.get("rollback_error", ""))

    def test_partial_restoration_capability_fails_cleanly(self):
        forge = make_forge()
        store = {"a": 1, "b": 2}
        diagnostic_id = apply_fix(forge, store=store, changes={"a": 10, "b": 20})
        incomplete_executor = RepairExecutor(resource_store=store, allowed_resources={"a"})
        result = forge.rollback(diagnostic_id, reason="teste", executor=incomplete_executor)
        self.assertEqual(result.status, DiagnosticStatus.ROLLBACK_FAILED)

    def test_multiple_versions_rollback_after_more_than_one_fix(self):
        forge = make_forge()
        store = {"value": "A"}
        diagnostic_id = apply_fix(forge, store=store, changes={"value": "B"})
        forge.start_testing(diagnostic_id)
        forge.record_test_result(diagnostic_id, passed=False, evidence={"reason": "regrediu"})
        forge.resume_from_block(diagnostic_id, target_status=DiagnosticStatus.FIX_APPLYING, authorized_by=ATLAS_PRINCIPAL)
        executor2 = RepairExecutor(resource_store=store, allowed_resources={"value"})
        plan2 = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="forge@system", changes={"value": "C"})
        forge.execute_repair(diagnostic_id, plan=plan2, executor=executor2)
        self.assertEqual(store["value"], "C")

        result = forge.rollback(diagnostic_id, reason="reverter", executor=executor2)
        self.assertEqual(result.status, DiagnosticStatus.ROLLED_BACK)
        self.assertEqual(store["value"], "B")

    def test_rollback_after_restart_with_valid_restorer_succeeds(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            forge1 = make_forge(db_path=db_path)
            store = {"value": "X"}
            diagnostic_id = apply_fix(forge1, store=store, changes={"value": "Y"})

            forge2 = make_forge(db_path=db_path)
            executor2 = RepairExecutor(resource_store=store, allowed_resources={"value"})
            result = forge2.rollback(diagnostic_id, reason="após restart", executor=executor2)
            self.assertEqual(result.status, DiagnosticStatus.ROLLED_BACK)
            self.assertEqual(store["value"], "X")
        finally:
            if os.path.exists(db_path):
                db_module.remove_db_file(db_path)


if __name__ == "__main__":
    unittest.main()


class NoRollbackNeededSemanticsTests(unittest.TestCase):
    """MICROCORREÇÃO FINALÍSSIMA — rollback sem checkpoint distingue
    'nada havia para desfazer' de 'rollback necessário mas
    impossível' de 'rollback realmente concluído'."""

    def test_rollback_before_any_fix_applied_is_no_rollback_needed(self):
        forge = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        result = forge.rollback(d.diagnostic_id, reason="teste")
        self.assertEqual(result.status, DiagnosticStatus.NO_ROLLBACK_NEEDED)
        self.assertNotEqual(result.status, DiagnosticStatus.ROLLED_BACK)
        self.assertFalse(result.evidence.get("rollback_restored"))

    def test_no_rollback_needed_records_evidence_and_reason(self):
        forge = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        result = forge.rollback(d.diagnostic_id, reason="motivo específico do teste")
        self.assertEqual(result.evidence.get("rollback_reason"), "motivo específico do teste")
        self.assertIn("rollback_note", result.evidence)

    def test_checkpoint_expected_but_missing_after_real_fix_is_rollback_failed(self):
        """Distinção crítica: quando uma correção REAL foi aplicada
        (applied_at presente) mas o checkpoint sumiu, NUNCA é
        confundido com 'nada a fazer' — é uma anomalia."""
        forge = make_forge()
        diagnostic_id = apply_fix(forge, store={"value": "A"}, changes={"value": "B"})
        d = forge.store.get(diagnostic_id)
        del d.evidence["repair_backup"]
        if "repair_backup_hash" in d.evidence:
            del d.evidence["repair_backup_hash"]
        forge.store.update(d)

        executor = RepairExecutor(resource_store={"value": "B"}, allowed_resources={"value"})
        result = forge.rollback(diagnostic_id, reason="teste", executor=executor)
        self.assertEqual(result.status, DiagnosticStatus.ROLLBACK_FAILED)
        self.assertNotEqual(result.status, DiagnosticStatus.NO_ROLLBACK_NEEDED)

    def test_valid_checkpoint_full_cycle_A_to_B_to_rollback_to_A(self):
        forge = make_forge()
        store = {"value": "A"}
        diagnostic_id = apply_fix(forge, store=store, changes={"value": "B"})
        self.assertEqual(store["value"], "B")
        executor = RepairExecutor(resource_store=store, allowed_resources={"value"})
        result = forge.rollback(diagnostic_id, reason="teste", executor=executor)
        self.assertEqual(store["value"], "A")
        self.assertTrue(result.evidence.get("rollback_restored"))
        self.assertEqual(result.status, DiagnosticStatus.ROLLED_BACK)

    def test_restart_before_rollback_without_fix_preserves_behavior(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            forge1 = make_forge(db_path=db_path)
            d = forge1.detect(issue_summary="x", evidence={})
            forge1.diagnose(d.diagnostic_id, analysis={})

            forge2 = make_forge(db_path=db_path)
            result = forge2.rollback(d.diagnostic_id, reason="após restart, sem fix")
            self.assertEqual(result.status, DiagnosticStatus.NO_ROLLBACK_NEEDED)
        finally:
            if os.path.exists(db_path):
                db_module.remove_db_file(db_path)

    def test_no_rollback_needed_is_terminal(self):
        forge = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        forge.rollback(d.diagnostic_id, reason="teste")
        from forge.engineer import InvalidPipelineTransition
        with self.assertRaises(InvalidPipelineTransition):
            forge.diagnose(d.diagnostic_id, analysis={})

    def test_three_way_distinction_never_confused(self):
        """Garante a distinção clara entre os 3 desfechos possíveis."""
        forge = make_forge()

        # nada havia para desfazer
        d1 = forge.detect(issue_summary="x1", evidence={})
        r1 = forge.rollback(d1.diagnostic_id, reason="x")

        # rollback necessário mas impossível (checkpoint corrompido)
        diagnostic_id2 = apply_fix(forge, store={"value": "A"}, changes={"value": "B"})
        d2 = forge.store.get(diagnostic_id2)
        d2.evidence["repair_backup_hash"] = "hash-invalido"
        forge.store.update(d2)
        executor2 = RepairExecutor(resource_store={"value": "B"}, allowed_resources={"value"})
        r2 = forge.rollback(diagnostic_id2, reason="x", executor=executor2)

        # rollback realmente concluído
        store3 = {"value": "A"}
        diagnostic_id3 = apply_fix(forge, store=store3, changes={"value": "B"})
        executor3 = RepairExecutor(resource_store=store3, allowed_resources={"value"})
        r3 = forge.rollback(diagnostic_id3, reason="x", executor=executor3)

        statuses = {r1.status, r2.status, r3.status}
        self.assertEqual(len(statuses), 3, "os 3 desfechos devem ser estados DISTINTOS")
        self.assertEqual(r1.status, DiagnosticStatus.NO_ROLLBACK_NEEDED)
        self.assertEqual(r2.status, DiagnosticStatus.ROLLBACK_FAILED)
        self.assertEqual(r3.status, DiagnosticStatus.ROLLED_BACK)
