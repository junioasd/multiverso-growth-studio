import os
import tempfile
import unittest

from atlas.director import AtlasDirector
from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role
from forge.engineer import (
    ForgeEngineer,
    ForgeHierarchyViolation,
    FixNotAuthorized,
    InvalidPipelineTransition,
)
from forge.models import DiagnosticStatus, RiskLevel, Severity
from forge.repair import FixPlan, RepairExecutor, RepairNotPermitted

"""
Testes FORGE (CORRIGIDO) - bug simulado, diagnostico, causa raiz,
correcao REAL (RepairExecutor), teste, nao-regressao, rollback,
bloqueio PERSISTIDO (sobrevive a restart, nao e mais estado morto),
liberacao apos validacao.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
ATLAS_PRINCIPAL = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})


def make_forge(*, db_path=None):
    audit = AuditTrail(db_path=db_path)
    principal = Principal(subject="forge@system", role=Role.FORGE, scopes={"*"})
    forge = ForgeEngineer(principal=principal, audit=audit, db_path=db_path)
    return forge, audit


def run_to_risk_assessed(forge, *, risk_level=RiskLevel.LOW):
    d = forge.detect(issue_summary="bug simulado: job travado apos restart", evidence={"symptom": "timeout"})
    forge.diagnose(d.diagnostic_id, analysis={"logs_checked": True})
    forge.identify_root_cause(d.diagnostic_id, root_cause="lock nao liberado apos falha de commit")
    forge.propose_fix(d.diagnostic_id, proposed_fix="ajustar timeout")
    forge.assess_risk(d.diagnostic_id, risk_level=risk_level)
    return d.diagnostic_id


def run_full_pipeline(forge, *, risk_level=RiskLevel.LOW, authorized_by=ATLAS_PRINCIPAL, store=None):
    diagnostic_id = run_to_risk_assessed(forge, risk_level=risk_level)
    forge.approve_fix(diagnostic_id, authorized_by=authorized_by)
    forge.start_applying(diagnostic_id)
    store = store if store is not None else {"timeout_seconds": 30}
    executor = RepairExecutor(resource_store=store, allowed_resources={"timeout_seconds"})
    plan = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="forge@system", changes={"timeout_seconds": 60})
    forge.execute_repair(diagnostic_id, plan=plan, executor=executor)
    forge.start_testing(diagnostic_id)
    forge.record_test_result(diagnostic_id, passed=True, evidence={"tests_run": 5})
    forge.record_non_regression_result(diagnostic_id, passed=True, evidence={"full_suite": "passed"})
    return diagnostic_id


class HierarchyTests(unittest.TestCase):
    def test_requires_forge_role(self):
        audit = AuditTrail()
        for wrong_role in (Role.OWNER, Role.ATLAS, Role.NAVIGATOR, Role.OPERATOR):
            with self.assertRaises(ForgeHierarchyViolation):
                ForgeEngineer(principal=Principal(subject="x", role=wrong_role, scopes={"*"}), audit=audit)


class SimulatedBugPipelineTests(unittest.TestCase):
    def test_full_pipeline_detect_to_release(self):
        forge, _ = make_forge()
        diagnostic_id = run_full_pipeline(forge)
        released = forge.release(diagnostic_id)
        self.assertEqual(released.status, DiagnosticStatus.RELEASED)

    def test_detect_records_issue_summary_evidence_and_severity(self):
        forge, _ = make_forge()
        d = forge.detect(issue_summary="latencia alta", evidence={"p99_ms": 900}, severity=Severity.HIGH)
        self.assertEqual(d.status, DiagnosticStatus.DETECTED)
        self.assertEqual(d.evidence["p99_ms"], 900)
        self.assertEqual(d.severity, Severity.HIGH)

    def test_diagnosing_stage(self):
        forge, _ = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        diagnosing = forge.diagnose(d.diagnostic_id, analysis={"reproducible": True})
        self.assertEqual(diagnosing.status, DiagnosticStatus.DIAGNOSING)
        self.assertTrue(diagnosing.evidence["reproducible"])

    def test_root_cause_found_stage(self):
        forge, _ = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        forge.diagnose(d.diagnostic_id, analysis={})
        with_cause = forge.identify_root_cause(d.diagnostic_id, root_cause="race condition")
        self.assertEqual(with_cause.root_cause, "race condition")
        self.assertEqual(with_cause.status, DiagnosticStatus.ROOT_CAUSE_FOUND)

    def test_cannot_skip_pipeline_stage(self):
        forge, _ = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        with self.assertRaises(InvalidPipelineTransition):
            forge.propose_fix(d.diagnostic_id, proposed_fix="x")

    def test_cannot_approve_before_risk_assessed(self):
        forge, _ = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        forge.diagnose(d.diagnostic_id, analysis={})
        forge.identify_root_cause(d.diagnostic_id, root_cause="x")
        forge.propose_fix(d.diagnostic_id, proposed_fix="x")
        with self.assertRaises(InvalidPipelineTransition):
            forge.approve_fix(d.diagnostic_id, authorized_by=OWNER)

    def test_cannot_execute_repair_before_applying(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        executor = RepairExecutor(resource_store={}, allowed_resources=set())
        plan = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="x", changes={})
        with self.assertRaises(InvalidPipelineTransition):
            forge.execute_repair(diagnostic_id, plan=plan, executor=executor)


class AuthorizationTests(unittest.TestCase):
    def test_approve_fix_requires_owner_or_atlas(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        unauthorized = Principal(subject="op-1", role=Role.OPERATOR, scopes={"*"})
        with self.assertRaises(FixNotAuthorized):
            forge.approve_fix(diagnostic_id, authorized_by=unauthorized)

    def test_high_risk_requires_owner_not_just_atlas(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge, risk_level=RiskLevel.HIGH)
        with self.assertRaises(FixNotAuthorized):
            forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)

    def test_high_risk_owner_authorization_succeeds(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge, risk_level=RiskLevel.HIGH)
        applied = forge.approve_fix(diagnostic_id, authorized_by=OWNER)
        self.assertEqual(applied.status, DiagnosticStatus.FIX_APPROVED)

    def test_low_risk_atlas_authorization_succeeds(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge, risk_level=RiskLevel.LOW)
        applied = forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        self.assertEqual(applied.status, DiagnosticStatus.FIX_APPROVED)


class RealRepairExecutionTests(unittest.TestCase):
    def test_repair_actually_changes_resource(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(diagnostic_id)

        store = {"timeout_seconds": 30}
        executor = RepairExecutor(resource_store=store, allowed_resources={"timeout_seconds"})
        plan = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="forge@system", changes={"timeout_seconds": 60})
        applied = forge.execute_repair(diagnostic_id, plan=plan, executor=executor)

        self.assertEqual(applied.status, DiagnosticStatus.FIX_APPLIED)
        self.assertEqual(store["timeout_seconds"], 60)
        self.assertIn("diff", applied.evidence)

    def test_dry_run_never_changes_resource_or_advances_state(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(diagnostic_id)

        store = {"timeout_seconds": 30}
        executor = RepairExecutor(resource_store=store, allowed_resources={"timeout_seconds"})
        plan = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="forge@system", changes={"timeout_seconds": 60})
        result = forge.execute_repair(diagnostic_id, plan=plan, executor=executor, dry_run=True)

        self.assertEqual(store["timeout_seconds"], 30)
        self.assertEqual(result.status, DiagnosticStatus.FIX_APPLYING)

    def test_repair_blocked_when_resource_not_permitted(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(diagnostic_id)

        store = {"secret_key": "nao_toque"}
        executor = RepairExecutor(resource_store=store, allowed_resources=set())
        plan = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="x", changes={"secret_key": "hack"})
        with self.assertRaises(RepairNotPermitted):
            forge.execute_repair(diagnostic_id, plan=plan, executor=executor)
        self.assertEqual(store["secret_key"], "nao_toque")

    def test_partial_failure_blocks_pipeline_and_reverts(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(diagnostic_id)

        class FailingDict(dict):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._failed_once = False

            def __setitem__(self, key, value):
                if key == "b" and value == 20 and not self._failed_once:
                    self._failed_once = True
                    raise RuntimeError("falha na segunda mudanca")
                super().__setitem__(key, value)

        store = FailingDict({"a": 1, "b": 2})
        executor = RepairExecutor(resource_store=store, allowed_resources={"a", "b"})
        plan = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="x", changes={"a": 10, "b": 20})
        blocked = forge.execute_repair(diagnostic_id, plan=plan, executor=executor)

        self.assertEqual(blocked.status, DiagnosticStatus.BLOCKED)
        self.assertEqual(store["a"], 1, "falha parcial deve reverter TUDO, mesmo o que já tinha aplicado")


class TestingAndNonRegressionTests(unittest.TestCase):
    def test_failed_test_blocks_pipeline(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(diagnostic_id)
        store = {"timeout_seconds": 30}
        executor = RepairExecutor(resource_store=store, allowed_resources={"timeout_seconds"})
        plan = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="x", changes={"timeout_seconds": 60})
        forge.execute_repair(diagnostic_id, plan=plan, executor=executor)
        forge.start_testing(diagnostic_id)
        blocked = forge.record_test_result(diagnostic_id, passed=False, evidence={"failure": "assert error"})
        self.assertEqual(blocked.status, DiagnosticStatus.BLOCKED)

    def test_failed_regression_test_blocks_pipeline(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(diagnostic_id)
        store = {"timeout_seconds": 30}
        executor = RepairExecutor(resource_store=store, allowed_resources={"timeout_seconds"})
        plan = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="x", changes={"timeout_seconds": 60})
        forge.execute_repair(diagnostic_id, plan=plan, executor=executor)
        forge.start_testing(diagnostic_id)
        forge.record_test_result(diagnostic_id, passed=True, evidence={})
        blocked = forge.record_non_regression_result(diagnostic_id, passed=False, evidence={"broke": "test_x"})
        self.assertEqual(blocked.status, DiagnosticStatus.BLOCKED)

    def test_validation_requires_evidence(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        forge.start_applying(diagnostic_id)
        store = {"timeout_seconds": 30}
        executor = RepairExecutor(resource_store=store, allowed_resources={"timeout_seconds"})
        plan = FixPlan.new(diagnostic_id=diagnostic_id, executor_name="x", changes={"timeout_seconds": 60})
        forge.execute_repair(diagnostic_id, plan=plan, executor=executor)
        forge.start_testing(diagnostic_id)
        forge.record_test_result(diagnostic_id, passed=True, evidence={"x": 1})
        with self.assertRaises(ValueError):
            forge.record_non_regression_result(diagnostic_id, passed=True, evidence={})

    def test_release_only_after_validation(self):
        forge, _ = make_forge()
        diagnostic_id = run_full_pipeline(forge)
        forge.release(diagnostic_id)
        with self.assertRaises(InvalidPipelineTransition):
            forge.release(diagnostic_id)


class RollbackTests(unittest.TestCase):
    def test_rollback_without_fix_applied_is_no_rollback_needed(self):
        """CORREÇÃO (microcorreção finalíssima): rollback antes de
        qualquer correção real aplicada NUNCA é ROLLED_BACK — não há
        nada a desfazer."""
        forge, _ = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        forge.diagnose(d.diagnostic_id, analysis={})
        result = forge.rollback(d.diagnostic_id, reason="causa raiz errada")
        self.assertEqual(result.status, DiagnosticStatus.NO_ROLLBACK_NEEDED)
        self.assertEqual(result.evidence["rollback_reason"], "causa raiz errada")
        self.assertFalse(result.evidence["rollback_restored"])

    def test_cannot_transition_after_rollback(self):
        forge, _ = make_forge()
        d = forge.detect(issue_summary="x", evidence={})
        forge.rollback(d.diagnostic_id, reason="abortado")
        with self.assertRaises(InvalidPipelineTransition):
            forge.diagnose(d.diagnostic_id, analysis={})


class BlockedNotDeadEndTests(unittest.TestCase):
    def test_blocked_resumes_with_authorization(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge._block(diagnostic_id, reason="revisão manual necessária")
        resumed = forge.resume_from_block(diagnostic_id, target_status=DiagnosticStatus.RISK_ASSESSED, authorized_by=OWNER)
        self.assertEqual(resumed.status, DiagnosticStatus.RISK_ASSESSED)

    def test_pipeline_continues_normally_after_resume(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge._block(diagnostic_id, reason="revisão manual necessária")
        forge.resume_from_block(diagnostic_id, target_status=DiagnosticStatus.RISK_ASSESSED, authorized_by=OWNER)
        approved = forge.approve_fix(diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        self.assertEqual(approved.status, DiagnosticStatus.FIX_APPROVED)

    def test_resume_without_authorization_rejected(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge)
        forge._block(diagnostic_id, reason="x")
        unauthorized = Principal(subject="op", role=Role.OPERATOR, scopes={"*"})
        with self.assertRaises(FixNotAuthorized):
            forge.resume_from_block(diagnostic_id, target_status=DiagnosticStatus.RISK_ASSESSED, authorized_by=unauthorized)

    def test_resume_high_risk_requires_owner(self):
        forge, _ = make_forge()
        diagnostic_id = run_to_risk_assessed(forge, risk_level=RiskLevel.HIGH)
        forge._block(diagnostic_id, reason="x")
        with self.assertRaises(FixNotAuthorized):
            forge.resume_from_block(diagnostic_id, target_status=DiagnosticStatus.RISK_ASSESSED, authorized_by=ATLAS_PRINCIPAL)
        resumed = forge.resume_from_block(diagnostic_id, target_status=DiagnosticStatus.RISK_ASSESSED, authorized_by=OWNER)
        self.assertEqual(resumed.status, DiagnosticStatus.RISK_ASSESSED)


class PersistedBlockTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def test_block_operation_preventively(self):
        forge, _ = make_forge(db_path=self.db_path)
        d = forge.block_operation(operation_name="deploy_v9", reason="regressão potencial detectada", evidence={"risk": "alto"})
        self.assertEqual(d.status, DiagnosticStatus.BLOCKED)
        self.assertTrue(forge.is_operation_blocked("deploy_v9"))

    def test_block_survives_restart(self):
        forge1, _ = make_forge(db_path=self.db_path)
        forge1.block_operation(operation_name="deploy_v9", reason="risco", evidence={})

        forge2, _ = make_forge(db_path=self.db_path)
        self.assertTrue(forge2.is_operation_blocked("deploy_v9"))

    def test_diagnostic_survives_restart(self):
        forge1, _ = make_forge(db_path=self.db_path)
        d = forge1.detect(issue_summary="x", evidence={"a": 1})
        forge1.diagnose(d.diagnostic_id, analysis={})

        forge2, _ = make_forge(db_path=self.db_path)
        recovered = forge2.store.get(d.diagnostic_id)
        self.assertEqual(recovered.status, DiagnosticStatus.DIAGNOSING)

    def test_release_operation_block_requires_authorization(self):
        from forge.models import BlockClassification

        forge, _ = make_forge(db_path=self.db_path)
        d = forge.block_operation(
            operation_name="deploy_v9", reason="risco", evidence={}, classification=BlockClassification.ADMINISTRATIVE,
        )
        unauthorized = Principal(subject="op", role=Role.OPERATOR, scopes={"*"})
        with self.assertRaises(FixNotAuthorized):
            forge.release_operation_block(d.diagnostic_id, authorized_by=unauthorized, justification="teste")
        forge.release_operation_block(d.diagnostic_id, authorized_by=OWNER, justification="risco reavaliado como baixo")
        self.assertFalse(forge.is_operation_blocked("deploy_v9"))

    def test_unblock_survives_restart(self):
        from forge.models import BlockClassification

        forge1, _ = make_forge(db_path=self.db_path)
        d = forge1.block_operation(
            operation_name="deploy_v9", reason="risco", evidence={}, classification=BlockClassification.ADMINISTRATIVE,
        )
        forge1.release_operation_block(d.diagnostic_id, authorized_by=OWNER, justification="risco reavaliado")

        forge2, _ = make_forge(db_path=self.db_path)
        self.assertFalse(forge2.is_operation_blocked("deploy_v9"))


class AtlasForgeIntegrationTests(unittest.TestCase):
    def test_atlas_forge_subordinate_identity(self):
        mgc = MultiGerenciadorComercial()
        audit = AuditTrail()
        facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
        atlas = AtlasDirector(facade=facade, principal=ATLAS_PRINCIPAL)
        self.assertEqual(atlas.forge.principal.role, Role.FORGE)
        d = atlas.forge.detect(issue_summary="via atlas", evidence={})
        self.assertEqual(d.status, DiagnosticStatus.DETECTED)


if __name__ == "__main__":
    unittest.main()
