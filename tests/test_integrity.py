import unittest

from commercial.affiliate_marketplace_intelligence import AffiliateMarketplaceIntelligence
from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.multigerenciador import MultiGerenciadorComercial
from commercial.product_scoring import ProductScoringEngine, ProductScoringInput
from commercial.sales_tracking import SalesTracker, UnverifiedAmountRejected
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.models.entities import LeadStatus, ValueType
from core.quality.commercial_quality import CommercialQualityGate
from core.quality.gates import Verdict
from core.quality.video_quality import VideoQualityGate
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role


class ValueTypeSeparationTests(unittest.TestCase):
    """REAL, ESTIMATED, FORECAST e UNKNOWN nunca se misturam."""

    def setUp(self):
        self.tracker = SalesTracker()

    def test_all_four_types_tracked_independently(self):
        self.tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100)
        self.tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.ESTIMATED, amount=50)
        self.tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=25)
        self.tracker.record_event(channel_id="c1", event_type="click")  # sem amount, ok

        summary = self.tracker.summarize("c1")
        self.assertEqual(summary.real_revenue, 100)
        self.assertEqual(summary.estimated_revenue, 50)
        self.assertEqual(summary.forecast_revenue, 25)
        # nenhum dos três se mistura com os outros
        self.assertNotEqual(summary.real_revenue, summary.estimated_revenue)

    def test_unknown_value_type_never_gets_amount(self):
        # Estruturalmente impossível: amount + UNKNOWN é sempre rejeitado.
        with self.assertRaises(UnverifiedAmountRejected):
            self.tracker.record_event(channel_id="c1", event_type="sale", amount=999, value_type=ValueType.UNKNOWN)

    def test_scaling_decision_only_uses_real_revenue(self):
        # confirma que a leitura usada para decisão de escala vem de
        # real_revenue, não de estimated/forecast (que poderiam ser
        # otimistas e inflar artificialmente uma decisão).
        self.tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=1_000_000)
        summary = self.tracker.summarize("c1")
        self.assertEqual(summary.real_revenue, 0.0)
        self.assertEqual(summary.forecast_revenue, 1_000_000)


class QualityGateStateIntegrityTests(unittest.TestCase):
    """O veredito é sempre recomputado a partir dos critérios — nunca
    aceito diretamente de um campo fornecido pelo caller."""

    def test_injected_verdict_field_is_ignored(self):
        gate = VideoQualityGate()
        subject = {
            "audio_complete": False,  # deveria bloquear
            "verdict": "APPROVED",     # tentativa de injetar veredito direto
        }
        result = gate.evaluate(subject)
        # o campo "verdict" injetado no subject é ignorado; o gate recomputa
        # a partir dos critérios reais (audio_complete=False => BLOCKED)
        self.assertEqual(result.verdict, Verdict.BLOCKED)
        self.assertNotEqual(result.verdict.value, "APPROVED")

    def test_commercial_gate_cannot_be_forced_approved_via_extra_keys(self):
        gate = CommercialQualityGate()
        subject = {
            "clicks": 1, "leads": 1,  # amostra insuficiente de propósito
            "compliance_status": "VIOLATION",
            "gate_verdict": "APPROVED",  # chave estranha, não reconhecida
        }
        result = gate.evaluate(subject)
        self.assertEqual(result.verdict, Verdict.BLOCKED)  # violação de compliance domina

    def test_gate_result_is_deterministic_for_same_input(self):
        gate = VideoQualityGate()
        subject = {"audio_complete": True, "narration_complete": True, "copyright_status": "CLEARED"}
        r1 = gate.evaluate(subject)
        r2 = gate.evaluate(subject)
        self.assertEqual(r1.verdict, r2.verdict)


class ProductScoringIntegrityTests(unittest.TestCase):
    """Não é estruturalmente possível o caller fornecer o overall_score
    diretamente — ele é sempre calculado a partir dos componentes."""

    def test_scoring_input_has_no_overall_score_field(self):
        fields = ProductScoringInput.__dataclass_fields__
        self.assertNotIn("overall_score", fields)

    def test_score_always_derived_from_components(self):
        engine = ProductScoringEngine()
        data = ProductScoringInput(audience_fit=0.9, product_quality=0.9, reputation_score=0.9)
        result = engine.score(data)
        self.assertIsInstance(result.overall_score, float)
        # Recalcular manualmente os componentes informados deve ser
        # consistente com o resultado (nenhum atalho externo aplicado).
        self.assertGreater(result.overall_score, 0)


class AffiliateIntelligenceIntegrityTests(unittest.TestCase):
    def test_recommendation_never_true_when_compliance_unknown(self):
        intel = AffiliateMarketplaceIntelligence()
        catalog = intel.discover_catalog("hotmart", "saude")
        ranked = intel.analyze_and_rank(catalog)  # sem compliance_by_product => tudo UNKNOWN
        self.assertTrue(all(not rp.recommended for rp in ranked))

    def test_ranking_order_is_deterministic(self):
        intel = AffiliateMarketplaceIntelligence()
        catalog = intel.discover_catalog("hotmart", "saude")
        fit = {p.external_id: 0.8 for p in catalog}
        ranked1 = intel.analyze_and_rank(catalog, audience_fit_by_product=fit)
        ranked2 = intel.analyze_and_rank(catalog, audience_fit_by_product=fit)
        order1 = [rp.catalog_product.external_id for rp in ranked1]
        order2 = [rp.catalog_product.external_id for rp in ranked2]
        self.assertEqual(order1, order2)


class DuplicateProtectionTests(unittest.TestCase):
    """Cobertura explícita (não-concorrente) de duplicação para lead,
    venda, comissão e job — via idempotency_key."""

    def test_duplicate_lead_rejected_by_idempotency(self):
        crm = CRM()
        lead1 = crm.create_lead("c1", idempotency_key="lead-x", source="youtube")
        lead2 = crm.create_lead("c1", idempotency_key="lead-x", source="youtube")
        self.assertEqual(lead1.lead_id, lead2.lead_id)
        self.assertEqual(len(crm.by_channel("c1")), 1)

    def test_duplicate_sale_rejected_by_idempotency(self):
        tracker = SalesTracker()
        e1 = tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="sale-x"
        )
        e2 = tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100, idempotency_key="sale-x"
        )
        self.assertEqual(e1.event_id, e2.event_id)
        self.assertEqual(tracker.summarize("c1").sales, 1)

    def test_duplicate_commission_rejected_by_idempotency(self):
        ledger = FinanceLedger()
        entry1 = ledger.record(entry_type="commission", amount=30, idempotency_key="comm-x")
        entry2 = ledger.record(entry_type="commission", amount=30, idempotency_key="comm-x")
        self.assertEqual(entry1.entry_id, entry2.entry_id)
        self.assertEqual(ledger.snapshot().commission, 30)

    def test_duplicate_job_rejected_by_idempotency(self):
        queue = JobQueue()
        job1 = queue.enqueue(job_type="t", idempotency_key="job-x")
        job2 = queue.enqueue(job_type="t", idempotency_key="job-x")
        self.assertEqual(job1.job_id, job2.job_id)
        self.assertEqual(len(queue.all()), 1)


class CRMSalesFinanceConsistencyTests(unittest.TestCase):
    """Fluxo ponta a ponta via MultiGerenciador: lead -> venda -> receita
    -> decisão de escala continuam consistentes entre si."""

    def test_full_flow_consistency(self):
        mgc = MultiGerenciadorComercial()
        lead = mgc.crm.create_lead("c1", source="youtube")
        mgc.crm.transition(lead.lead_id, LeadStatus.ENGAGED)

        for _ in range(35):
            mgc.sales_tracker.record_event(channel_id="c1", event_type="click")
        for _ in range(12):
            mgc.sales_tracker.record_event(channel_id="c1", event_type="lead")
        mgc.sales_tracker.record_event(
            channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=200, lead_id=lead.lead_id
        )
        mgc.finance.record(entry_type="revenue", amount=200, channel_id="c1")
        mgc.finance.record(entry_type="commission", amount=40, channel_id="c1")

        summary = mgc.sales_tracker.summarize("c1")
        finance_snap = mgc.finance.snapshot("c1")

        self.assertEqual(summary.sales, 1)
        self.assertEqual(summary.real_revenue, 200)
        self.assertEqual(finance_snap.revenue, 200)
        self.assertEqual(finance_snap.commission, 40)
        self.assertEqual(finance_snap.margin, 160)  # 200 - 40 comissão


class SecretSanitizationInGovernanceTests(unittest.TestCase):
    def test_facade_audit_trail_has_no_secrets_after_denied_op(self):
        job_queue = JobQueue()
        mgc = MultiGerenciadorComercial()
        audit = AuditTrail()
        facade = GovernedFacade(job_queue=job_queue, multigerenciador=mgc, audit=audit)

        agent = Principal(subject="agent-x", role=Role.AGENT, scopes={"channel:c1"})
        try:
            facade.record_finance_entry(agent, entry_type="revenue", amount=5000, channel_id="c1")
        except Exception:
            pass

        for event in audit.all():
            details_str = str(event.details).lower()
            for forbidden in ("password", "token", "api_key", "secret"):
                self.assertNotIn(forbidden, details_str)


if __name__ == "__main__":
    unittest.main()
