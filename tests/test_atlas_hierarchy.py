import unittest

from atlas.director import (
    CRITICAL_ACTION_TYPES,
    AtlasDirector,
    AtlasHierarchyViolation,
)
from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.security.audit import AuditTrail
from core.security.rbac import AccessDenied, Permission, Principal, Role, has_permission

"""
Testes ATLAS — HIERARQUIA E LIMITES DE AUTORIDADE.

Hierarquia obrigatória: OWNER -> SUPERVISÃO/GOVERNANÇA -> ATLAS ->
demais IAs/agentes. ATLAS NÃO é Owner e não pode alterar autoridade,
governança, segurança, credenciais ou regras protegidas.
"""


def make_atlas(scopes=None):
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes=scopes or {"*"})
    return AtlasDirector(facade=facade, principal=principal), facade, audit


class HierarchyConstructionTests(unittest.TestCase):
    def test_atlas_director_requires_atlas_role(self):
        mgc = MultiGerenciadorComercial()
        audit = AuditTrail()
        facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)

        for wrong_role in (Role.OWNER, Role.OPERATOR, Role.AGENT, Role.VIEWER):
            principal = Principal(subject="x", role=wrong_role, scopes={"*"})
            with self.assertRaises(AtlasHierarchyViolation):
                AtlasDirector(facade=facade, principal=principal)

    def test_atlas_director_constructs_with_atlas_role(self):
        atlas, _, _ = make_atlas()
        self.assertEqual(atlas.principal.role, Role.ATLAS)


class AtlasPermissionBoundaryTests(unittest.TestCase):
    """ATLAS nunca recebe, por padrão, permissões de Owner."""

    def test_atlas_lacks_manage_credentials(self):
        principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})
        self.assertFalse(has_permission(principal, Permission.MANAGE_CREDENTIALS))

    def test_atlas_lacks_manage_global_operations(self):
        principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})
        self.assertFalse(has_permission(principal, Permission.MANAGE_GLOBAL_OPERATIONS))

    def test_atlas_lacks_view_global_dashboard(self):
        principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})
        self.assertFalse(has_permission(principal, Permission.VIEW_GLOBAL_DASHBOARD))

    def test_atlas_lacks_approve_publication(self):
        principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})
        self.assertFalse(has_permission(principal, Permission.APPROVE_PUBLICATION))

    def test_atlas_lacks_publish_content(self):
        principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})
        self.assertFalse(has_permission(principal, Permission.PUBLISH_CONTENT))

    def test_atlas_has_operational_coordination_permissions(self):
        principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})
        for perm in (
            Permission.MANAGE_CRM, Permission.MANAGE_SALES, Permission.MANAGE_JOBS,
            Permission.MANAGE_LEARNING, Permission.MANAGE_PRODUCTS, Permission.MANAGE_FUNNELS,
            Permission.VIEW_DASHBOARD, Permission.RUN_PRODUCTION_JOB, Permission.READ_CATALOG,
        ):
            self.assertTrue(has_permission(principal, perm), f"ATLAS deveria ter {perm.value}")


class CriticalActionsAlwaysEscalatedTests(unittest.TestCase):
    """item 15: nunca publicar, gastar, contratar, alterar credenciais
    ou conectar conta externa sem a autoridade correspondente — ATLAS
    SEMPRE escalona, nunca executa."""

    def setUp(self):
        self.atlas, self.facade, self.audit = make_atlas()

    def test_all_critical_action_types_are_escalated_not_executed(self):
        for action_type in CRITICAL_ACTION_TYPES:
            item = self.atlas.request_critical_action(action_type=action_type, details={})
            self.assertFalse(item.resolved, f"{action_type} nunca deveria ser resolvido automaticamente")
            self.assertIn(action_type, item.context["action_type"])

    def test_publish_content_action_never_executes_anything(self):
        item = self.atlas.request_critical_action(
            action_type="publish_content", details={"channel_id": "chan_1", "title": "video"}
        )
        self.assertFalse(item.resolved)
        self.assertIn("Owner", item.reason)

    def test_spend_money_action_never_executes_anything(self):
        item = self.atlas.request_critical_action(action_type="spend_money", details={"amount": 500})
        self.assertFalse(item.resolved)

    def test_modify_credentials_action_never_executes_anything(self):
        item = self.atlas.request_critical_action(action_type="modify_credentials", details={})
        self.assertFalse(item.resolved)

    def test_unknown_critical_action_type_rejected_outright(self):
        with self.assertRaises(ValueError):
            self.atlas.request_critical_action(action_type="do_something_undefined", details={})

    def test_critical_action_is_audited(self):
        self.atlas.request_critical_action(action_type="connect_external_account", details={})
        events = [e for e in self.audit.all() if e.action == "atlas_escalate_to_owner"]
        self.assertTrue(any("connect_external_account" in str(e.details) for e in events))


class AttemptToExceedAuthorityTests(unittest.TestCase):
    """Tentativas explícitas de o ATLAS ultrapassar sua autoridade."""

    def setUp(self):
        self.atlas, self.facade, self.audit = make_atlas(scopes={"channel:chan_1"})

    def test_atlas_cannot_receive_objective_from_non_owner_principal(self):
        fake_owner = Principal(subject="not-owner", role=Role.OPERATOR, scopes={"*"})
        with self.assertRaises(AccessDenied):
            self.atlas.receive_objective(fake_owner, objective="Crescer vendas", channel_id="chan_1")

    def test_atlas_cannot_resolve_its_own_escalation(self):
        item = self.atlas.request_critical_action(action_type="spend_money", details={})
        with self.assertRaises(AccessDenied):
            self.atlas.escalation_desk.resolve(
                item.escalation_id, resolution="aprovado", resolved_by=self.atlas.principal
            )

    def test_atlas_cannot_manage_finance_directly(self):
        # ATLAS não tem MANAGE_FINANCE — qualquer tentativa de gravar um
        # lançamento financeiro diretamente pela fachada é recusada.
        with self.assertRaises(AccessDenied):
            self.facade.record_finance_entry(
                self.atlas.principal, entry_type="revenue", amount=100, channel_id="chan_1"
            )

    def test_atlas_cannot_view_global_dashboard(self):
        from core.isolation.isolation import IsolationViolation
        # mesmo com escopo coringa, sem VIEW_GLOBAL_DASHBOARD o agregado
        # global é recusado.
        wildcard_atlas, facade2, _ = make_atlas(scopes={"*"})
        with self.assertRaises(AccessDenied):
            wildcard_atlas.analyze_performance(channel_id=None)

    def test_atlas_cannot_enqueue_global_job_without_channel(self):
        wildcard_atlas, facade2, _ = make_atlas(scopes={"*"})
        with self.assertRaises(AccessDenied):
            facade2.enqueue_job(
                wildcard_atlas.principal, job_type="system_maintenance", idempotency_key="k1"
            )

    def test_atlas_construction_with_owner_role_impossible(self):
        mgc = MultiGerenciadorComercial()
        audit = AuditTrail()
        facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
        forged = Principal(subject="atlas@system", role=Role.OWNER, scopes={"*"})
        with self.assertRaises(AtlasHierarchyViolation):
            AtlasDirector(facade=facade, principal=forged)


if __name__ == "__main__":
    unittest.main()
