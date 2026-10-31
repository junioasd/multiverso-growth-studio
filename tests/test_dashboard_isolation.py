import http.cookiejar
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from tests._auth_test_helpers import fast_hash_password

from api.bootstrap import build_app
from api.server import run_server
from core.governance.facade import GovernedFacade
from core.isolation.isolation import IsolationViolation
from core.models import db as db_module
from core.models.entities import ValueType
from core.security.auth import create_session_token
from core.security.rbac import AccessDenied, Principal, Role

"""
Testes de regressão — MICROCORREÇÃO 3, item 1
(ISOLAMENTO E AUTORIZAÇÃO DO PAINEL).

Falha original: /dashboard e /api/dashboard aceitavam uma sessão
restrita a um canal, mas sempre retornavam dados agregados de TODOS os
canais, ignorando o escopo da identidade autenticada.

Usa apenas sessões SINTÉTICAS (construídas localmente com
`create_session_token`/`Principal`), nunca credenciais reais.
"""


class FacadeDashboardScopeTests(unittest.TestCase):
    """Nível de fachada — sem subir servidor HTTP."""

    def setUp(self):
        self.app = build_app(persistent=False, init_database=False)
        self.owner = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
        self.app.facade.record_sales_event(
            self.owner, channel_id="chan_A", event_type="sale", value_type=ValueType.REAL, amount=100
        )
        self.app.facade.record_sales_event(
            self.owner, channel_id="chan_B", event_type="sale", value_type=ValueType.REAL, amount=500
        )

    def test_operator_scoped_to_one_channel_sees_only_that_channel(self):
        operator = Principal(subject="op-1", role=Role.OPERATOR, scopes={"channel:chan_A"})
        view = self.app.facade.get_dashboard_view(operator)
        self.assertEqual(view["sales_summary"].real_revenue, 100)
        self.assertEqual(view["channels"], {"chan_A"})

    def test_operator_scoped_to_different_channel_sees_different_data(self):
        operator = Principal(subject="op-2", role=Role.OPERATOR, scopes={"channel:chan_B"})
        view = self.app.facade.get_dashboard_view(operator)
        self.assertEqual(view["sales_summary"].real_revenue, 500)

    def test_operator_with_no_channel_scope_sees_nothing(self):
        operator = Principal(subject="op-3", role=Role.OPERATOR, scopes=set())
        view = self.app.facade.get_dashboard_view(operator)
        self.assertEqual(view["sales_summary"].real_revenue, 0)
        self.assertEqual(view["channels"], set())

    def test_operator_cannot_request_channel_outside_own_scope(self):
        operator = Principal(subject="op-4", role=Role.OPERATOR, scopes={"channel:chan_A"})
        with self.assertRaises(IsolationViolation):
            self.app.facade.get_dashboard_view(operator, requested_channel_id="chan_B")

    def test_operator_can_request_own_channel_explicitly(self):
        operator = Principal(subject="op-5", role=Role.OPERATOR, scopes={"channel:chan_A", "channel:chan_B"})
        view = self.app.facade.get_dashboard_view(operator, requested_channel_id="chan_B")
        self.assertEqual(view["sales_summary"].real_revenue, 500)
        self.assertEqual(view["channels"], {"chan_B"})

    def test_wildcard_scope_without_global_permission_denied(self):
        # VIEWER tem VIEW_DASHBOARD mas NÃO VIEW_GLOBAL_DASHBOARD — mesmo
        # com escopo coringa, o agregado global exige a permissão
        # explícita adicional.
        viewer_wildcard = Principal(subject="viewer-x", role=Role.VIEWER, scopes={"*"})
        with self.assertRaises(AccessDenied):
            self.app.facade.get_dashboard_view(viewer_wildcard)

    def test_owner_wildcard_with_global_permission_sees_everything(self):
        view = self.app.facade.get_dashboard_view(self.owner)
        self.assertEqual(view["sales_summary"].real_revenue, 600)
        self.assertIsNone(view["channels"])  # sentinela "ALL"

    def test_no_dashboard_permission_denied(self):
        agent = Principal(subject="agent-1", role=Role.AGENT, scopes={"channel:chan_A"})
        with self.assertRaises(AccessDenied):
            self.app.facade.get_dashboard_view(agent)

    def test_crm_leads_also_filtered_by_scope(self):
        self.app.facade.create_lead(self.owner, channel_id="chan_A", source="youtube")
        self.app.facade.create_lead(self.owner, channel_id="chan_B", source="tiktok")
        self.app.facade.create_lead(self.owner, channel_id="chan_B", source="tiktok")

        operator_a = Principal(subject="op-a", role=Role.OPERATOR, scopes={"channel:chan_A"})
        view_a = self.app.facade.get_dashboard_view(operator_a)
        self.assertEqual(view_a["total_leads"], 1)

        operator_b = Principal(subject="op-b", role=Role.OPERATOR, scopes={"channel:chan_B"})
        view_b = self.app.facade.get_dashboard_view(operator_b)
        self.assertEqual(view_b["total_leads"], 2)

    def test_finance_also_filtered_by_scope(self):
        self.app.facade.record_finance_entry(self.owner, entry_type="revenue", amount=50, channel_id="chan_A")
        self.app.facade.record_finance_entry(self.owner, entry_type="revenue", amount=9999, channel_id="chan_B")

        operator_a = Principal(subject="op-fa", role=Role.OPERATOR, scopes={"channel:chan_A"})
        view_a = self.app.facade.get_dashboard_view(operator_a)
        self.assertEqual(view_a["finance_snapshot"].revenue, 50)


class HttpDashboardScopeTests(unittest.TestCase):
    """Nível HTTP — usa sessões SINTÉTICAS via create_session_token (sem
    credenciais reais)."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

        self.app = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=False,
        )
        owner_token = self.app.auth.login("owner@test.com", "TestPassword123")
        from core.security.auth import principal_from_session
        owner_principal = principal_from_session(self.app.auth.current_session(owner_token))
        self.app.facade.record_sales_event(
            owner_principal, channel_id="chan_A", event_type="sale", value_type=ValueType.REAL,
            amount=100, idempotency_key="a1",
        )
        self.app.facade.record_sales_event(
            owner_principal, channel_id="chan_B", event_type="sale", value_type=ValueType.REAL,
            amount=500, idempotency_key="b1",
        )

        self.server = run_server(host="127.0.0.1", port=0, app=self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        time.sleep(0.15)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def _get(self, path: str, token: str):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        req.add_header("Cookie", f"mgs_session={token}")
        return urllib.request.urlopen(req)

    def test_synthetic_operator_session_scoped_to_channel_a(self):
        token = create_session_token(subject="synthetic-op-a", role=Role.OPERATOR.value, scopes=["channel:chan_A"])
        r = self._get("/api/dashboard", token)
        data = json.loads(r.read())["dashboard"]
        self.assertEqual(data["revenue"], 100.0)
        self.assertEqual(data["scope_label"], "chan_A")

    def test_synthetic_operator_session_scoped_to_channel_b(self):
        token = create_session_token(subject="synthetic-op-b", role=Role.OPERATOR.value, scopes=["channel:chan_B"])
        r = self._get("/api/dashboard", token)
        data = json.loads(r.read())["dashboard"]
        self.assertEqual(data["revenue"], 500.0)

    def test_synthetic_session_cannot_request_out_of_scope_channel(self):
        token = create_session_token(subject="synthetic-op-a", role=Role.OPERATOR.value, scopes=["channel:chan_A"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/dashboard?channel=chan_B", token)
        self.assertEqual(ctx.exception.code, 403)

    def test_synthetic_session_without_dashboard_permission_denied(self):
        token = create_session_token(subject="synthetic-agent", role=Role.AGENT.value, scopes=["channel:chan_A"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/dashboard", token)
        self.assertEqual(ctx.exception.code, 403)

    def test_authorized_global_access_returns_everything(self):
        owner_token = self.app.auth.login("owner@test.com", "TestPassword123")
        r = self._get("/api/dashboard", owner_token)
        data = json.loads(r.read())["dashboard"]
        self.assertEqual(data["revenue"], 600.0)
        self.assertEqual(data["scope_label"], "GLOBAL")

    def test_dashboard_html_page_respects_scope_too(self):
        token = create_session_token(subject="synthetic-op-a", role=Role.OPERATOR.value, scopes=["channel:chan_A"])
        r = self._get("/dashboard", token)
        html = r.read().decode()
        self.assertIn("chan_A", html)
        self.assertNotIn("500.0", html)  # não deve vazar o valor do outro canal


if __name__ == "__main__":
    unittest.main()
