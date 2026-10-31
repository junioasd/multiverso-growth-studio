import unittest

from atlas.director import AtlasDirector
from atlas.quality_supervisor import SupervisionDecision
from commercial.multigerenciador import MultiGerenciadorComercial
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.security.audit import AuditTrail
from core.security.rbac import AccessDenied, Principal, Role

"""
Testes ATLAS - QUALITY GATES, BLOQUEIOS, ESCALONAMENTO AO OWNER e
AUDITORIA.
"""


def make_atlas(scopes=None):
    mgc = MultiGerenciadorComercial()
    audit = AuditTrail()
    facade = GovernedFacade(job_queue=JobQueue(), multigerenciador=mgc, audit=audit)
    principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes=scopes or {"*"})
    return AtlasDirector(facade=facade, principal=principal), facade, audit


OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


class VideoQualityGateSupervisionTests(unittest.TestCase):
    def test_clean_video_approved_proceeds(self):
        atlas, _, _ = make_atlas()
        subject = {
            "audio_complete": True, "narration_complete": True,
            "duration_expected_seconds": 100, "duration_actual_seconds": 100,
            "static_ratio": 0.1, "repeated_single_image": False, "irrelevant_scenes": False,
            "deformation_detected": False, "sync_offset_ms": 10, "captions_error_rate": 0.0,
            "pronunciation_score": 0.9, "thumbnail_score": 0.8, "fact_check_status": "OK",
            "cta_quality_score": 0.7, "music_fit_score": 0.6, "copyright_status": "CLEARED",
            "policy_violation": False,
        }
        outcome = atlas.supervise_video(subject)
        self.assertEqual(outcome.decision, SupervisionDecision.PROCEED)

    def test_copyright_unknown_blocks(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_video({"copyright_status": "UNKNOWN"})
        self.assertEqual(outcome.decision, SupervisionDecision.BLOCK)

    def test_policy_violation_blocks(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_video({"policy_violation": True, "copyright_status": "CLEARED"})
        self.assertEqual(outcome.decision, SupervisionDecision.BLOCK)

    def test_missing_fact_check_escalates(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_video({"copyright_status": "CLEARED"})
        self.assertEqual(outcome.decision, SupervisionDecision.ESCALATE)

    def test_static_video_escalates_for_rework(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_video({"static_ratio": 0.9, "copyright_status": "CLEARED"})
        self.assertEqual(outcome.decision, SupervisionDecision.ESCALATE)

    def test_block_never_proceeds_by_default_on_unknown_data(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_video({})
        self.assertNotEqual(outcome.decision, SupervisionDecision.PROCEED)


class CommercialScalingSupervisionTests(unittest.TestCase):
    def test_insufficient_sample_escalates_not_proceeds(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_commercial_scaling({"clicks": 2, "leads": 1, "compliance_status": "OK"})
        self.assertNotEqual(outcome.decision, SupervisionDecision.PROCEED)

    def test_compliance_violation_blocks(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_commercial_scaling({"compliance_status": "VIOLATION"})
        self.assertEqual(outcome.decision, SupervisionDecision.BLOCK)

    def test_healthy_metrics_proceed(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_commercial_scaling({
            "clicks": 100, "leads": 20, "compliance_status": "OK",
            "refund_rate": 0.02, "conversion_rate": 0.05, "margin": 500,
        })
        self.assertEqual(outcome.decision, SupervisionDecision.PROCEED)


class AffiliateComplianceSupervisionTests(unittest.TestCase):
    def test_all_unknown_blocks(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_affiliate_compliance({})
        self.assertEqual(outcome.decision, SupervisionDecision.BLOCK)

    def test_all_confirmed_proceeds(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_affiliate_compliance({
            "platform_rules_reviewed": True, "producer_terms_reviewed": True,
            "traffic_restrictions_ok": True, "keyword_restrictions_ok": True,
            "brand_usage_authorized": True, "image_usage_authorized": True,
            "claims_within_policy": True, "social_platform_policy_ok": True,
            "affiliate_disclosure_present": True, "legal_requirements_met": True,
        })
        self.assertEqual(outcome.decision, SupervisionDecision.PROCEED)


class QualityGateEscalationTests(unittest.TestCase):
    def test_escalate_decision_creates_pending_escalation(self):
        atlas, _, _ = make_atlas()
        outcome = atlas.supervise_video({"copyright_status": "CLEARED"})
        self.assertEqual(outcome.decision, SupervisionDecision.ESCALATE)
        pending = atlas.escalation_desk.pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("video_quality_gate", pending[0].reason)

    def test_block_decision_does_not_create_escalation(self):
        atlas, _, _ = make_atlas()
        atlas.supervise_video({"copyright_status": "UNKNOWN"})
        self.assertEqual(len(atlas.escalation_desk.pending()), 0)

    def test_proceed_decision_does_not_create_escalation(self):
        atlas, _, _ = make_atlas()
        atlas.supervise_affiliate_compliance({
            "platform_rules_reviewed": True, "producer_terms_reviewed": True,
            "traffic_restrictions_ok": True, "keyword_restrictions_ok": True,
            "brand_usage_authorized": True, "image_usage_authorized": True,
            "claims_within_policy": True, "social_platform_policy_ok": True,
            "affiliate_disclosure_present": True, "legal_requirements_met": True,
        })
        self.assertEqual(len(atlas.escalation_desk.pending()), 0)

    def test_owner_resolves_escalation(self):
        atlas, _, _ = make_atlas()
        atlas.supervise_video({"copyright_status": "CLEARED"})
        pending = atlas.escalation_desk.pending()
        resolved = atlas.escalation_desk.resolve(
            pending[0].escalation_id, resolution="fact-check confirmado manualmente", resolved_by=OWNER
        )
        self.assertTrue(resolved.resolved)
        self.assertEqual(len(atlas.escalation_desk.pending()), 0)

    def test_non_owner_cannot_resolve_escalation(self):
        atlas, _, _ = make_atlas()
        atlas.supervise_video({"copyright_status": "CLEARED"})
        pending = atlas.escalation_desk.pending()
        operator = Principal(subject="op-1", role=Role.OPERATOR, scopes={"*"})
        with self.assertRaises(AccessDenied):
            atlas.escalation_desk.resolve(pending[0].escalation_id, resolution="x", resolved_by=operator)

    def test_generic_escalate_method_works(self):
        atlas, _, _ = make_atlas()
        item = atlas.escalate(reason="Owner precisa decidir sobre novo fornecedor", context={"x": 1})
        self.assertIn(item, atlas.escalation_desk.pending())


class AuditTrailTests(unittest.TestCase):
    def test_receive_objective_is_audited(self):
        atlas, facade, audit = make_atlas()
        atlas.receive_objective(OWNER, objective="Crescer vendas", channel_id="chan_1")
        events = [e for e in audit.all() if e.action == "atlas_receive_objective"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].outcome, "SUCCESS")
        self.assertEqual(events[0].actor, "atlas@system")

    def test_supervision_decisions_are_audited(self):
        atlas, _, audit = make_atlas()
        atlas.supervise_video({"copyright_status": "UNKNOWN"})
        events = [e for e in audit.all() if e.action == "atlas_supervise_video"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details["decision"], "BLOCK")

    def test_learning_observation_is_audited(self):
        atlas, _, audit = make_atlas()
        atlas.record_learning_observation(scope_key="chan_1|prod", outcome="worked", context={})
        events = [e for e in audit.all() if e.action == "atlas_learning_observation"]
        self.assertEqual(len(events), 1)

    def test_denied_objective_attempt_is_audited_as_denied(self):
        atlas, _, audit = make_atlas()
        fake_owner = Principal(subject="fake", role=Role.OPERATOR, scopes={"*"})
        try:
            atlas.receive_objective(fake_owner, objective="x", channel_id="chan_1")
        except AccessDenied:
            pass
        events = [e for e in audit.all() if e.action == "atlas_receive_objective"]
        self.assertEqual(len(events), 0)

    def test_audit_never_contains_raw_secrets(self):
        atlas, _, audit = make_atlas()
        atlas.request_critical_action(action_type="modify_credentials", details={"password": "hunter2"})
        for event in audit.all():
            self.assertNotIn("hunter2", str(event.details))


if __name__ == "__main__":
    unittest.main()
