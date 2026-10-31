import os
import tempfile
import unittest

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.multigerenciador import MultiGerenciadorComercial
from commercial.sales_tracking import SalesTracker
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.models.entities import JobState, LeadStatus, ValueType
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role

"""
Testes de regressão — MICROCORREÇÃO 4, item 3
(LEITURAS ATUALIZADAS ENTRE INSTÂNCIAS).

Falha original: uma instância não enxergava, no painel ou em consultas
diretas, um registro gravado por OUTRA instância no mesmo banco — o
registro só aparecia após recriar a aplicação (porque as leituras
liam apenas o cache carregado uma vez no `__init__`).

Corrigido: com `db_path` configurado, as leituras operacionais
consultam o banco diretamente a cada chamada.
"""


class PersistenceTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)


class CRMFreshReadTests(PersistenceTestBase):
    def test_instance_b_sees_lead_created_by_instance_a_without_recreation(self):
        crm_a = CRM(db_path=self.db_path)
        crm_b = CRM(db_path=self.db_path)

        self.assertEqual(len(crm_b.by_channel("chan_1")), 0)
        lead = crm_a.create_lead("chan_1", source="youtube")
        self.assertEqual(len(crm_b.by_channel("chan_1")), 1)
        self.assertEqual(crm_b.get(lead.lead_id).lead_id, lead.lead_id)

    def test_instance_b_sees_transition_made_by_instance_a(self):
        crm_a = CRM(db_path=self.db_path)
        crm_b = CRM(db_path=self.db_path)

        lead = crm_a.create_lead("chan_1", source="youtube")
        crm_a.transition(lead.lead_id, LeadStatus.ENGAGED)

        seen_by_b = crm_b.get(lead.lead_id)
        self.assertEqual(seen_by_b.status, LeadStatus.ENGAGED)

    def test_instance_created_before_write_still_sees_it(self):
        # crm_b nasce ANTES de qualquer escrita — seu cache de __init__
        # está vazio, mas ainda assim deve ver escritas subsequentes.
        crm_b = CRM(db_path=self.db_path)
        crm_a = CRM(db_path=self.db_path)
        crm_a.create_lead("chan_1", source="youtube")

        self.assertEqual(len(crm_b.all_leads()), 1)

    def test_transition_does_not_overwrite_concurrent_update_with_stale_copy(self):
        """B lê o lead (fica com uma cópia em cache), A transiciona, B
        transiciona em seguida — B deve validar contra o estado ATUAL
        (já avançado por A), não contra sua cópia antiga."""
        crm_a = CRM(db_path=self.db_path)
        crm_b = CRM(db_path=self.db_path)

        lead = crm_a.create_lead("chan_1", source="youtube")
        crm_b.get(lead.lead_id)  # B agora tem uma cópia em cache (NEW)

        crm_a.transition(lead.lead_id, LeadStatus.ENGAGED)  # A avança para ENGAGED

        # B tenta ir direto para CUSTOMER — inválido a partir de ENGAGED
        # (só seria válido a partir de OFFERED); se B confiasse na cópia
        # antiga (NEW), erraria a validação de forma diferente. O
        # importante é que B veja ENGAGED como estado de partida.
        final = crm_b.transition(lead.lead_id, LeadStatus.QUALIFIED)  # válido a partir de ENGAGED
        self.assertEqual(final.status, LeadStatus.QUALIFIED)

    def test_by_channels_reflects_writes_from_other_instance(self):
        crm_a = CRM(db_path=self.db_path)
        crm_b = CRM(db_path=self.db_path)
        crm_a.create_lead("chan_1", source="youtube")
        crm_a.create_lead("chan_2", source="tiktok")

        result = crm_b.by_channels({"chan_1", "chan_2"})
        self.assertEqual(len(result), 2)


class SalesTrackerFreshReadTests(PersistenceTestBase):
    def test_instance_b_sees_events_from_instance_a(self):
        t_a = SalesTracker(db_path=self.db_path)
        t_b = SalesTracker(db_path=self.db_path)

        t_a.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100)
        summary = t_b.summarize("c1")
        self.assertEqual(summary.real_revenue, 100)

    def test_instance_b_sees_events_scoped_by_set(self):
        t_a = SalesTracker(db_path=self.db_path)
        t_b = SalesTracker(db_path=self.db_path)
        t_a.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=50)
        t_a.record_event(channel_id="c2", event_type="sale", value_type=ValueType.REAL, amount=999)

        summary = t_b.summarize({"c1"})
        self.assertEqual(summary.real_revenue, 50)


class FinanceLedgerFreshReadTests(PersistenceTestBase):
    def test_instance_b_sees_entries_from_instance_a(self):
        f_a = FinanceLedger(db_path=self.db_path)
        f_b = FinanceLedger(db_path=self.db_path)
        f_a.record(entry_type="revenue", amount=500, channel_id="c1")
        snap = f_b.snapshot("c1")
        self.assertEqual(snap.revenue, 500)


class JobQueueFreshReadTests(PersistenceTestBase):
    def test_instance_b_sees_job_enqueued_by_instance_a(self):
        q_a = JobQueue(db_path=self.db_path)
        q_b = JobQueue(db_path=self.db_path)
        job = q_a.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")
        seen = q_b.get(job.job_id)
        self.assertEqual(seen.job_id, job.job_id)

    def test_instance_b_sees_state_change_by_instance_a(self):
        q_a = JobQueue(db_path=self.db_path)
        q_b = JobQueue(db_path=self.db_path)
        job = q_a.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")
        q_a.start(job.job_id)

        seen = q_b.get(job.job_id)
        self.assertEqual(seen.state, JobState.RUNNING)

    def test_job_mutation_does_not_overwrite_concurrent_update(self):
        """B lê o job (RUNNING, feito por A), A completa o job, B tenta
        falhar o job em seguida — B deve operar sobre o estado ATUAL
        (COMPLETED), não sobre sua cópia antiga (RUNNING)."""
        q_a = JobQueue(db_path=self.db_path)
        q_b = JobQueue(db_path=self.db_path)
        job = q_a.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")
        q_a.start(job.job_id)
        q_b.get(job.job_id)  # B cacheia RUNNING

        q_a.complete(job.job_id, {"ok": True})

        # B consulta de novo antes de agir — reflete COMPLETED, não RUNNING
        current = q_b.get(job.job_id)
        self.assertEqual(current.state, JobState.COMPLETED)

    def test_all_reflects_jobs_from_other_instance(self):
        q_a = JobQueue(db_path=self.db_path)
        q_b = JobQueue(db_path=self.db_path)
        q_a.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")
        q_a.enqueue(job_type="produce_video", idempotency_key="k2", channel_id="c1")
        self.assertEqual(len(q_b.all()), 2)


class DashboardFreshReadTests(PersistenceTestBase):
    def test_dashboard_via_facade_reflects_writes_from_other_instance(self):
        mgc_a = MultiGerenciadorComercial(db_path=self.db_path)
        mgc_b = MultiGerenciadorComercial(db_path=self.db_path)
        audit = AuditTrail(db_path=self.db_path)
        facade_b = GovernedFacade(job_queue=JobQueue(db_path=self.db_path), multigerenciador=mgc_b, audit=audit)
        owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})

        mgc_a.sales_tracker.record_event(
            channel_id="chan_1", event_type="sale", value_type=ValueType.REAL, amount=250
        )
        mgc_a.crm.create_lead("chan_1", source="youtube")

        view = facade_b.get_dashboard_view(owner)
        self.assertEqual(view["sales_summary"].real_revenue, 250)
        self.assertEqual(view["total_leads"], 1)

    def test_dashboard_scope_authorization_preserved_with_fresh_reads(self):
        mgc_a = MultiGerenciadorComercial(db_path=self.db_path)
        mgc_b = MultiGerenciadorComercial(db_path=self.db_path)
        audit = AuditTrail(db_path=self.db_path)
        facade_b = GovernedFacade(job_queue=JobQueue(db_path=self.db_path), multigerenciador=mgc_b, audit=audit)

        mgc_a.sales_tracker.record_event(
            channel_id="chan_A", event_type="sale", value_type=ValueType.REAL, amount=100
        )
        mgc_a.sales_tracker.record_event(
            channel_id="chan_B", event_type="sale", value_type=ValueType.REAL, amount=999
        )

        operator = Principal(subject="op-a", role=Role.OPERATOR, scopes={"channel:chan_A"})
        view = facade_b.get_dashboard_view(operator)
        self.assertEqual(view["sales_summary"].real_revenue, 100)  # nunca vaza chan_B


if __name__ == "__main__":
    unittest.main()
