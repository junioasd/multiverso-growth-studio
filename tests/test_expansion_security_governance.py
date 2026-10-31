import unittest

from atlas.director import AtlasDirector, CRITICAL_ACTION_TYPES
from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.governance.idempotency import IdempotencyConflict
from core.jobs.queue import JobQueue
from core.security.audit import AuditTrail
from core.security.rbac import (
    AccessDenied,
    Permission,
    Principal,
    Role,
    has_permission,
)
from forge.engineer import ForgeEngineer, FixNotAuthorized
from navigator.executor import NavigatorExecutor
from navigator.models import ExecutionResult, NavigatorAction

"""
Testes SEGURANCA/GOVERNANCA (expansao) - Owner approval, idempotencia,
menor privilegio, tentativa de acao nao autorizada, dados sensiveis.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
ATLAS_PRINCIPAL = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})


def make_atlas():
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    return AtlasDirector(facade=facade, principal=ATLAS_PRINCIPAL), audit


class OwnerApprovalTests(unittest.TestCase):
    def test_expanded_critical_actions_all_escalate(self):
        atlas, _ = make_atlas()
        for action_type in CRITICAL_ACTION_TYPES:
            item = atlas.request_critical_action(action_type=action_type, details={})
            self.assertFalse(item.resolved)

    def test_new_critical_action_categories_present(self):
        for expected in (
            "transfer_funds", "destructive_deletion", "modify_security", "modify_ownership",
            "critical_structural_change", "sensitive_publication", "irreversible_action",
        ):
            self.assertIn(expected, CRITICAL_ACTION_TYPES)

    def test_original_critical_actions_preserved(self):
        for expected in (
            "publish_content", "spend_money", "contract_service", "modify_credentials", "connect_external_account",
        ):
            self.assertIn(expected, CRITICAL_ACTION_TYPES)

    def test_owner_approves_navigator_critical_action(self):
        atlas, _ = make_atlas()
        entry = atlas.navigator.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={})
        approved = atlas.escalation_desk.approve(entry.approval_used, decision="ok", resolved_by=OWNER)
        self.assertEqual(approved.status.value, "APPROVED")

    def test_forge_high_risk_fix_requires_owner(self):
        atlas, _ = make_atlas()
        from forge.models import RiskLevel

        d = atlas.forge.detect(issue_summary="x", evidence={})
        atlas.forge.diagnose(d.diagnostic_id, analysis={})
        atlas.forge.identify_root_cause(d.diagnostic_id, root_cause="x")
        atlas.forge.propose_fix(d.diagnostic_id, proposed_fix="x")
        atlas.forge.assess_risk(d.diagnostic_id, risk_level=RiskLevel.HIGH)
        with self.assertRaises(FixNotAuthorized):
            atlas.forge.approve_fix(d.diagnostic_id, authorized_by=ATLAS_PRINCIPAL)
        approved = atlas.forge.approve_fix(d.diagnostic_id, authorized_by=OWNER)
        self.assertEqual(approved.status.value, "FIX_APPROVED")


class IdempotencyTests(unittest.TestCase):
    def test_navigator_execution_idempotent_by_mission_task(self):
        atlas, _ = make_atlas()
        e1 = atlas.navigator.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id="m1", task_id="t1",
        )
        e2 = atlas.navigator.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id="m1", task_id="t1",
        )
        self.assertEqual(e1.entry_id, e2.entry_id)

    def test_mission_creation_idempotent(self):
        atlas, _ = make_atlas()
        from core.coordination.mission import MissionAssignment

        m = MissionAssignment.new(agent="NAVIGATOR", objective="x", mission_id="fixed", task_id="fixed_t")
        created1 = atlas.missions.create(m)
        created2 = atlas.missions.create(m)
        self.assertEqual(created1.mission_id, created2.mission_id)

    def test_critical_action_idempotency_key_reused_conflicts_on_different_content(self):
        atlas, _ = make_atlas()
        atlas.request_critical_action(action_type="spend_money", details={"amount": 100}, idempotency_key="k1")
        with self.assertRaises(IdempotencyConflict):
            atlas.request_critical_action(action_type="spend_money", details={"amount": 999}, idempotency_key="k1")


class LeastPrivilegeTests(unittest.TestCase):
    def test_navigator_lacks_publish_and_credentials(self):
        p = Principal(subject="x", role=Role.NAVIGATOR, scopes={"*"})
        self.assertFalse(has_permission(p, Permission.PUBLISH_CONTENT))
        self.assertFalse(has_permission(p, Permission.MANAGE_CREDENTIALS))
        self.assertFalse(has_permission(p, Permission.MANAGE_GLOBAL_OPERATIONS))

    def test_forge_lacks_business_mutation_permissions(self):
        p = Principal(subject="x", role=Role.FORGE, scopes={"*"})
        self.assertFalse(has_permission(p, Permission.MANAGE_CRM))
        self.assertFalse(has_permission(p, Permission.MANAGE_SALES))
        self.assertFalse(has_permission(p, Permission.MANAGE_CREDENTIALS))

    def test_navigator_and_forge_have_minimal_read_permissions_only(self):
        nav = Principal(subject="x", role=Role.NAVIGATOR, scopes={"*"})
        forge = Principal(subject="y", role=Role.FORGE, scopes={"*"})
        self.assertTrue(has_permission(nav, Permission.READ_CATALOG))
        self.assertTrue(has_permission(forge, Permission.VIEW_DASHBOARD))


class UnauthorizedAttemptTests(unittest.TestCase):
    def test_operator_cannot_apply_forge_fix(self):
        audit = AuditTrail()
        forge = ForgeEngineer(principal=Principal(subject="f", role=Role.FORGE, scopes={"*"}), audit=audit)
        from forge.models import RiskLevel

        d = forge.detect(issue_summary="x", evidence={})
        forge.diagnose(d.diagnostic_id, analysis={})
        forge.identify_root_cause(d.diagnostic_id, root_cause="x")
        forge.propose_fix(d.diagnostic_id, proposed_fix="x")
        forge.assess_risk(d.diagnostic_id, risk_level=RiskLevel.LOW)
        unauthorized = Principal(subject="op", role=Role.OPERATOR, scopes={"*"})
        with self.assertRaises(FixNotAuthorized):
            forge.approve_fix(d.diagnostic_id, authorized_by=unauthorized)

    def test_non_owner_cannot_approve_escalation(self):
        atlas, _ = make_atlas()
        item = atlas.request_critical_action(action_type="spend_money", details={})
        operator = Principal(subject="op", role=Role.OPERATOR, scopes={"*"})
        with self.assertRaises(AccessDenied):
            atlas.escalation_desk.approve(item.escalation_id, decision="x", resolved_by=operator)

    def test_navigator_cannot_act_outside_scoped_channel(self):
        from atlas.escalation import EscalationDesk
        from core.isolation.isolation import IsolationViolation

        audit = AuditTrail()
        desk = EscalationDesk(audit)
        nav = NavigatorExecutor(
            principal=Principal(subject="n", role=Role.NAVIGATOR, scopes={"channel:chan_1"}),
            escalation_desk=desk, audit=audit,
        )
        with self.assertRaises(IsolationViolation):
            nav.execute(platform="youtube", action=NavigatorAction.CHECK_STATUS, payload={}, channel_id="chan_2")


class SensitiveDataTests(unittest.TestCase):
    def test_working_memory_redacts_password(self):
        from core.memory.working_memory import WorkingMemory

        mem = WorkingMemory()
        mem.remember_short_term("creds", {"password": "hunter2", "token": "abc123"})
        recalled = mem.recall_short_term("creds")
        self.assertNotIn("hunter2", str(recalled))

    def test_bridge_never_stores_real_secret_value(self):
        from bridge.hub import BridgeHub

        hub = BridgeHub()
        ref = hub.register_credential_reference(
            platform="youtube", scopes=["upload"], external_vault_key="VAULT_YOUTUBE_TOKEN",
        )
        # só a referência é guardada -- nenhum campo carrega um valor de segredo.
        self.assertEqual(ref.external_vault_key, "VAULT_YOUTUBE_TOKEN")
        self.assertFalse(hasattr(ref, "secret_value"))

    def test_navigator_escalation_context_sanitized(self):
        atlas, _ = make_atlas()
        entry = atlas.navigator.execute(
            platform="youtube", action=NavigatorAction.PUBLISH,
            payload={"password": "hunter2", "title": "video"},
        )
        item = atlas.escalation_desk.get(entry.approval_used)
        self.assertNotIn("hunter2", str(item.context))


if __name__ == "__main__":
    unittest.main()
