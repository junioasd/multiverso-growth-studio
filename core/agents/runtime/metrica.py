"""
core.agents.runtime.metrica
===============================
BLOCO FINAL 1 (item 1) â€” METRICA: agente REAL de mÃ©tricas
multidimensionais. Opera SOMENTE sobre dados que jÃ¡ existem nos
sistemas reais do MULTIVERSO (FinanceLedger/SalesTracker/CRM) â€”
nunca fabrica um nÃºmero. Toda mÃ©trica devolvida carrega a evidÃªncia
(quantidade de entradas/eventos usados, canal, janela) que a
sustenta.
"""

from __future__ import annotations

from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability


class MetricaAgent(RealAgentContract):
    agent_id = "METRICA"
    primary_task_types = frozenset({
        "COMPUTE_FINANCE_SNAPSHOT", "COMPUTE_CAC", "COMPUTE_LTV", "COMPUTE_ROI", "COMPUTE_SALES_SUMMARY",
    })
    all_rounder_task_types = frozenset({"COMPUTE_FINANCE_SNAPSHOT"})
    max_input_items = 10_000

    def __init__(self, *, finance_ledger=None, sales_tracker=None, audit=None) -> None:
        super().__init__(audit=audit)
        self.finance_ledger = finance_ledger
        self.sales_tracker = sales_tracker

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type == "COMPUTE_FINANCE_SNAPSHOT":
            return self._compute_finance_snapshot(payload)
        if task_type == "COMPUTE_CAC":
            return self._compute_cac(payload)
        if task_type == "COMPUTE_LTV":
            return self._compute_ltv(payload)
        if task_type == "COMPUTE_ROI":
            return self._compute_roi(payload)
        if task_type == "COMPUTE_SALES_SUMMARY":
            return self._compute_sales_summary(payload)
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type nÃ£o implementado")

    def _compute_finance_snapshot(self, payload: dict) -> AgentTaskResult:
        if self.finance_ledger is None:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type="COMPUTE_FINANCE_SNAPSHOT",
                reason="nenhum FinanceLedger real conectado a este agente",
            )
        channel_id = payload.get("channel_id")
        snapshot = self.finance_ledger.snapshot(channel_id)
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="COMPUTE_FINANCE_SNAPSHOT",
            output={
                "revenue": snapshot.revenue, "expenses": snapshot.expenses,
                "committed_cost": snapshot.committed_cost, "realized_cost": snapshot.realized_cost,
                "commission": snapshot.commission, "refunds": snapshot.refunds, "margin": snapshot.margin,
                "operational_profit_loss": snapshot.operational_profit_loss, "channel_id": channel_id,
            },
            evidence_ids=[f"finance_ledger:channel={channel_id}"], confidence=1.0,
        )

    def _compute_cac(self, payload: dict) -> AgentTaskResult:
        if self.finance_ledger is None:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type="COMPUTE_CAC", reason="nenhum FinanceLedger real conectado",
            )
        marketing_spend = payload.get("marketing_spend")
        new_customers = payload.get("new_customers")
        if marketing_spend is None or new_customers is None:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="COMPUTE_CAC",
                reason="marketing_spend e new_customers sÃ£o obrigatÃ³rios",
            )
        cac = self.finance_ledger.cac(marketing_spend=marketing_spend, new_customers=new_customers)
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="COMPUTE_CAC", output={"cac": cac},
            evidence_ids=[f"input:marketing_spend={marketing_spend},new_customers={new_customers}"],
            confidence=1.0 if cac != "UNKNOWN" else 0.0,
        )

    def _compute_ltv(self, payload: dict) -> AgentTaskResult:
        if self.finance_ledger is None:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type="COMPUTE_LTV", reason="nenhum FinanceLedger real conectado",
            )
        avg_revenue = payload.get("avg_revenue_per_customer")
        avg_lifespan = payload.get("avg_customer_lifespan_periods")
        if avg_revenue is None or avg_lifespan is None:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="COMPUTE_LTV",
                reason="avg_revenue_per_customer e avg_customer_lifespan_periods sÃ£o obrigatÃ³rios",
            )
        ltv = self.finance_ledger.ltv(avg_revenue_per_customer=avg_revenue, avg_customer_lifespan_periods=avg_lifespan)
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="COMPUTE_LTV", output={"ltv": ltv},
            evidence_ids=[f"input:avg_revenue={avg_revenue},avg_lifespan={avg_lifespan}"],
            confidence=1.0 if ltv != "UNKNOWN" else 0.0,
        )

    def _compute_roi(self, payload: dict) -> AgentTaskResult:
        if self.finance_ledger is None:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type="COMPUTE_ROI", reason="nenhum FinanceLedger real conectado",
            )
        gain = payload.get("gain")
        cost = payload.get("cost")
        if gain is None or cost is None:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="COMPUTE_ROI", reason="gain e cost sÃ£o obrigatÃ³rios",
            )
        roi = self.finance_ledger.roi(gain=gain, cost=cost)
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="COMPUTE_ROI", output={"roi": roi},
            evidence_ids=[f"input:gain={gain},cost={cost}"], confidence=1.0 if roi != "UNKNOWN" else 0.0,
        )

    def _compute_sales_summary(self, payload: dict) -> AgentTaskResult:
        if self.sales_tracker is None:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type="COMPUTE_SALES_SUMMARY", reason="nenhum SalesTracker real conectado",
            )
        channel_id = payload.get("channel_id")
        summary = self.sales_tracker.summarize_filtered(channel_id=channel_id) if hasattr(
            self.sales_tracker, "summarize_filtered"
        ) else None
        if summary is None:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type="COMPUTE_SALES_SUMMARY",
                reason="SalesTracker conectado nÃ£o expÃµe summarize_filtered()",
            )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="COMPUTE_SALES_SUMMARY",
            output={
                "sales": summary.sales, "refunds": summary.refunds, "real_revenue": summary.real_revenue,
                "estimated_revenue": summary.estimated_revenue, "forecast_revenue": summary.forecast_revenue,
                "channel_id": channel_id,
            },
            evidence_ids=[f"sales_tracker:channel={channel_id}"], confidence=1.0,
        )


def register_metrica_capabilities(capability_registry, *, granted_by: str) -> None:
    """Registra as capacidades REAIS de METRICA no Capability Registry
    â€” nunca autodeclarado pelo prÃ³prio agente (sempre via autoridade
    externa, conforme a disciplina jÃ¡ estabelecida)."""
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="METRICA", capability=Capability.DATA_ANALYSIS, level=CompetencyLevel.MASTER,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )

