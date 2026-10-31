"""
atlas.performance
====================
ATLAS — itens 10-12: usar dados REAIS para analisar receita, margem,
conversão, CAC, LTV, ROI, desempenho por canal/produto/campanha — nunca
inventando métricas, vendas, tendências ou resultados, e diferenciando
sempre REAL, ESTIMATED, INFERRED (FORECAST, no vocabulário já existente
do sistema) e UNKNOWN.

Este módulo NÃO calcula nada por conta própria além do que
`core.governance.facade.GovernedFacade` e `commercial.finance.FinanceLedger`
já expõem (ambos já testados e já aplicam a separação REAL/ESTIMATED/
FORECAST/UNKNOWN desde a Microcorreção 3). `PerformanceAnalyst` apenas
organiza esses números sob a ótica de um relatório do ATLAS, e é
estrutural — não textual — a respeito de quando algo é UNKNOWN: campos
sem dado suficiente ficam com o literal `"UNKNOWN"` (string), nunca com
um número inventado ou um zero que não foi de fato observado.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.governance.facade import GovernedFacade
from core.security.rbac import Principal

UNKNOWN = "UNKNOWN"


@dataclass
class PerformanceReport:
    channel_scope: str  # rótulo do escopo efetivamente consultado (nunca oculto)
    real_revenue: float
    estimated_revenue: float
    forecast_revenue: float
    sales_real: int
    sales_estimated: int
    sales_forecast: int
    refunds_real: int
    margin: float | str
    conversion_rate: float | str
    cac: float | str
    ltv: float | str
    roi: float | str
    evidence_notes: list[str] = field(default_factory=list)


class PerformanceAnalyst:
    def __init__(self, facade: GovernedFacade) -> None:
        self.facade = facade

    def analyze(
        self,
        principal: Principal,
        *,
        channel_id: str | None = None,
        marketing_spend: float | None = None,
        new_customers: int | None = None,
        avg_revenue_per_customer: float | None = None,
        avg_customer_lifespan_periods: float | None = None,
    ) -> PerformanceReport:
        """Sempre passa pela `GovernedFacade` — a mesma autorização e o
        mesmo isolamento por escopo que qualquer outro principal do
        sistema (ATLAS incluso) já respeitam. Nunca lê CRM/Sales/Finance
        diretamente por fora da fachada."""
        view = self.facade.get_dashboard_view(principal, requested_channel_id=channel_id)
        sales_summary = view["sales_summary"]
        finance_snapshot = view["finance_snapshot"]
        channels = view["channels"]
        scope_label = "GLOBAL" if channels is None else (",".join(sorted(channels)) or "NONE")

        notes: list[str] = []

        if sales_summary.clicks:
            conversion_rate: float | str = sales_summary.sales / sales_summary.clicks
        else:
            conversion_rate = UNKNOWN
            notes.append("Sem cliques registrados — conversão desconhecida (não é 0 comprovado).")

        if sales_summary.sales_estimated or sales_summary.sales_forecast:
            notes.append(
                "Existem vendas ESTIMATED/FORECAST no período — nunca somadas a real_revenue "
                "nem tratadas como resultado comprovado."
            )

        margin: float | str = finance_snapshot.margin if finance_snapshot is not None else UNKNOWN

        # CAC/LTV/ROI só são calculados quando o CHAMADOR fornece os
        # insumos reais necessários — nunca estimados/adivinhados aqui.
        # Reaproveita literalmente `FinanceLedger.cac/ltv/roi`, que já
        # retornam "UNKNOWN" quando os insumos não permitem um cálculo
        # confiável (Seção 49 / já testado).
        if marketing_spend is not None and new_customers is not None:
            cac = self.facade.finance.cac(marketing_spend=marketing_spend, new_customers=new_customers)
        else:
            cac = UNKNOWN
            notes.append("CAC não calculado — marketing_spend/new_customers não informados.")

        if avg_revenue_per_customer is not None and avg_customer_lifespan_periods is not None:
            ltv = self.facade.finance.ltv(
                avg_revenue_per_customer=avg_revenue_per_customer,
                avg_customer_lifespan_periods=avg_customer_lifespan_periods,
            )
        else:
            ltv = UNKNOWN
            notes.append("LTV não calculado — avg_revenue_per_customer/avg_customer_lifespan_periods não informados.")

        if finance_snapshot is not None and finance_snapshot.realized_cost:
            roi = self.facade.finance.roi(gain=sales_summary.real_revenue, cost=finance_snapshot.realized_cost)
        else:
            roi = UNKNOWN
            notes.append("ROI não calculado — sem custo realizado registrado no escopo.")

        return PerformanceReport(
            channel_scope=scope_label,
            real_revenue=sales_summary.real_revenue,
            estimated_revenue=sales_summary.estimated_revenue,
            forecast_revenue=sales_summary.forecast_revenue,
            sales_real=sales_summary.sales,
            sales_estimated=sales_summary.sales_estimated,
            sales_forecast=sales_summary.sales_forecast,
            refunds_real=sales_summary.refunds,
            margin=margin,
            conversion_rate=conversion_rate,
            cac=cac,
            ltv=ltv,
            roi=roi,
            evidence_notes=notes,
        )


# ---------------------------------------------------------------------------
# ATLAS+RADAR — item 2: DESEMPENHO COMERCIAL MULTIDIMENSIONAL
# ---------------------------------------------------------------------------
#
# `MultiDimensionalPerformanceAnalyst` é uma capacidade NOVA e SEPARADA
# de `PerformanceAnalyst` (mantido acima, inalterado, para preservar
# 100% de compatibilidade com o ATLAS V1 já aprovado). Permite analisar
# e cruzar produto/oferta/campanha/funil/plataforma/fonte de tráfego —
# além de canal — reaproveitando `SalesTracker.summarize_filtered()` e
# `SalesTracker.distinct_values()` (Microcorreção ATLAS+RADAR em
# `commercial/sales_tracking.py`). O isolamento por canal continua
# OBRIGATÓRIO: toda consulta multidimensional primeiro valida o escopo
# do principal via `GovernedFacade.get_dashboard_view()` (a MESMA
# autorização/auditoria de sempre) — os filtros adicionais (produto,
# campanha, etc.) NUNCA ampliam o conjunto de canais já autorizado.

DIMENSION_NAMES = ("channel_id", "product_id", "campaign_id", "funnel_id", "platform", "traffic_source")


@dataclass
class DimensionalPerformanceReport:
    dimensions: dict = field(default_factory=dict)  # ex: {"channel_id": "chan_1", "product_id": "prod_9"}
    real_revenue: float = 0.0
    estimated_revenue: float = 0.0
    forecast_revenue: float = 0.0
    sales_real: int = 0
    sales_estimated: int = 0
    sales_forecast: int = 0
    refunds_real: int = 0
    conversion_rate: float | str = UNKNOWN
    evidence_notes: list = field(default_factory=list)


class MultiDimensionalPerformanceAnalyst:
    def __init__(self, facade: GovernedFacade) -> None:
        self.facade = facade

    def _authorized_channel_filter(self, principal: Principal, channel_id: str | None):
        """Reaproveita a MESMA checagem de autorização/isolamento/auditoria
        de `GovernedFacade.get_dashboard_view()` — nunca duplica essa
        lógica. Retorna o filtro de canal já validado (None = "todos os
        canais que o principal já tem autorização de ver")."""
        view = self.facade.get_dashboard_view(principal, requested_channel_id=channel_id)
        return view["channels"]

    def analyze(
        self,
        principal: Principal,
        *,
        channel_id: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        funnel_id: str | None = None,
        platform: str | None = None,
        traffic_source: str | None = None,
    ) -> DimensionalPerformanceReport:
        channel_filter = self._authorized_channel_filter(principal, channel_id)

        summary = self.facade.sales_tracker.summarize_filtered(
            channel_id=channel_filter, product_id=product_id, campaign_id=campaign_id,
            funnel_id=funnel_id, platform=platform, traffic_source=traffic_source,
        )

        conversion_rate: float | str
        notes: list[str] = []
        if summary.clicks:
            conversion_rate = summary.sales / summary.clicks
        else:
            conversion_rate = UNKNOWN
            notes.append("Sem cliques registrados neste recorte — conversão desconhecida.")

        if summary.sales_estimated or summary.sales_forecast:
            notes.append("Existem vendas ESTIMATED/FORECAST neste recorte — nunca somadas ao valor REAL.")

        dims = {
            "channel_id": channel_id if channel_id is not None else ("GLOBAL" if channel_filter is None else ",".join(sorted(channel_filter)) or "NONE"),
            "product_id": product_id or UNKNOWN,
            "campaign_id": campaign_id or UNKNOWN,
            "funnel_id": funnel_id or UNKNOWN,
            "platform": platform or UNKNOWN,
            "traffic_source": traffic_source or UNKNOWN,
        }

        return DimensionalPerformanceReport(
            dimensions=dims,
            real_revenue=summary.real_revenue,
            estimated_revenue=summary.estimated_revenue,
            forecast_revenue=summary.forecast_revenue,
            sales_real=summary.sales,
            sales_estimated=summary.sales_estimated,
            sales_forecast=summary.sales_forecast,
            refunds_real=summary.refunds,
            conversion_rate=conversion_rate,
            evidence_notes=notes,
        )

    def breakdown(
        self, principal: Principal, *, group_by: str, channel_id: str | None = None, **fixed_filters,
    ) -> dict:
        """Cruzamento (ex: produto x canal): agrupa por `group_by`
        (uma das dimensões), aplicando os demais filtros fixos. Só
        inclui valores REALMENTE registrados (`SalesTracker.distinct_values`)
        — nunca uma lista de categorias inventada."""
        if group_by not in DIMENSION_NAMES:
            raise ValueError(f"dimensão desconhecida para cruzamento: {group_by}")

        channel_filter = self._authorized_channel_filter(principal, channel_id)
        distinct_filters = dict(fixed_filters)
        distinct_filters["channel_id"] = channel_filter if channel_id is None else channel_id

        values = self.facade.sales_tracker.distinct_values(group_by, **distinct_filters)

        report: dict[str, DimensionalPerformanceReport] = {}
        for value in values:
            kwargs = dict(fixed_filters)
            kwargs["channel_id"] = channel_id
            kwargs[group_by] = value
            report[value] = self.analyze(principal, **kwargs)
        return report
