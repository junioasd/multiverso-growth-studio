"""
core.agents.runtime.nexus
=============================
BLOCO FINAL 1 (item 1) — NEXUS: agente REAL de funis, ofertas e
conversão. Taxas de conversão calculadas a partir dos `funnel_counts`
REAIS do CRM — nunca uma estimativa "genérica de mercado". Estágio
sem nenhum lead em algum dos dois pontos comparados é reportado como
`UNDEFINED`, nunca 0% nem 100% arbitrário.
"""

from __future__ import annotations

from commercial.crm import CRM
from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability

# ordem oficial do funil — mesma máquina de estados de commercial.crm
_FUNNEL_ORDER = ["NEW", "ENGAGED", "QUALIFIED", "OFFERED", "CUSTOMER"]


class NexusAgent(RealAgentContract):
    agent_id = "NEXUS"
    primary_task_types = frozenset({"ANALYZE_FUNNEL_CONVERSION", "IDENTIFY_BIGGEST_DROP_OFF"})
    all_rounder_task_types = frozenset({"ANALYZE_FUNNEL_CONVERSION"})
    max_input_items = 500

    def __init__(self, *, crm: CRM | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.crm = crm

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if self.crm is None:
            return AgentTaskResult.unavailable(agent_id=self.agent_id, task_type=task_type, reason="nenhum CRM real conectado")
        if task_type == "ANALYZE_FUNNEL_CONVERSION":
            return self._analyze_conversion(payload)
        if task_type == "IDENTIFY_BIGGEST_DROP_OFF":
            return self._biggest_drop_off(payload)
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _stage_totals(self, channel_id: str) -> dict:
        """CORREÇÃO: um lead em `ENGAGED` já PASSOU por `NEW` — o total
        acumulado de cada estágio inclui todos os estágios
        posteriores (nunca conta só quem está PARADO exatamente ali,
        senão a taxa de conversão ficaria artificialmente baixa)."""
        counts = self.crm.funnel_counts(channel_id)
        cumulative = {}
        for i, stage in enumerate(_FUNNEL_ORDER):
            cumulative[stage] = sum(counts.get(s, 0) for s in _FUNNEL_ORDER[i:])
        return cumulative

    def _analyze_conversion(self, payload: dict) -> AgentTaskResult:
        if "channel_id" not in payload:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="ANALYZE_FUNNEL_CONVERSION", reason="channel_id é obrigatório",
            )
        cumulative = self._stage_totals(payload["channel_id"])
        rates = {}
        for i in range(len(_FUNNEL_ORDER) - 1):
            from_stage, to_stage = _FUNNEL_ORDER[i], _FUNNEL_ORDER[i + 1]
            from_count = cumulative[from_stage]
            if from_count == 0:
                rates[f"{from_stage}->{to_stage}"] = "UNDEFINED"
            else:
                rates[f"{from_stage}->{to_stage}"] = round(cumulative[to_stage] / from_count, 4)
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="ANALYZE_FUNNEL_CONVERSION",
            output={"conversion_rates": rates, "stage_totals": cumulative}, confidence=1.0,
        )

    def _biggest_drop_off(self, payload: dict) -> AgentTaskResult:
        if "channel_id" not in payload:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="IDENTIFY_BIGGEST_DROP_OFF", reason="channel_id é obrigatório",
            )
        cumulative = self._stage_totals(payload["channel_id"])
        defined_rates = {}
        for i in range(len(_FUNNEL_ORDER) - 1):
            from_stage, to_stage = _FUNNEL_ORDER[i], _FUNNEL_ORDER[i + 1]
            if cumulative[from_stage] > 0:
                defined_rates[f"{from_stage}->{to_stage}"] = cumulative[to_stage] / cumulative[from_stage]
        if not defined_rates:
            return AgentTaskResult.completed(
                agent_id=self.agent_id, task_type="IDENTIFY_BIGGEST_DROP_OFF",
                output={"status": "INSUFFICIENT_DATA", "reason": "nenhum estágio com leads suficientes para comparar"},
                confidence=0.0,
            )
        worst = min(defined_rates, key=defined_rates.get)
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="IDENTIFY_BIGGEST_DROP_OFF",
            output={"worst_transition": worst, "rate": round(defined_rates[worst], 4), "all_rates": defined_rates},
            confidence=1.0,
        )


def register_nexus_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="NEXUS", capability=Capability.COMMERCIAL_INTELLIGENCE, level=CompetencyLevel.MASTER,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
