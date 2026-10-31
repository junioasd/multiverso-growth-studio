"""
core.agents.runtime.sentinel
================================
MICROCORREÇÃO — item 1 (25 AGENTES: eliminar PARTIAL). SENTINEL agora
tem runtime REAL — wrapper fino sobre `atlas.quality_supervisor.
QualitySupervisor` (já real, já testado, nunca reimplementado aqui).
`UNKNOWN`/evidência insuficiente nunca vira aprovação por omissão —
sempre `ESCALATE`, preservando a disciplina já estabelecida.
"""

from __future__ import annotations

from atlas.quality_supervisor import QualitySupervisor
from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability


class SentinelAgent(RealAgentContract):
    agent_id = "SENTINEL"
    primary_task_types = frozenset({
        "REVIEW_VIDEO", "REVIEW_COMMERCIAL_SCALING", "REVIEW_AFFILIATE_COMPLIANCE", "REVIEW_OPPORTUNITY",
    })
    all_rounder_task_types = frozenset({"REVIEW_VIDEO"})
    max_input_items = 200

    def __init__(self, *, quality_supervisor: QualitySupervisor | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.quality_supervisor = quality_supervisor if quality_supervisor is not None else QualitySupervisor()

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        subject = payload.get("subject")
        if not isinstance(subject, dict):
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type=task_type, reason="'subject' (dict) é obrigatório",
            )
        method_by_task = {
            "REVIEW_VIDEO": self.quality_supervisor.review_video,
            "REVIEW_COMMERCIAL_SCALING": self.quality_supervisor.review_commercial_scaling,
            "REVIEW_AFFILIATE_COMPLIANCE": self.quality_supervisor.review_affiliate_compliance,
            "REVIEW_OPPORTUNITY": self.quality_supervisor.review_opportunity,
        }
        method = method_by_task.get(task_type)
        if method is None:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

        outcome = method(subject)
        confidence = 1.0 if outcome.gate_result.verdict.value != "UNKNOWN" else 0.0
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type=task_type,
            output={
                "decision": outcome.decision.value, "verdict": outcome.gate_result.verdict.value,
                "gate_name": outcome.gate_name, "reasons": outcome.gate_result.reasons,
                "criteria": outcome.gate_result.criteria,
            },
            evidence_ids=[outcome.gate_name], confidence=confidence,
        )


def register_sentinel_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="SENTINEL", capability=Capability.QUALITY_CONTROL, level=CompetencyLevel.MASTER,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
