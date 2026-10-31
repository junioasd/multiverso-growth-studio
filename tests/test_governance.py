import unittest

from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade, InvalidBinding, InvalidIdentity
from core.governance.replay_guard import ReplayRejected
from core.isolation.isolation import IsolationViolation
from core.jobs.queue import JobQueue
from core.models.entities import LeadStatus, ValueType
from core.security.audit import AuditTrail
from core.security.rbac import AccessDenied, Permission, Principal, Role


def make_facade():
    job_queue = JobQueue()
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=job_queue, multigerenciador=mgc, audit=audit)
    return facade, audit


class InvalidIdentityTests(unittest.TestCase):
    """Fail-closed quando identidade for inválida/ausente."""

    def setUp(self):
        self.facade, self.audit = make_facade()

    def test_none_principal_denied_on_create_lead(self):
        with self.assertRaises(InvalidIdentity):
            self.facade.create_lead(None, channel_id="chan_1", source="youtube")

    def test_none_principal_denied_on_sales_event(self):
        with self.assertRaises(InvalidIdentity):
            self.facade.record_sales_event(None, channel_id="chan_1", event_type="click")

    def test_none_principal_denied_on_finance_entry(self):
        with self.assertRaises(InvalidIdentity):
            self.facade.record_finance_entry(None, entry_type="revenue", amount=100)

    def test_none_principal_denied_on_job_enqueue(self):
        with self.assertRaises(InvalidIdentity):
            self.facade.enqueue_job(None, job_type="produce_video", idempotency_key="k1")

    def test_none_principal_denied_on_learning(self):
        with self.assertRaises(InvalidIdentity):
            self.facade.record_learning(None, scope_key="chan_1|x", outcome="worked", context={})

    def test_denial_is_audited(self):
        try:
            self.facade.create_lead(None, channel_id="chan_1")
        except InvalidIdentity:
            pass
        events = self.audit.all()
        self.assertTrue(any(e.outcome == "DENIED" and e.action == "create_lead" for e in events))


class InvalidBindingTests(unittest.TestCase):
    """Fail-closed quando projeto/canal forem ausentes ou UNKNOWN."""

    def setUp(self):
        self.facade, self.audit = make_facade()
        self.owner = Principal(subject="owner@example.com", role=Role.OWNER, scopes={"*"})

    def test_unknown_channel_denied(self):
        with self.assertRaises(InvalidBinding):
            self.facade.create_lead(self.owner, channel_id="UNKNOWN")

    def test_empty_channel_denied(self):
        with self.assertRaises(InvalidBinding):
            self.facade.record_sales_event(self.owner, channel_id="", event_type="click")

    def test_none_channel_denied_for_lead(self):
        with self.assertRaises(InvalidBinding):
            self.facade.create_lead(self.owner, channel_id=None)

    def test_unknown_project_denied_for_job_when_required(self):
        with self.assertRaises(InvalidBinding):
            self.facade.enqueue_job(
                self.owner, job_type="produce_video", idempotency_key="k1", project_id="UNKNOWN"
            )

    def test_global_finance_entry_without_channel_allowed(self):
        # lançamentos financeiros globais (sem canal específico) são
        # legítimos e não devem ser bloqueados por binding ausente.
        entry = self.facade.record_finance_entry(self.owner, entry_type="expense", amount=50)
        self.assertEqual(entry.entry_type, "expense")


class RBACEnforcementTests(unittest.TestCase):
    """Operações mutáveis exigem a Permission correta."""

    def setUp(self):
        self.facade, self.audit = make_facade()

    def test_viewer_cannot_create_lead(self):
        viewer = Principal(subject="viewer-1", role=Role.VIEWER, scopes={"channel:chan_1"})
        with self.assertRaises(AccessDenied):
            self.facade.create_lead(viewer, channel_id="chan_1")

    def test_agent_cannot_record_sales_event(self):
        agent = Principal(subject="agent-1", role=Role.AGENT, scopes={"channel:chan_1"})
        with self.assertRaises(AccessDenied):
            self.facade.record_sales_event(agent, channel_id="chan_1", event_type="click")

    def test_agent_cannot_manage_finance(self):
        agent = Principal(subject="agent-1", role=Role.AGENT, scopes={"channel:chan_1"})
        with self.assertRaises(AccessDenied):
            self.facade.record_finance_entry(agent, entry_type="revenue", amount=10, channel_id="chan_1")

    def test_operator_can_create_lead_within_scope(self):
        operator = Principal(subject="op-1", role=Role.OPERATOR, scopes={"channel:chan_1"})
        lead = self.facade.create_lead(operator, channel_id="chan_1", source="youtube")
        self.assertEqual(lead.channel_id, "chan_1")


class CrossScopeGovernanceTests(unittest.TestCase):
    """Isolamento aplicado mesmo quando a operação passa pela fachada."""

    def setUp(self):
        self.facade, self.audit = make_facade()

    def test_operator_scoped_to_chan_1_cannot_touch_chan_2(self):
        operator = Principal(subject="op-1", role=Role.OPERATOR, scopes={"channel:chan_1"})
        with self.assertRaises(IsolationViolation):
            self.facade.create_lead(operator, channel_id="chan_2")

    def test_operator_cannot_enqueue_job_outside_scope(self):
        operator = Principal(subject="op-1", role=Role.OPERATOR, scopes={"channel:chan_1"})
        with self.assertRaises(IsolationViolation):
            self.facade.enqueue_job(
                operator, job_type="produce_video", idempotency_key="k1", channel_id="chan_2"
            )

    def test_owner_wildcard_scope_bypasses_isolation(self):
        owner = Principal(subject="owner@example.com", role=Role.OWNER, scopes={"*"})
        lead = self.facade.create_lead(owner, channel_id="chan_99")
        self.assertEqual(lead.channel_id, "chan_99")

    def test_transition_lead_rejects_channel_mismatch_even_if_scoped(self):
        operator = Principal(subject="op-1", role=Role.OPERATOR, scopes={"channel:chan_1", "channel:chan_2"})
        lead = self.facade.create_lead(operator, channel_id="chan_1")
        # Tenta transicionar o lead alegando pertencer ao chan_2 (mentira) —
        # defesa em profundidade deve bloquear mesmo com escopo amplo.
        with self.assertRaises(IsolationViolation):
            self.facade.transition_lead(
                operator, channel_id="chan_2", lead_id=lead.lead_id, new_status=LeadStatus.ENGAGED
            )

    def test_job_start_checked_against_jobs_actual_channel(self):
        owner = Principal(subject="owner@example.com", role=Role.OWNER, scopes={"*"})
        job = self.facade.enqueue_job(
            owner, job_type="produce_video", idempotency_key="k1", channel_id="chan_1"
        )
        # operator sem escopo do chan_1 não pode iniciar o job, mesmo
        # conhecendo o job_id.
        operator = Principal(subject="op-1", role=Role.OPERATOR, scopes={"channel:chan_2"})
        with self.assertRaises(IsolationViolation):
            self.facade.start_job(operator, job_id=job.job_id)


class ReplayProtectionTests(unittest.TestCase):
    def setUp(self):
        self.facade, self.audit = make_facade()
        self.owner = Principal(subject="owner@example.com", role=Role.OWNER, scopes={"*"})

    def test_same_operation_id_rejected_on_replay(self):
        self.facade.record_sales_event(
            self.owner, channel_id="chan_1", event_type="click", operation_id="op-abc-1"
        )
        with self.assertRaises(ReplayRejected):
            self.facade.record_sales_event(
                self.owner, channel_id="chan_1", event_type="click", operation_id="op-abc-1"
            )

    def test_different_operation_ids_both_allowed(self):
        self.facade.record_sales_event(
            self.owner, channel_id="chan_1", event_type="click", operation_id="op-1"
        )
        self.facade.record_sales_event(
            self.owner, channel_id="chan_1", event_type="click", operation_id="op-2"
        )
        summary = self.facade.sales_tracker.summarize("chan_1")
        self.assertEqual(summary.clicks, 2)

    def test_no_operation_id_never_replay_blocked(self):
        # sem operation_id, a proteção de replay desta camada não se aplica
        # (a idempotência de domínio, via idempotency_key, continua valendo)
        self.facade.record_sales_event(self.owner, channel_id="chan_1", event_type="view")
        self.facade.record_sales_event(self.owner, channel_id="chan_1", event_type="view")
        summary = self.facade.sales_tracker.summarize("chan_1")
        self.assertEqual(summary.views, 2)


class GovernedFacadeAuditTests(unittest.TestCase):
    def setUp(self):
        self.facade, self.audit = make_facade()
        self.owner = Principal(subject="owner@example.com", role=Role.OWNER, scopes={"*"})

    def test_success_is_audited(self):
        self.facade.create_lead(self.owner, channel_id="chan_1")
        events = self.audit.all()
        self.assertTrue(any(e.outcome == "SUCCESS" and e.action == "create_lead" for e in events))

    def test_audit_never_contains_raw_secrets(self):
        agent = Principal(subject="agent-1", role=Role.AGENT, scopes={"channel:chan_1"})
        try:
            self.facade.record_finance_entry(
                agent, entry_type="revenue", amount=1000, channel_id="chan_1"
            )
        except AccessDenied:
            pass
        for event in self.audit.all():
            self.assertNotIn("password", str(event.details).lower())


if __name__ == "__main__":
    unittest.main()
