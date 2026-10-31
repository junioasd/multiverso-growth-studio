"""
bridge.execution.commercial_platforms
==========================================
BLOCO FINAL 2 (item 4 — INTEGRAÇÕES COMERCIAIS). Adapters REAIS para
Hotmart, Monetizze e Google (Analytics/Ads) — estrutura de endpoint
REAL e publicamente documentada:

- Hotmart: developers.hotmart.com (API de Vendas/Assinaturas)
- Monetizze: docs.monetizze.com.br (API de Vendas)
- Google Analytics Data API: developers.google.com/analytics/devguides/reporting/data/v1
- Google Ads API: developers.google.com/google-ads/api (mencionado
  para inteligência ADS — item 6, nunca usado para iniciar gasto sem
  aprovação explícita do Owner)
"""

from __future__ import annotations

from bridge.execution.platform_adapters import PlatformActionResult, PlatformApiAdapter


class HotmartAdapter(PlatformApiAdapter):
    platform = "hotmart"
    base_url = "https://developers.hotmart.com/payments/api/v1"

    def list_sales(self, *, start_date_ms: int, end_date_ms: int, max_results: int = 50) -> PlatformActionResult:
        return self._request(
            action="LIST_SALES", method="GET", path="/sales/history",
            params={"start_date": start_date_ms, "end_date": end_date_ms, "max_results": max_results},
        )

    def get_subscription_status(self, *, subscriber_code: str) -> PlatformActionResult:
        return self._request(
            action="GET_SUBSCRIPTION_STATUS", method="GET", path="/subscriptions",
            params={"subscriber_code": subscriber_code},
        )

    def get_commissions(self, *, transaction_id: str) -> PlatformActionResult:
        return self._request(
            action="GET_COMMISSIONS", method="GET", path="/sales/commissions",
            params={"transaction": transaction_id},
        )


class MonetizzeAdapter(PlatformApiAdapter):
    platform = "monetizze"
    base_url = "https://api.monetizze.com.br/2.1"

    def list_sales(self, *, data_inicial: str, data_final: str) -> PlatformActionResult:
        return self._request(
            action="LIST_SALES", method="GET", path="/venda/lista",
            params={"dataInicial": data_inicial, "dataFinal": data_final},
        )

    def get_sale_details(self, *, sale_id: str) -> PlatformActionResult:
        return self._request(action="GET_SALE_DETAILS", method="GET", path=f"/venda/{sale_id}")


class GoogleAnalyticsAdapter(PlatformApiAdapter):
    platform = "google_analytics"
    base_url = "https://analyticsdata.googleapis.com/v1beta"

    def run_report(self, *, property_id: str, metrics: list, dimensions: list, date_range_days: int = 7) -> PlatformActionResult:
        return self._request(
            action="RUN_REPORT", method="POST", path=f"/properties/{property_id}:runReport",
            body={
                "dateRanges": [{"startDate": f"{date_range_days}daysAgo", "endDate": "today"}],
                "metrics": [{"name": m} for m in metrics], "dimensions": [{"name": d} for d in dimensions],
            },
        )


class GoogleAdsAdapter(PlatformApiAdapter):
    """CORREÇÃO (item 6 — operação orgânica primeiro): este adapter
    existe para MANTER A INTELIGÊNCIA DE ADS ATIVA (estudar padrões
    vencedores em campanhas já existentes, nunca para criar/aumentar
    gasto) — `create_campaign`/`increase_budget` NÃO são implementados
    aqui de propósito; qualquer ação que iniciasse gasto real precisa
    passar primeiro pela aprovação explícita do Owner via
    `atlas.escalation.EscalationDesk`, fora do escopo deste adapter."""

    platform = "google_ads"
    base_url = "https://googleads.googleapis.com/v17"

    def get_campaign_performance(self, *, customer_id: str, campaign_id: str) -> PlatformActionResult:
        return self._request(
            action="GET_CAMPAIGN_PERFORMANCE", method="GET",
            path=f"/customers/{customer_id}/googleAds:search",
            params={"query": f"SELECT metrics.impressions, metrics.clicks, metrics.cost_micros "
                              f"FROM campaign WHERE campaign.id = {campaign_id}"},
        )
