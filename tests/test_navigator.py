import os
import tempfile
import unittest

from atlas.director import AtlasDirector
from bridge.hub import BridgeHub, ConnectorType, HealthStatus
from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.isolation.isolation import IsolationViolation
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role
from navigator.adapters import PlatformExecutionAdapter
from navigator.executor import NavigatorExecutor, NavigatorHierarchyViolation, UnknownPlatform
from navigator.models import CRITICAL_NAVIGATOR_ACTIONS, ExecutionResult, NavigatorAction

"""
Testes NAVIGATOR - delegacao pelo ATLAS, selecao de mecanismo de
execucao, executor temporario, isolamento entre contas, autorizacao,
idempotencia, retry, evidencia, falha, restart, acao critica bloqueada
sem aprovacao.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


def make_navigator(*, scopes=None, db_path=None):
    audit = AuditTrail(db_path=db_path)
    from atlas.escalation import EscalationDesk
    desk = EscalationDesk(audit, db_path=db_path)
    principal = Principal(subject="navigator@system", role=Role.NAVIGATOR, scopes=scopes or {"*"})
    nav = NavigatorExecutor(principal=principal, escalation_desk=desk, audit=audit, db_path=db_path)
    return nav, desk, audit


class HierarchyTests(unittest.TestCase):
    def test_requires_navigator_role(self):
        audit = AuditTrail()
        from atlas.escalation import EscalationDesk
        desk = EscalationDesk(audit)
        for wrong_role in (Role.OWNER, Role.ATLAS, Role.FORGE, Role.OPERATOR):
            with self.assertRaises(NavigatorHierarchyViolation):
                NavigatorExecutor(principal=Principal(subject="x", role=wrong_role, scopes={"*"}), escalation_desk=desk, audit=audit)


class DelegationByAtlasTests(unittest.TestCase):
    def test_atlas_delegates_mission_and_navigator_executes(self):
        mgc = MultiGerenciadorComercial()
        audit = AuditTrail()
        facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
        atlas = AtlasDirector(facade=facade, principal=Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"}))

        mission = atlas.delegate_mission(agent="NAVIGATOR", objective="coletar metricas", context={"channel": "chan_1"})
        entry = atlas.navigator.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id=mission.mission_id, task_id=mission.task_id,
        )
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)
        self.assertEqual(entry.mission_id, mission.mission_id)


class MechanismSelectionTests(unittest.TestCase):
    def test_priority_order_official_api_first(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.BROWSER_AUTOMATION,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        best = hub.best_available_execution_path(platform="youtube")
        self.assertEqual(best.connector_type, ConnectorType.OFFICIAL_API)

    def test_falls_back_to_lower_priority_mechanism(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="tiktok", connector_type=ConnectorType.COMPUTER_USE_RPA,
            is_configured=True, authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        best = hub.best_available_execution_path(platform="tiktok")
        self.assertEqual(best.connector_type, ConnectorType.COMPUTER_USE_RPA)

    def test_no_connector_registered_returns_none(self):
        hub = BridgeHub()
        self.assertIsNone(hub.best_available_execution_path(platform="never_registered"))

    def test_not_configured_connector_never_returned_as_available(self):
        """CORREÇÃO item 5: nunca retorna como disponível um conector
        com is_configured=False, mesmo que seja o único registrado."""
        hub = BridgeHub()
        hub.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=False)
        self.assertIsNone(hub.best_available_execution_path(platform="youtube"))

    def test_unauthenticated_real_connector_never_returned(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
            is_configured=True, authenticated=False,
        )
        self.assertIsNone(hub.best_available_execution_path(platform="youtube"))

    def test_missing_required_scope_never_returned(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True,
            authenticated=True, health=HealthStatus.OK, permissions_ok=True,
            scopes=["read"], required_scopes=["upload"],
        )
        self.assertIsNone(hub.best_available_execution_path(platform="youtube"))

    def test_unhealthy_connector_never_returned(self):
        hub = BridgeHub()
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True,
            authenticated=True, health=HealthStatus.DOWN, permissions_ok=True,
        )
        self.assertIsNone(hub.best_available_execution_path(platform="youtube"))

    def test_mock_fallback_always_viable_and_lowest_priority(self):
        hub = BridgeHub()
        hub.register_mock_fallback(platform="blog")
        best = hub.best_available_execution_path(platform="blog")
        self.assertEqual(best.connector_type, ConnectorType.MOCK_FALLBACK)

    def test_mock_fallback_never_wins_over_real_configured_connector(self):
        hub = BridgeHub()
        hub.register_mock_fallback(platform="youtube")
        hub.register_connector(
            platform="youtube", connector_type=ConnectorType.OFFICIAL_API, is_configured=True,
            authenticated=True, health=HealthStatus.OK, permissions_ok=True,
        )
        best = hub.best_available_execution_path(platform="youtube")
        self.assertEqual(best.connector_type, ConnectorType.OFFICIAL_API)

    def test_adapter_never_reports_real_without_configuration(self):
        adapter = PlatformExecutionAdapter(provider_name="youtube")
        self.assertFalse(adapter.is_configured())
        result = adapter.execute(action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(result.mode.value, "MOCK")


class TemporaryExecutorTests(unittest.TestCase):
    def test_spawn_and_run(self):
        nav, _, _ = make_navigator()
        temp = nav.spawn_temporary_executor(platform="tiktok")
        entry = temp.run(action=NavigatorAction.CHECK_STATUS, payload={})
        self.assertEqual(entry.executor, temp.executor_id)
        self.assertIn("exec_tiktok_", temp.executor_id)

    def test_unknown_platform_rejected(self):
        nav, _, _ = make_navigator()
        with self.assertRaises(UnknownPlatform):
            nav.spawn_temporary_executor(platform="plataforma-nunca-registrada")

    def test_temporary_executors_do_not_need_to_persist(self):
        nav, _, _ = make_navigator()
        temp = nav.spawn_temporary_executor(platform="blog")
        self.assertIn(temp.executor_id, [e.executor_id for e in nav.temporary_executors()])


class IsolationTests(unittest.TestCase):
    def test_scoped_navigator_cannot_act_on_other_channel(self):
        nav, _, _ = make_navigator(scopes={"channel:chan_1"})
        with self.assertRaises(IsolationViolation):
            nav.execute(platform="youtube", action=NavigatorAction.CHECK_STATUS, payload={}, channel_id="chan_other")

    def test_scoped_navigator_can_act_on_own_channel(self):
        nav, _, _ = make_navigator(scopes={"channel:chan_1"})
        entry = nav.execute(platform="youtube", action=NavigatorAction.CHECK_STATUS, payload={}, channel_id="chan_1")
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)

    def test_no_channel_specified_never_checked(self):
        nav, _, _ = make_navigator(scopes={"channel:chan_1"})
        entry = nav.execute(platform="youtube", action=NavigatorAction.CHECK_STATUS, payload={})
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)


class AuthorizationAndCriticalActionTests(unittest.TestCase):
    def test_all_critical_actions_always_escalated(self):
        nav, desk, _ = make_navigator()
        for action in CRITICAL_NAVIGATOR_ACTIONS:
            entry = nav.execute(platform="youtube", action=action, payload={})
            self.assertEqual(entry.result, ExecutionResult.ESCALATED.value)
            self.assertIsNotNone(entry.approval_used)

    def test_critical_action_never_executes_without_approval(self):
        nav, desk, _ = make_navigator()
        entry = nav.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={"title": "x"})
        self.assertNotEqual(entry.result, ExecutionResult.SUCCESS.value)
        pending = desk.pending()
        self.assertEqual(len(pending), 1)

    def test_non_critical_actions_execute_directly(self):
        nav, desk, _ = make_navigator()
        entry = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)
        self.assertEqual(len(desk.pending()), 0)

    def test_owner_can_approve_escalated_navigator_action(self):
        nav, desk, _ = make_navigator()
        entry = nav.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={})
        approved = desk.approve(entry.approval_used, decision="aprovado pelo Owner", resolved_by=OWNER)
        self.assertEqual(approved.status.value, "APPROVED")


class IdempotencyTests(unittest.TestCase):
    def test_same_mission_task_action_not_reexecuted(self):
        nav, _, _ = make_navigator()
        e1 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, mission_id="m1", task_id="t1")
        e2 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, mission_id="m1", task_id="t1")
        self.assertEqual(e1.entry_id, e2.entry_id)

    def test_different_task_id_executes_independently(self):
        nav, _, _ = make_navigator()
        e1 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, mission_id="m1", task_id="t1")
        e2 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, mission_id="m1", task_id="t2")
        self.assertNotEqual(e1.entry_id, e2.entry_id)

    def test_without_mission_task_still_deduplicates_by_full_content_identity(self):
        """CORREÇÃO (idempotência completa): sem mission_id/task_id, a
        identidade passa a ser derivada do CONTEÚDO completo (platform,
        action, payload, conta, canal) — duas chamadas com o MESMO
        conteúdo são a MESMA execução por definição, não duas
        execuções acidentalmente distintas."""
        nav, _, _ = make_navigator()
        e1 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        e2 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(e1.entry_id, e2.entry_id)

    def test_different_payload_without_mission_task_executes_independently(self):
        nav, _, _ = make_navigator()
        e1 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={"range": "7d"})
        e2 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={"range": "30d"})
        self.assertNotEqual(e1.entry_id, e2.entry_id)


class RetryAndFailureTests(unittest.TestCase):
    def test_transient_failure_retries_and_eventually_succeeds(self):
        from bridge.hub import ConnectorType
        from navigator.mechanism_executors import MockFallbackExecutor

        class FlakyExecutor(MockFallbackExecutor):
            def __init__(self):
                super().__init__(provider_name="youtube")
                self.calls = 0

            def execute(self, *, action, payload):
                self.calls += 1
                if self.calls <= 2:
                    raise RuntimeError("falha transitoria")
                return super().execute(action=action, payload=payload)

        nav, _, _ = make_navigator()
        nav.register_mechanism_executor(platform="youtube", mechanism=ConnectorType.MOCK_FALLBACK, executor=FlakyExecutor())
        entry = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.SUCCESS.value)
        self.assertGreaterEqual(entry.retry_count, 2)

    def test_persistent_failure_recorded_as_failed_after_retries(self):
        from bridge.hub import ConnectorType
        from navigator.mechanism_executors import MockFallbackExecutor

        class AlwaysFailingExecutor(MockFallbackExecutor):
            def execute(self, *, action, payload):
                raise RuntimeError("falha permanente")

        nav, _, _ = make_navigator()
        nav.max_retries = 2
        nav.register_mechanism_executor(
            platform="youtube", mechanism=ConnectorType.MOCK_FALLBACK,
            executor=AlwaysFailingExecutor(provider_name="youtube"),
        )
        entry = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={})
        self.assertEqual(entry.result, ExecutionResult.FAILED.value)
        self.assertIsNotNone(entry.error)

    def test_evidence_recorded_on_success(self):
        nav, _, _ = make_navigator()
        entry = nav.execute(platform="blog", action=NavigatorAction.CHECK_STATUS, payload={})
        self.assertIn("mode", entry.evidence)

    def test_unknown_platform_recorded_as_failed(self):
        nav, _, _ = make_navigator()
        entry = nav.execute(platform="never-registered", action=NavigatorAction.CHECK_STATUS, payload={})
        self.assertEqual(entry.result, ExecutionResult.FAILED.value)


class RestartTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def test_execution_log_survives_restart(self):
        nav1, _, _ = make_navigator(db_path=self.db_path)
        entry = nav1.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id="m1", task_id="t1",
        )
        nav2, _, _ = make_navigator(db_path=self.db_path)
        recovered = nav2.log.by_mission("m1")
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].entry_id, entry.entry_id)

    def test_idempotency_preserved_after_restart(self):
        nav1, _, _ = make_navigator(db_path=self.db_path)
        e1 = nav1.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id="m1", task_id="t1",
        )
        nav2, _, _ = make_navigator(db_path=self.db_path)
        e2 = nav2.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={},
            mission_id="m1", task_id="t1",
        )
        self.assertEqual(e1.entry_id, e2.entry_id)


if __name__ == "__main__":
    unittest.main()


class ExpiredApprovalAndDifferentContextTests(unittest.TestCase):
    """Testes adversariais adicionais exigidos pela correção: aprovação
    EXPIRED nunca autoriza; payload/conta/plataforma diferentes nunca
    reutilizam a mesma identidade de execução."""

    def test_expired_approval_never_authorizes(self):
        nav, desk, _ = make_navigator()
        entry = nav.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={"title": "x"})
        desk.expire_stale(ttl_seconds=-1)  # forca expiracao imediata
        retry = nav.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={"title": "x"})
        self.assertEqual(retry.result, ExecutionResult.BLOCKED.value)

    def test_different_platform_same_action_different_identity(self):
        nav, _, _ = make_navigator()
        e1 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, mission_id="m1", task_id="t1")
        e2 = nav.execute(platform="instagram", action=NavigatorAction.COLLECT_METRICS, payload={}, mission_id="m1", task_id="t1")
        self.assertNotEqual(e1.entry_id, e2.entry_id)

    def test_different_account_context_different_identity(self):
        nav, _, _ = make_navigator()
        e1 = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, mission_id="m1", task_id="t1",
            account_context="conta_a",
        )
        e2 = nav.execute(
            platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, mission_id="m1", task_id="t1",
            account_context="conta_b",
        )
        self.assertNotEqual(e1.entry_id, e2.entry_id)

    def test_different_channel_different_identity(self):
        nav, _, _ = make_navigator(scopes={"*"})
        e1 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, channel_id="chan_a")
        e2 = nav.execute(platform="youtube", action=NavigatorAction.COLLECT_METRICS, payload={}, channel_id="chan_b")
        self.assertNotEqual(e1.entry_id, e2.entry_id)

    def test_approval_used_linked_to_execution(self):
        nav, desk, _ = make_navigator()
        pending = nav.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={})
        desk.approve(pending.approval_used, decision="ok", resolved_by=OWNER)
        consumed = nav.execute(platform="youtube", action=NavigatorAction.PUBLISH, payload={})
        self.assertEqual(consumed.approval_used, pending.approval_used)
        self.assertEqual(consumed.result, ExecutionResult.SUCCESS.value)
