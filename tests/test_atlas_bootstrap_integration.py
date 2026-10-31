import os
import tempfile
import unittest
from tests._auth_test_helpers import fast_hash_password

from api.bootstrap import build_app
from core.models import db as db_module
from atlas.quality_supervisor import SupervisionDecision
from commercial.funnel_engine import FunnelSelectionInput
from core.models.entities import ValueType
from core.security.rbac import Role

"""
Testes ATLAS - INTEGRACAO COMPLETA via api.bootstrap.build_app.
"""


class AtlasBootstrapIntegrationTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def test_atlas_wired_correctly_in_app(self):
        app = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=True,
        )
        self.assertEqual(app.atlas.principal.role, Role.ATLAS)
        self.assertIs(app.atlas.facade, app.facade)

    def test_atlas_analysis_reflects_persisted_data_across_restart(self):
        app1 = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=True,
        )
        owner_token = app1.auth.login("owner@test.com", "TestPassword123")
        from core.security.auth import principal_from_session
        owner_principal = principal_from_session(app1.auth.current_session(owner_token))

        app1.facade.record_sales_event(
            owner_principal, channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
            amount=300, idempotency_key="s1",
        )

        app2 = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=False,
        )
        report = app2.atlas.analyze_performance(channel_id="chan_1")
        self.assertEqual(report.real_revenue, 300)

    def test_atlas_plan_and_opportunity_flow_end_to_end(self):
        app = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=True,
        )
        owner_token = app.auth.login("owner@test.com", "TestPassword123")
        from core.security.auth import principal_from_session
        owner_principal = principal_from_session(app.auth.current_session(owner_token))

        plan = app.atlas.receive_objective(
            owner_principal, objective="Testar novo funil de afiliados", channel_id="chan_1"
        )
        self.assertGreater(len(plan.steps), 0)

        assessment = app.atlas.evaluate_opportunities(
            channel_id="chan_1", platform="youtube", affiliate_platform="hotmart",
            niche="saude", funnel_request=FunnelSelectionInput(platform="youtube"),
        )
        self.assertGreater(len(assessment.opportunities), 0)

    def test_atlas_never_bypasses_idempotency_hardening(self):
        app = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=True,
        )
        owner_token = app.auth.login("owner@test.com", "TestPassword123")
        from core.security.auth import principal_from_session
        owner_principal = principal_from_session(app.auth.current_session(owner_token))

        e1 = app.facade.record_sales_event(
            owner_principal, channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
            amount=50, idempotency_key="dup",
        )
        e2 = app.facade.record_sales_event(
            owner_principal, channel_id="chan_1", event_type="sale", value_type=ValueType.REAL,
            amount=50, idempotency_key="dup",
        )
        self.assertEqual(e1.event_id, e2.event_id)
        report = app.atlas.analyze_performance(channel_id="chan_1")
        self.assertEqual(report.real_revenue, 50)

    def test_full_suite_of_atlas_supervision_via_bootstrap(self):
        app = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=True,
        )
        outcome = app.atlas.supervise_video({"copyright_status": "UNKNOWN"})
        self.assertEqual(outcome.decision, SupervisionDecision.BLOCK)

        events = [e for e in app.audit.all() if e.action == "atlas_supervise_video"]
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
