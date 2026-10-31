"""
core.agents.runtime.mercury
===============================
BLOCO FINAL 1 (item 1) — MERCURY: agente REAL de MultiGerenciador
Comercial. Agrega dados JÁ REAIS de múltiplos canais (CRM + Finance +
Sales) para dar uma visão consolidada — nunca calcula uma média/ranking
com canais que não têm dado nenhum (excluídos explicitamente, nunca
tratados como zero silencioso).
"""

from __future__ import annotations

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.sales_tracking import SalesTracker
from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability


class MercuryAgent(RealAgentContract):
    agent_id = "MERCURY"
    primary_task_types = frozenset({"COMPARE_CHANNELS", "CONSOLIDATED_OVERVIEW"})
    all_rounder_task_types = frozenset({"CONSOLIDATED_OVERVIEW"})
    max_input_items = 200

    def __init__(self, *, crm: CRM | None = None, finance_ledger: FinanceLedger | None = None,
                 sales_tracker: SalesTracker | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.crm = crm
        self.finance_ledger = finance_ledger
        self.sales_tracker = sales_tracker

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type == "COMPARE_CHANNELS":
            return self._compare_channels(payload)
        if task_type == "CONSOLIDATED_OVERVIEW":
            return self._consolidated_overview(payload)
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _consolidated_overview(self, payload: dict) -> AgentTaskResult:
        channel_id = payload.get("channel_id")
        missing_systems = []
        overview = {"channel_id": channel_id}

        if self.finance_ledger is not None:
            snapshot = self.finance_ledger.snapshot(channel_id)
            overview["finance"] = {"revenue": snapshot.revenue, "margin": snapshot.margin}
        else:
            missing_systems.append("finance_ledger")

        if self.sales_tracker is not None:
            summary = self.sales_tracker.summarize_filtered(channel_id=channel_id)
            overview["sales"] = {"sales": summary.sales, "real_revenue": summary.real_revenue}
        else:
            missing_systems.append("sales_tracker")

        if self.crm is not None:
            overview["funnel"] = self.crm.funnel_counts(channel_id) if channel_id else None
        else:
            missing_systems.append("crm")

        if len(missing_systems) == 3:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type="CONSOLIDATED_OVERVIEW",
                reason="nenhum sistema real conectado (crm/finance_ledger/sales_tracker)",
            )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="CONSOLIDATED_OVERVIEW",
            output={"overview": overview, "unavailable_systems": missing_systems},
            confidence=1.0 - (len(missing_systems) / 3),
        )

    def _compare_channels(self, payload: dict) -> AgentTaskResult:
        if self.finance_ledger is None:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type="COMPARE_CHANNELS", reason="nenhum FinanceLedger real conectado",
            )
        channel_ids = payload.get("channel_ids")
        if not channel_ids or not isinstance(channel_ids, list):
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="COMPARE_CHANNELS", reason="channel_ids (lista) é obrigatório",
            )
        comparisons = []
        for cid in channel_ids:
            snapshot = self.finance_ledger.snapshot(cid)
            has_data = snapshot.revenue != 0 or snapshot.expenses != 0
            comparisons.append({
                "channel_id": cid, "revenue": snapshot.revenue, "margin": snapshot.margin, "has_data": has_data,
            })
        with_data = [c for c in comparisons if c["has_data"]]
        ranked = sorted(with_data, key=lambda c: c["revenue"], reverse=True)
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="COMPARE_CHANNELS",
            output={
                "ranked_by_revenue": ranked,
                "channels_without_data": [c["channel_id"] for c in comparisons if not c["has_data"]],
            },
            confidence=1.0 if with_data else 0.0,
        )


def register_mercury_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="MERCURY", capability=Capability.COMMERCIAL_INTELLIGENCE, level=CompetencyLevel.MASTER,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
