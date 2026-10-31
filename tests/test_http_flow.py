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
from core.models import db as db_module

"""
Testes de regressão — MICROCORREÇÃO 2, item 6 (ACESSO OWNER E PAINEL).

Sobe um servidor HTTP real (stdlib) em uma porta efêmera para cada
teste e valida o fluxo ponta a ponta: /login, /dashboard, mutações
autenticadas via API, e rejeição de mutações sem sessão.
"""


class HttpFlowTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

        self.app = build_app(
            owner_email="owner@test.com",
            owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path,
            init_database=False,
        )
        self.server = run_server(host="127.0.0.1", port=0, app=self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.15)

        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _post_json(self, path: str, payload: dict, opener=None):
        opener = opener or self.opener
        req = urllib.request.Request(
            self._url(path), data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
        )
        return opener.open(req)

    def _login(self):
        return self._post_json("/api/login", {"email": "owner@test.com", "password": "TestPassword123"})


class LoginPageTests(HttpFlowTestCase):
    def test_login_page_returns_html_form(self):
        r = self.opener.open(self._url("/login"))
        html = r.read().decode()
        self.assertEqual(r.status, 200)
        self.assertIn("<form", html)
        self.assertIn("password", html)

    def test_dashboard_without_session_redirects_to_login(self):
        r = self.opener.open(self._url("/dashboard"))
        self.assertTrue(r.url.endswith("/login"))

    def test_login_with_wrong_password_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json("/api/login", {"email": "owner@test.com", "password": "wrong"})
        self.assertEqual(ctx.exception.code, 401)

    def test_login_with_correct_password_sets_session_and_redirect(self):
        r = self._login()
        data = json.loads(r.read())
        self.assertEqual(data["redirect"], "/dashboard")
        # cookie de sessão deve estar presente
        cookie_names = [c.name for c in self.cj]
        self.assertIn("mgs_session", cookie_names)


class DashboardPageTests(HttpFlowTestCase):
    def test_dashboard_accessible_after_login(self):
        self._login()
        r = self.opener.open(self._url("/dashboard"))
        html = r.read().decode()
        self.assertEqual(r.status, 200)
        self.assertIn("Dashboard", html)
        self.assertIn("Owner War Room", html)

    def test_dashboard_shows_unknown_style_indicator_when_no_data(self):
        self._login()
        r = self.opener.open(self._url("/api/dashboard"))
        data = json.loads(r.read())["dashboard"]
        # sem nenhuma mutação ainda, quality_score continua UNKNOWN
        self.assertEqual(data["quality_score"], "UNKNOWN")

    def test_dashboard_reflects_real_persisted_leads_and_revenue(self):
        self._login()
        self._post_json("/api/leads", {"channel_id": "chan_1", "source": "youtube", "idempotency_key": "lead-1"})
        self._post_json("/api/sales-events", {
            "channel_id": "chan_1", "event_type": "sale", "value_type": "REAL",
            "amount": 100, "idempotency_key": "sale-1",
        })
        r = self.opener.open(self._url("/api/dashboard"))
        data = json.loads(r.read())["dashboard"]
        self.assertEqual(data["leads"], 1)
        self.assertEqual(data["revenue"], 100.0)

    def test_dashboard_net_revenue_after_full_refund_is_zero(self):
        self._login()
        self._post_json("/api/sales-events", {
            "channel_id": "chan_1", "event_type": "sale", "value_type": "REAL",
            "amount": 100, "idempotency_key": "sale-1",
        })
        self._post_json("/api/sales-events", {
            "channel_id": "chan_1", "event_type": "refund", "value_type": "REAL",
            "amount": 100, "idempotency_key": "refund-1",
        })
        r = self.opener.open(self._url("/api/dashboard"))
        data = json.loads(r.read())["dashboard"]
        self.assertEqual(data["revenue"], 0.0)


class AuthenticatedMutationTests(HttpFlowTestCase):
    def test_create_lead_requires_authentication(self):
        unauth_opener = urllib.request.build_opener()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json("/api/leads", {"channel_id": "chan_1", "source": "x"}, opener=unauth_opener)
        self.assertEqual(ctx.exception.code, 401)

    def test_record_sales_event_requires_authentication(self):
        unauth_opener = urllib.request.build_opener()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json(
                "/api/sales-events", {"channel_id": "chan_1", "event_type": "click"}, opener=unauth_opener
            )
        self.assertEqual(ctx.exception.code, 401)

    def test_enqueue_job_requires_authentication(self):
        unauth_opener = urllib.request.build_opener()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json(
                "/api/jobs", {"job_type": "produce_video", "idempotency_key": "j1", "channel_id": "chan_1"},
                opener=unauth_opener,
            )
        self.assertEqual(ctx.exception.code, 401)

    def test_authenticated_lead_creation_succeeds(self):
        self._login()
        r = self._post_json("/api/leads", {"channel_id": "chan_1", "source": "youtube"})
        self.assertEqual(r.status, 201)
        data = json.loads(r.read())
        self.assertIn("lead_id", data)

    def test_authenticated_job_enqueue_succeeds(self):
        self._login()
        r = self._post_json(
            "/api/jobs", {"job_type": "produce_video", "idempotency_key": "j1", "channel_id": "chan_1"}
        )
        self.assertEqual(r.status, 201)

    def test_duplicate_idempotency_key_returns_conflict(self):
        self._login()
        self._post_json("/api/leads", {"channel_id": "chan_1", "source": "youtube", "idempotency_key": "lead-x"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json(
                "/api/leads", {"channel_id": "chan_1", "source": "tiktok", "idempotency_key": "lead-x"}
            )
        self.assertEqual(ctx.exception.code, 409)

    def test_logout_revokes_access_to_mutations(self):
        self._login()
        self._post_json("/api/leads", {"channel_id": "chan_1", "source": "youtube", "idempotency_key": "l1"})

        req = urllib.request.Request(self._url("/api/logout"), data=b"{}", headers={"Content-Type": "application/json"})
        self.opener.open(req)

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json("/api/leads", {"channel_id": "chan_1", "source": "youtube", "idempotency_key": "l2"})
        self.assertEqual(ctx.exception.code, 401)


class PersistenceAcrossServerRestartTests(unittest.TestCase):
    """O servidor HTTP em si é só transporte — a persistência real é do
    banco. Confirma que dados criados via HTTP sobrevivem quando o app é
    reconstruído (novo processo simulado) sobre o mesmo db_path."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def test_http_created_lead_survives_app_restart(self):
        app1 = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=False,
        )
        server1 = run_server(host="127.0.0.1", port=0, app=app1)
        port = server1.server_address[1]
        thread = threading.Thread(target=server1.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)

        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/login",
            data=json.dumps({"email": "owner@test.com", "password": "TestPassword123"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        opener.open(req)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/leads",
            data=json.dumps({"channel_id": "chan_1", "source": "youtube", "idempotency_key": "l1"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        opener.open(req)

        server1.shutdown()
        server1.server_close()

        # "reinício": novo app, mesmo db_path, nenhuma referência ao anterior
        app2 = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=False,
        )
        self.assertEqual(len(app2.multigerenciador.crm.by_channel("chan_1")), 1)


if __name__ == "__main__":
    unittest.main()
