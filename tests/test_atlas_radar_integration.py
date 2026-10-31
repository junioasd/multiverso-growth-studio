import os
import tempfile
import unittest
from tests._auth_test_helpers import fast_hash_password

from api.bootstrap import build_app
from core.models import db as db_module
from atlas.director import AtlasDirector, AtlasHierarchyViolation
from atlas.escalation import ApprovalStatus
from atlas.quality_supervisor import SupervisionDecision
from atlas.radar.models import ChannelType
from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.security.audit import AuditTrail
from core.security.auth import principal_from_session
from core.security.rbac import Principal, Role

"""
Testes ATLAS+RADAR - INTEGRACAO COMPLETA (bootstrap real), QUALITY
GATES no Radar, LEARNING LOOP do radar, e TENTATIVA DE ULTRAPASSAGEM
DE AUTORIDADE cobrindo os novos recursos.
"""


class AtlasRadarBootstrapIntegrationTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def _build(self, init=True):
        return build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=init,
        )

    def test_atlas_has_radar_and_multidimensional_analyst_wired(self):
        app = self._build()
        self.assertIsNotNone(app.atlas.radar)
        self.assertIsNotNone(app.atlas.multidimensional_analyst)
        self.assertGreater(len(app.atlas.radar.sources), 20)

    def test_critical_action_request_survives_restart_via_bootstrap(self):
        app1 = self._build()
        esc = app1.atlas.request_critical_action(
            action_type="spend_money", details={"amount": 1000}, idempotency_key="req-boot-1",
        )
        self.assertEqual(esc.status, ApprovalStatus.PENDING_OWNER_APPROVAL)

        app2 = self._build(init=False)
        recovered = app2.atlas.escalation_desk.get(esc.escalation_id)
        self.assertEqual(recovered.status, ApprovalStatus.PENDING_OWNER_APPROVAL)

    def test_owner_approval_via_bootstrap_persists(self):
        app1 = self._build()
        owner_token = app1.auth.login("owner@test.com", "TestPassword123")
        owner = principal_from_session(app1.auth.current_session(owner_token))

        esc = app1.atlas.request_critical_action(action_type="publish_content", details={})
        app1.atlas.escalation_desk.approve(esc.escalation_id, decision="aprovado", resolved_by=owner)

        app2 = self._build(init=False)
        recovered = app2.atlas.escalation_desk.get(esc.escalation_id)
        self.assertEqual(recovered.status, ApprovalStatus.APPROVED)

    def test_retry_of_critical_action_after_restart_never_duplicates(self):
        app1 = self._build()
        esc1 = app1.atlas.request_critical_action(
            action_type="contract_service", details={"vendor": "x"}, idempotency_key="req-retry",
        )
        app2 = self._build(init=False)
        esc2 = app2.atlas.request_critical_action(
            action_type="contract_service", details={"vendor": "x"}, idempotency_key="req-retry",
        )
        self.assertEqual(esc1.escalation_id, esc2.escalation_id)
        with_all = app2.atlas.escalation_desk.all()
        matching = [e for e in with_all if e.escalation_id == esc1.escalation_id]
        self.assertEqual(len(matching), 1)

    def test_radar_scan_and_rank_via_bootstrap(self):
        app = self._build()
        opportunities = app.atlas.scan_opportunities(niche="fitness")
        self.assertGreater(len(opportunities), 0)
        ranked = app.atlas.rank_opportunities(opportunities)
        self.assertEqual(len(ranked), len(opportunities))

    def test_regression_full_suite_of_hardening_preserved(self):
        from core.models.entities import ValueType

        app = self._build()
        owner_token = app.auth.login("owner@test.com", "TestPassword123")
        owner = principal_from_session(app.auth.current_session(owner_token))

        e1 = app.facade.record_sales_event(
            owner, channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
            amount=100, idempotency_key="dup",
        )
        e2 = app.facade.record_sales_event(
            owner, channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
            amount=100, idempotency_key="dup",
        )
        self.assertEqual(e1.event_id, e2.event_id)

        lead = app.facade.create_lead(owner, channel_id="chan_1", source="youtube")
        self.assertIsNotNone(lead.lead_id)


def _make_atlas():
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})
    return AtlasDirector(facade=facade, principal=principal), facade, audit


class RadarQualityGateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.atlas, self.facade, self.audit = _make_atlas()

    def test_opportunity_with_no_evidence_escalates(self):
        opportunities = self.atlas.scan_opportunities(channel_types={ChannelType.YOUTUBE})
        outcome = self.atlas.supervise_opportunity(opportunities[0])
        self.assertEqual(outcome.decision, SupervisionDecision.ESCALATE)

    def test_opportunity_with_policy_incompatible_blocks(self):
        opportunities = self.atlas.scan_opportunities(channel_types={ChannelType.YOUTUBE})
        outcome = self.atlas.supervise_opportunity(opportunities[0], policy_compatible=False)
        self.assertEqual(outcome.decision, SupervisionDecision.BLOCK)

    def test_opportunity_supervision_creates_escalation_when_needed(self):
        opportunities = self.atlas.scan_opportunities(channel_types={ChannelType.YOUTUBE})
        self.atlas.supervise_opportunity(opportunities[0])
        pending = self.atlas.escalation_desk.pending()
        self.assertGreaterEqual(len(pending), 1)

    def test_opportunity_supervision_is_audited(self):
        opportunities = self.atlas.scan_opportunities(channel_types={ChannelType.YOUTUBE})
        self.atlas.supervise_opportunity(opportunities[0])
        events = [e for e in self.audit.all() if e.action == "atlas_supervise_opportunity"]
        self.assertEqual(len(events), 1)


class RadarLearningLoopTests(unittest.TestCase):
    def setUp(self):
        self.atlas, self.facade, self.audit = _make_atlas()

    def test_opportunity_outcome_recorded_in_learning_loop(self):
        opportunities = self.atlas.scan_opportunities(channel_types={ChannelType.YOUTUBE})
        record = self.atlas.record_opportunity_outcome(
            opportunity=opportunities[0], outcome="worked", context={"note": "converteu bem"},
        )
        self.assertEqual(record.outcome, "worked")

    def test_learning_never_alters_gate_verdict(self):
        opportunities = self.atlas.scan_opportunities(channel_types={ChannelType.YOUTUBE})
        for _ in range(20):
            self.atlas.record_opportunity_outcome(opportunity=opportunities[0], outcome="worked", context={})
        ranked = self.atlas.rank_opportunities(opportunities)
        self.assertEqual(ranked[0].score, "UNKNOWN")


class AttemptToExceedAuthorityWithRadarTests(unittest.TestCase):
    def setUp(self):
        self.atlas, self.facade, self.audit = _make_atlas()

    def test_radar_never_executes_publish_directly(self):
        radar_public_methods = {
            name for name in dir(self.atlas.radar)
            if not name.startswith("_") and callable(getattr(self.atlas.radar, name))
        }
        forbidden_verbs = {"publish", "spend", "contract", "connect", "buy", "pay"}
        for method in radar_public_methods:
            for verb in forbidden_verbs:
                self.assertNotIn(verb, method.lower())

    def test_ranking_result_alone_cannot_trigger_critical_action(self):
        opportunities = self.atlas.scan_opportunities()
        self.atlas.rank_opportunities(opportunities)
        self.assertEqual(len(self.atlas.escalation_desk.pending()), 0)

    def test_atlas_still_cannot_be_constructed_with_owner_role(self):
        forged = Principal(subject="atlas@system", role=Role.OWNER, scopes={"*"})
        with self.assertRaises(AtlasHierarchyViolation):
            AtlasDirector(facade=self.facade, principal=forged)


if __name__ == "__main__":
    unittest.main()
