import json
import unittest
from unittest.mock import MagicMock, patch

from bridge.execution.commercial_platforms import (
    GoogleAdsAdapter, GoogleAnalyticsAdapter, HotmartAdapter, MonetizzeAdapter,
)
from bridge.execution.vault import TokenType, TokenVault

"""
Testes BLOCO FINAL 2 (item 4 — integrações comerciais): estrutura
real de endpoint por adapter — sempre com rede mockada.
"""


def _fake_response(payload: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.status = 200
    response.__enter__ = lambda self: response
    response.__exit__ = lambda self, *a: None
    return response


class CommercialAdaptersTests(unittest.TestCase):
    def setUp(self):
        self.vault = TokenVault()

    def _ref(self, platform: str) -> str:
        return self.vault.store_token(platform=platform, token_type=TokenType.API_KEY, value="tok")

    def test_hotmart_list_sales_real_endpoint(self):
        adapter = HotmartAdapter(vault=self.vault, account_reference_id=self._ref("hotmart"))
        with patch("urllib.request.urlopen", return_value=_fake_response({"items": []})) as mock_urlopen:
            result = adapter.list_sales(start_date_ms=1000, end_date_ms=2000)
            url = mock_urlopen.call_args[0][0].full_url
        self.assertTrue(result.success)
        self.assertIn("developers.hotmart.com", url)
        self.assertIn("sales/history", url)

    def test_hotmart_without_credential_unavailable(self):
        adapter = HotmartAdapter(account_reference_id="sem_credencial")
        result = adapter.get_commissions(transaction_id="t1")
        self.assertFalse(result.success)

    def test_monetizze_list_sales_real_endpoint(self):
        adapter = MonetizzeAdapter(vault=self.vault, account_reference_id=self._ref("monetizze"))
        with patch("urllib.request.urlopen", return_value=_fake_response({"vendas": []})) as mock_urlopen:
            result = adapter.list_sales(data_inicial="2024-01-01", data_final="2024-01-31")
            url = mock_urlopen.call_args[0][0].full_url
        self.assertTrue(result.success)
        self.assertIn("api.monetizze.com.br", url)

    def test_google_analytics_run_report_real_endpoint(self):
        adapter = GoogleAnalyticsAdapter(vault=self.vault, account_reference_id=self._ref("google_analytics"))
        with patch("urllib.request.urlopen", return_value=_fake_response({"rows": []})) as mock_urlopen:
            result = adapter.run_report(property_id="123", metrics=["sessions"], dimensions=["date"])
            request = mock_urlopen.call_args[0][0]
        self.assertTrue(result.success)
        self.assertIn("analyticsdata.googleapis.com", request.full_url)
        self.assertIn("properties/123:runReport", request.full_url)

    def test_google_ads_never_creates_campaign_or_spend(self):
        """CORREÇÃO (item 6 — operação orgânica primeiro): o adapter
        de Google Ads nunca expõe um método que crie campanha ou
        aumente orçamento — apenas leitura de performance para manter
        a inteligência de ADS ativa sem nunca iniciar gasto."""
        adapter = GoogleAdsAdapter(vault=self.vault, account_reference_id=self._ref("google_ads"))
        self.assertFalse(hasattr(adapter, "create_campaign"))
        self.assertFalse(hasattr(adapter, "increase_budget"))
        self.assertFalse(hasattr(adapter, "set_budget"))

    def test_google_ads_read_only_performance_query(self):
        adapter = GoogleAdsAdapter(vault=self.vault, account_reference_id=self._ref("google_ads"))
        with patch("urllib.request.urlopen", return_value=_fake_response({"results": []})) as mock_urlopen:
            result = adapter.get_campaign_performance(customer_id="c1", campaign_id="camp1")
            request = mock_urlopen.call_args[0][0]
        self.assertTrue(result.success)
        self.assertEqual(request.get_method(), "GET")


if __name__ == "__main__":
    unittest.main()
