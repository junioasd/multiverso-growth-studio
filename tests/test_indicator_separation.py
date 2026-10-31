import http.cookiejar
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from tests._auth_test_helpers import fast_hash_password

from api.bootstrap import build_app
from api.server import run_server
from commercial.sales_tracking import SalesTracker
from core.models import db as db_module
from core.models.entities import ValueType

"""
Testes de regressão — MICROCORREÇÃO 3, item 4 (SEPARAÇÃO DOS INDICADORES).

Falha original: uma venda FORECAST aparecia contada junto com vendas
REAIS no painel (`summary.sales` misturava todos os tipos de evidência).

Testa cenários somente-previstos, somente-estimados, desconhecidos,
reais e mistos — no nível do `SalesTracker` e também no HTML/JSON do
painel servido via HTTP.
"""


class SalesSummarySeparationTests(unittest.TestCase):
    def test_forecast_only_never_counted_as_real_sale(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=5000)
        summary = tracker.summarize("c1")
        self.assertEqual(summary.sales, 0)  # .sales é SOMENTE real
        self.assertEqual(summary.sales_forecast, 1)
        self.assertEqual(summary.real_revenue, 0)
        self.assertEqual(summary.forecast_revenue, 5000)

    def test_estimated_only_never_counted_as_real_sale(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.ESTIMATED, amount=800)
        summary = tracker.summarize("c1")
        self.assertEqual(summary.sales, 0)
        self.assertEqual(summary.sales_estimated, 1)
        self.assertEqual(summary.estimated_revenue, 800)

    def test_unknown_sale_without_amount_tracked_separately(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.UNKNOWN)
        summary = tracker.summarize("c1")
        self.assertEqual(summary.sales, 0)
        self.assertEqual(summary.sales_unknown, 1)

    def test_real_only_counted_normally(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=200)
        summary = tracker.summarize("c1")
        self.assertEqual(summary.sales, 1)
        self.assertEqual(summary.real_revenue, 200)

    def test_mixed_scenario_all_four_types_kept_fully_separate(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100)
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.ESTIMATED, amount=200)
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=300)
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.UNKNOWN)

        summary = tracker.summarize("c1")
        self.assertEqual(summary.sales, 1)               # só a REAL
        self.assertEqual(summary.sales_estimated, 1)
        self.assertEqual(summary.sales_forecast, 1)
        self.assertEqual(summary.sales_unknown, 1)
        self.assertEqual(summary.real_revenue, 100)
        self.assertEqual(summary.estimated_revenue, 200)
        self.assertEqual(summary.forecast_revenue, 300)

    def test_refunds_also_separated_by_evidence_type(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="refund", value_type=ValueType.REAL, amount=10)
        tracker.record_event(channel_id="c1", event_type="refund", value_type=ValueType.FORECAST, amount=9999)
        summary = tracker.summarize("c1")
        self.assertEqual(summary.refunds, 1)  # só REAL
        self.assertEqual(summary.refunds_forecast, 1)

    def test_real_sale_and_real_refund_still_nets_to_zero(self):
        # preserva o cálculo já corrigido na rodada anterior
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100)
        tracker.record_event(channel_id="c1", event_type="refund", value_type=ValueType.REAL, amount=100)
        summary = tracker.summarize("c1")
        self.assertEqual(summary.real_revenue, 0)

    def test_forecast_sale_never_affects_real_net_revenue(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100)
        tracker.record_event(channel_id="c1", event_type="refund", value_type=ValueType.REAL, amount=100)
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=999999)
        summary = tracker.summarize("c1")
        self.assertEqual(summary.real_revenue, 0)  # forecast não contamina o real


class DashboardIndicatorSeparationHttpTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

        self.app = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=False,
        )
        self.server = run_server(host="127.0.0.1", port=0, app=self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        time.sleep(0.15)
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/login",
            data=json.dumps({"email": "owner@test.com", "password": "TestPassword123"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.opener.open(req)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
        )
        return self.opener.open(req)

    def test_json_dashboard_separates_all_four_types(self):
        self._post("/api/sales-events", {
            "channel_id": "c1", "event_type": "sale", "value_type": "REAL", "amount": 100, "idempotency_key": "r1",
        })
        self._post("/api/sales-events", {
            "channel_id": "c1", "event_type": "sale", "value_type": "ESTIMATED", "amount": 200, "idempotency_key": "e1",
        })
        self._post("/api/sales-events", {
            "channel_id": "c1", "event_type": "sale", "value_type": "FORECAST", "amount": 300, "idempotency_key": "f1",
        })

        r = self.opener.open(f"http://127.0.0.1:{self.port}/api/dashboard")
        data = json.loads(r.read())["dashboard"]

        self.assertEqual(data["sales"], 1)              # só REAL
        self.assertEqual(data["sales_estimated"], 1)
        self.assertEqual(data["sales_forecast"], 1)
        self.assertEqual(data["revenue"], 100.0)
        self.assertEqual(data["revenue_estimated"], 200.0)
        self.assertEqual(data["revenue_forecast"], 300.0)

    def test_html_dashboard_shows_forecast_separately_labeled(self):
        self._post("/api/sales-events", {
            "channel_id": "c1", "event_type": "sale", "value_type": "FORECAST", "amount": 5000, "idempotency_key": "f1",
        })
        r = self.opener.open(f"http://127.0.0.1:{self.port}/dashboard")
        html = r.read().decode()
        self.assertIn("PREVISTO", html.upper())
        self.assertIn("5000", html)
        # a linha de "Vendas (REAL)" deve mostrar 0, não a venda forecast
        self.assertIn("Vendas (REAL)</td><td>0<", html)

    def test_html_war_room_never_presents_forecast_as_real_revenue_row(self):
        self._post("/api/sales-events", {
            "channel_id": "c1", "event_type": "sale", "value_type": "FORECAST", "amount": 7777, "idempotency_key": "f1",
        })
        r = self.opener.open(f"http://127.0.0.1:{self.port}/dashboard")
        html = r.read().decode()
        self.assertIn("Receita PREVISTA/FORECAST", html)
        self.assertIn("receita acumulada (REAL", html)


if __name__ == "__main__":
    unittest.main()
