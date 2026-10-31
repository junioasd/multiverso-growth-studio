"""
api.dashboard
==============
Performance Dashboard (Seção 63) e Owner War Room (Seção 64). Somente
dados existentes — ausentes são reportados como UNKNOWN. Sem métricas
inventadas.

MICROCORREÇÃO 3 (auditoria):
- item 1: o snapshot é sempre construído a partir de dados JÁ
  filtrados pelo escopo da identidade autenticada (a filtragem em si
  acontece em `core.governance.facade.GovernedFacade.get_dashboard_view`,
  não aqui — este módulo só formata o que recebe).
- item 4: `sales`/`revenue` (campos "genéricos") representam SOMENTE
  evidência REAL. Vendas/reembolsos ESTIMATED e FORECAST aparecem em
  campos separados e claramente rotulados — nunca somados aos reais.
"""

from __future__ import annotations

from dataclasses import dataclass, field

UNKNOWN = "UNKNOWN"


@dataclass
class DashboardSnapshot:
    content_produced: int | str = UNKNOWN
    content_published: int | str = UNKNOWN
    quality_score: float | str = UNKNOWN
    views: int | str = UNKNOWN
    watch_time_seconds: float | str = UNKNOWN
    retention: float | str = UNKNOWN
    ctr: float | str = UNKNOWN
    leads: int | str = UNKNOWN

    # --- Indicadores SEPARADOS por tipo de evidência (item 4) ---
    # "sales"/"revenue" (sem sufixo) = SOMENTE REAL, nunca inclui
    # previsão/estimativa. Isso é o resultado comprovado.
    sales: int | str = UNKNOWN
    sales_estimated: int | str = UNKNOWN
    sales_forecast: int | str = UNKNOWN
    sales_unknown: int | str = UNKNOWN
    refunds: int | str = UNKNOWN
    refunds_estimated: int | str = UNKNOWN
    refunds_forecast: int | str = UNKNOWN
    revenue: float | str = UNKNOWN               # líquido, REAL
    revenue_estimated: float | str = UNKNOWN      # líquido, ESTIMATED
    revenue_forecast: float | str = UNKNOWN       # líquido, FORECAST

    commission: float | str = UNKNOWN
    cost: float | str = UNKNOWN
    margin: float | str = UNKNOWN
    top_channels: list[str] = field(default_factory=list)
    top_products: list[str] = field(default_factory=list)
    top_offers: list[str] = field(default_factory=list)
    top_funnels: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scope_label: str = UNKNOWN  # "GLOBAL" | "chan_1,chan_2" | canal único — nunca oculto


class PerformanceDashboard:
    """Monta um snapshot a partir de fontes já filtradas por escopo
    (ver `GovernedFacade.get_dashboard_view`). Nunca preenche um campo
    sem dado — usa UNKNOWN. Nunca mistura REAL com ESTIMATED/FORECAST
    nos campos "genéricos" (sales/revenue)."""

    def build_snapshot(
        self,
        *,
        sales_summary=None,
        finance_snapshot=None,
        total_leads: int | None = None,
        quality_scores: list[float] | None = None,
        top_channels: list[str] | None = None,
        top_products: list[str] | None = None,
        top_offers: list[str] | None = None,
        top_funnels: list[str] | None = None,
        warnings: list[str] | None = None,
        scope_label: str | None = None,
    ) -> DashboardSnapshot:
        snap = DashboardSnapshot()

        if sales_summary is not None:
            snap.views = sales_summary.views
            snap.sales = sales_summary.sales  # já é REAL-only (SalesSummary.sales)
            snap.sales_estimated = sales_summary.sales_estimated
            snap.sales_forecast = sales_summary.sales_forecast
            snap.sales_unknown = sales_summary.sales_unknown
            snap.refunds = sales_summary.refunds  # REAL-only
            snap.refunds_estimated = sales_summary.refunds_estimated
            snap.refunds_forecast = sales_summary.refunds_forecast
            snap.revenue = sales_summary.real_revenue
            snap.revenue_estimated = sales_summary.estimated_revenue
            snap.revenue_forecast = sales_summary.forecast_revenue

        if total_leads is not None:
            snap.leads = total_leads

        if finance_snapshot is not None:
            snap.cost = finance_snapshot.realized_cost
            snap.margin = finance_snapshot.margin
            snap.commission = finance_snapshot.commission

        if quality_scores:
            snap.quality_score = round(sum(quality_scores) / len(quality_scores), 3)

        snap.top_channels = top_channels or []
        snap.top_products = top_products or []
        snap.top_offers = top_offers or []
        snap.top_funnels = top_funnels or []
        snap.warnings = warnings or []
        if scope_label is not None:
            snap.scope_label = scope_label

        return snap


@dataclass
class WarRoomAnswer:
    question: str
    answer: str
    evidence_type: str  # "REAL" | "ESTIMATED" | "FORECAST" | "UNKNOWN" | "N/A"
    based_on_real_data: bool  # SEMPRE == (evidence_type == "REAL") — nunca inferido de outra forma


class OwnerWarRoom:
    """Responde às perguntas da Seção 64 usando somente dados
    fornecidos — sem inventar métricas.

    MICROCORREÇÃO 4 (auditoria — item 4, CLASSIFICAÇÃO DE EVIDÊNCIA):
    Falha original: `based_on_real_data` era derivado apenas de "o valor
    é diferente de UNKNOWN?" — uma receita FORECAST, sendo um número
    presente e não-UNKNOWN, era marcada como `based_on_real_data=True`,
    mesmo o texto dizendo corretamente "previsão".

    Agora cada resposta recebe um `evidence_type` EXPLÍCITO, escolhido
    pelo chamador de `add()` (nunca inferido da presença/ausência do
    valor) — e `based_on_real_data` é SEMPRE `evidence_type == "REAL"`,
    nunca calculado de outro jeito. A existência de um registro
    persistido não comprova por si só que o conteúdo é um resultado
    real: é o `value_type` de origem (REAL/ESTIMATED/FORECAST/UNKNOWN)
    que decide o `evidence_type` da resposta, não a mera presença do
    valor. Um valor 0 (zero efetivamente observado) continua distinto
    de UNKNOWN (ausência de dado) — isso não muda com esta correção.
    """

    def answer(self, snapshot: DashboardSnapshot) -> list[WarRoomAnswer]:
        answers: list[WarRoomAnswer] = []

        def add(question: str, value, evidence_type: str, note: str = "") -> None:
            if value in (None, UNKNOWN, [], ""):
                answers.append(WarRoomAnswer(question, "UNKNOWN (dado insuficiente)", "UNKNOWN", False))
            else:
                answers.append(
                    WarRoomAnswer(
                        question, f"{value}{(' — ' + note) if note else ''}",
                        evidence_type, evidence_type == "REAL",
                    )
                )

        add("Escopo destes dados", snapshot.scope_label, "N/A")
        add(
            "Qual canal está crescendo?",
            ", ".join(snapshot.top_channels) if snapshot.top_channels else UNKNOWN, "REAL",
        )
        add(
            "Qual produto está vendendo?",
            ", ".join(snapshot.top_products) if snapshot.top_products else UNKNOWN, "REAL",
        )
        add(
            "Qual oferta converte?",
            ", ".join(snapshot.top_offers) if snapshot.top_offers else UNKNOWN, "REAL",
        )
        add(
            "Qual funil funciona?",
            ", ".join(snapshot.top_funnels) if snapshot.top_funnels else UNKNOWN, "REAL",
        )
        add(
            "Qual a receita acumulada (REAL, líquida)?", snapshot.revenue, "REAL",
            "nunca inclui previsão/estimativa",
        )
        add(
            "Receita ESTIMADA (não comprovada)", snapshot.revenue_estimated, "ESTIMATED",
            "não confundir com resultado real",
        )
        add(
            "Receita PREVISTA/FORECAST (não comprovada)", snapshot.revenue_forecast, "FORECAST",
            "não confundir com resultado real",
        )
        add("Qual a margem atual?", snapshot.margin, "REAL")
        add(
            "O que deve ser interrompido / merece decisão do Owner?",
            "; ".join(snapshot.warnings) if snapshot.warnings else UNKNOWN, "REAL",
        )

        return answers
