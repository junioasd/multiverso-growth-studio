"""
core.agents.runtime.relay
=============================
BLOCO FINAL 1 (item 1) — RELAY: agente REAL de leads/CRM/
relacionamento. Wrapper sobre `commercial.crm.CRM` (já real,
persistido, com máquina de estados validada) — nunca inventa um lead
nem uma transição inválida (a própria `CRM.transition()` já recusa
transições fora da máquina de estados oficial).
"""

from __future__ import annotations

from commercial.crm import CRM, InvalidLeadTransition, LeadNotFound
from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability
from core.models.entities import LeadStatus


class RelayAgent(RealAgentContract):
    agent_id = "RELAY"
    primary_task_types = frozenset({"TRANSITION_LEAD", "GET_FUNNEL_COUNTS", "LIST_LEADS_BY_CHANNEL", "IDENTIFY_STALLED_LEADS"})
    all_rounder_task_types = frozenset({"GET_FUNNEL_COUNTS"})
    max_input_items = 5_000

    def __init__(self, *, crm: CRM | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.crm = crm

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if self.crm is None:
            return AgentTaskResult.unavailable(agent_id=self.agent_id, task_type=task_type, reason="nenhum CRM real conectado")
        if task_type == "TRANSITION_LEAD":
            return self._transition_lead(payload)
        if task_type == "GET_FUNNEL_COUNTS":
            return self._funnel_counts(payload)
        if task_type == "LIST_LEADS_BY_CHANNEL":
            return self._list_leads(payload)
        if task_type == "IDENTIFY_STALLED_LEADS":
            return self._identify_stalled(payload)
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _transition_lead(self, payload: dict) -> AgentTaskResult:
        if "lead_id" not in payload or "new_status" not in payload:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="TRANSITION_LEAD", reason="lead_id e new_status são obrigatórios",
            )
        try:
            new_status = LeadStatus(payload["new_status"])
        except ValueError:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="TRANSITION_LEAD", reason=f"new_status inválido: {payload['new_status']}",
            )
        try:
            lead = self.crm.transition(payload["lead_id"], new_status)
        except (LeadNotFound, InvalidLeadTransition) as exc:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type="TRANSITION_LEAD", reason=str(exc))
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="TRANSITION_LEAD",
            output={"lead_id": lead.lead_id, "status": lead.status.value}, evidence_ids=[lead.lead_id], confidence=1.0,
        )

    def _funnel_counts(self, payload: dict) -> AgentTaskResult:
        if "channel_id" not in payload:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="GET_FUNNEL_COUNTS", reason="channel_id é obrigatório",
            )
        counts = self.crm.funnel_counts(payload["channel_id"])
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="GET_FUNNEL_COUNTS", output={"counts": counts}, confidence=1.0,
        )

    def _list_leads(self, payload: dict) -> AgentTaskResult:
        if "channel_id" not in payload:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="LIST_LEADS_BY_CHANNEL", reason="channel_id é obrigatório",
            )
        leads = self.crm.by_channel(payload["channel_id"])
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="LIST_LEADS_BY_CHANNEL",
            output={"leads": [{"lead_id": l.lead_id, "status": l.status.value} for l in leads]},
            evidence_ids=[l.lead_id for l in leads], confidence=1.0,
        )

    def _identify_stalled(self, payload: dict) -> AgentTaskResult:
        """Leads que estão em um estado NÃO-terminal (nem CUSTOMER, nem
        LOST, nem UNSUBSCRIBED) — candidatos reais a remarketing/
        recuperação. Nunca "inventa" um motivo — só reporta o estado
        atual real."""
        if "channel_id" not in payload:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="IDENTIFY_STALLED_LEADS", reason="channel_id é obrigatório",
            )
        terminal = {LeadStatus.CUSTOMER, LeadStatus.LOST, LeadStatus.UNSUBSCRIBED}
        leads = self.crm.by_channel(payload["channel_id"])
        stalled = [l for l in leads if l.status not in terminal]
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="IDENTIFY_STALLED_LEADS",
            output={"stalled_leads": [{"lead_id": l.lead_id, "status": l.status.value} for l in stalled], "total_checked": len(leads)},
            evidence_ids=[l.lead_id for l in stalled], confidence=1.0,
        )


def register_relay_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="RELAY", capability=Capability.COMMERCIAL_INTELLIGENCE, level=CompetencyLevel.ESPECIALISTA,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
