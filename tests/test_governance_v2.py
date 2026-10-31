import unittest

from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.governance.idempotency import IdempotencyConflict
from core.jobs.queue import JobQueue
from core.models.entities import LeadStatus, ValueType
from core.security.audit import AuditTrail
from core.security.rbac import AccessDenied, Principal, Role
from commercial.crm import InvalidLeadTransition

"""
Testes de regressão — MICROCORREÇÃO 2, itens 2 (GOVERNANÇA) e 3 (AUDITORIA).
"""


def make_facade():
    job_queue = JobQueue()
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=job_queue, multigerenciador=mgc, audit=audit)
    return facade, audit


class ExplicitGlobalPermissionTests(unittest.TestCase):
    """Operações sem canal/projeto exigem MANAGE_GLOBAL_OPERATIONS além
    da permissão de domínio — não basta MANAGE_JOBS/MANAGE_FINANCE."""

    def setUp(self):
        self.facade, self.audit = make_facade()
        self.operator = Principal(subject="op-1", role=Role.OPERATOR, scopes={"*"})
        self.owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})

    def test_operator_cannot_enqueue_global_job(self):
        # Operator TEM MANAGE_JOBS, mas job sem canal/projeto é "global"
        # e exige MANAGE_GLOBAL_OPERATIONS, que Operator não possui.
        with self.assertRaises(AccessDenied):
            self.facade.enqueue_job(self.operator, job_type="system_maintenance", idempotency_key="k1")

    def test_owner_can_enqueue_global_job(self):
        job = self.facade.enqueue_job(self.owner, job_type="system_maintenance", idempotency_key="k1")
        self.assertIsNone(job.channel_id)
        self.assertIsNone(job.project_id)

    def test_operator_can_enqueue_channel_scoped_job(self):
        # Job COM canal não é "global" — MANAGE_JOBS sozinha é suficiente.
        job = self.facade.enqueue_job(
            self.operator, job_type="produce_video", idempotency_key="k1", channel_id="chan_1"
        )
        self.assertEqual(job.channel_id, "chan_1")

    def test_operator_cannot_record_global_finance_entry(self):
        with self.assertRaises(AccessDenied):
            self.facade.record_finance_entry(self.operator, entry_type="expense", amount=50, channel_id=None)

    def test_owner_can_record_global_finance_entry(self):
        entry = self.facade.record_finance_entry(self.owner, entry_type="expense", amount=50, channel_id=None)
        self.assertIsNone(entry.channel_id)

    def test_operator_cannot_manage_finance_at_all(self):
        # Operator nunca recebeu MANAGE_FINANCE (least privilege já
        # existente) — negado tanto para lançamento global quanto para
        # lançamento com canal específico, pois a checagem de permissão
        # de domínio ocorre antes da checagem de operação global.
        with self.assertRaises(AccessDenied):
            self.facade.record_finance_entry(
                self.operator, entry_type="expense", amount=50, channel_id="chan_1"
            )

    def test_denied_global_attempt_is_audited_as_denied_not_success(self):
        try:
            self.facade.enqueue_job(self.operator, job_type="system_maintenance", idempotency_key="k1")
        except AccessDenied:
            pass
        events = [e for e in self.audit.all() if e.actor == "op-1" and e.action == "enqueue_job"]
        self.assertTrue(all(e.outcome == "DENIED" for e in events))
        self.assertFalse(any(e.outcome == "SUCCESS" for e in events))

    def test_job_mutation_scope_derived_from_actual_job_not_caller_claim(self):
        # Job criado SEM escopo (global, por Owner) — qualquer tentativa
        # de operá-lo depois também precisa da permissão global, mesmo
        # que o chamador não informe nada no momento da mutação (o
        # escopo é lido do job persistido, não alegado pelo chamador).
        job = self.facade.enqueue_job(self.owner, job_type="system_maintenance", idempotency_key="k1")
        with self.assertRaises(AccessDenied):
            self.facade.start_job(self.operator, job_id=job.job_id)


class AuditAfterCompletionTests(unittest.TestCase):
    """SUCCESS só é gravado depois que a operação de domínio conclui;
    falhas pós-autorização geram ERROR, nunca SUCCESS."""

    def setUp(self):
        self.facade, self.audit = make_facade()
        self.owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})

    def test_idempotency_conflict_recorded_as_error_not_success(self):
        self.facade.create_lead(self.owner, channel_id="chan_1", idempotency_key="k1", source="youtube")
        try:
            self.facade.create_lead(self.owner, channel_id="chan_1", idempotency_key="k1", source="tiktok")
        except IdempotencyConflict:
            pass

        events = [e for e in self.audit.all() if e.action == "create_lead"]
        outcomes = [e.outcome for e in events]
        self.assertIn("SUCCESS", outcomes)  # a primeira chamada
        self.assertIn("ERROR", outcomes)    # a segunda, com conflito
        # nunca um SUCCESS espúrio para a chamada que na verdade falhou
        self.assertEqual(outcomes.count("SUCCESS"), 1)

    def test_invalid_lead_transition_recorded_as_error(self):
        lead = self.facade.create_lead(self.owner, channel_id="chan_1", source="youtube")
        try:
            # NEW -> CUSTOMER direto é inválido
            self.facade.transition_lead(
                self.owner, channel_id="chan_1", lead_id=lead.lead_id, new_status=LeadStatus.CUSTOMER
            )
        except InvalidLeadTransition:
            pass

        events = [e for e in self.audit.all() if e.action == "transition_lead"]
        self.assertTrue(any(e.outcome == "ERROR" for e in events))
        self.assertFalse(any(e.outcome == "SUCCESS" for e in events))

    def test_success_only_recorded_after_real_completion(self):
        # confirma que ANTES de chamar a fachada não existe nenhum
        # evento (ou seja, nada é pré-registrado antes da execução real)
        self.assertEqual(len(self.audit.all()), 0)
        self.facade.record_sales_event(self.owner, channel_id="chan_1", event_type="click")
        events = [e for e in self.audit.all() if e.action == "record_sales_event"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].outcome, "SUCCESS")

    def test_denied_before_execution_never_reaches_domain_service(self):
        viewer = Principal(subject="viewer-1", role=Role.VIEWER, scopes={"channel:chan_1"})
        try:
            self.facade.create_lead(viewer, channel_id="chan_1", source="youtube")
        except AccessDenied:
            pass
        # nenhum lead deve ter sido criado de fato
        self.assertEqual(len(self.facade.crm.by_channel("chan_1")), 0)


if __name__ == "__main__":
    unittest.main()
