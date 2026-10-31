"""
core.agents.runtime.veritas
===============================
BLOCO FINAL 1 (item 1) — VERITAS: agente REAL de pesquisa factual e
checagem. Nunca "decide" se uma claim é verdadeira por conta própria —
sempre resolve contra o `EvidenceRegistry` real (a mesma fonte de
verdade usada pelo Quality Gate/Adoption Pipeline) e reporta a
confiança EXATAMENTE proporcional à evidência genuína encontrada.
Uma claim sem evidência registrada nunca é confirmada nem negada —
é reportada como `UNVERIFIABLE`, nunca um veredito inventado.
"""

from __future__ import annotations

from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability
from core.evidence.registry import EvidenceRegistry, VerificationStatus


class VeritasAgent(RealAgentContract):
    agent_id = "VERITAS"
    primary_task_types = frozenset({"VERIFY_CLAIM", "CROSS_REFERENCE_EVIDENCE"})
    all_rounder_task_types = frozenset({"VERIFY_CLAIM"})
    max_input_items = 200

    def __init__(self, *, evidence_registry: EvidenceRegistry | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.evidence_registry = evidence_registry

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type == "VERIFY_CLAIM":
            return self._verify_claim(payload)
        if task_type == "CROSS_REFERENCE_EVIDENCE":
            return self._cross_reference(payload)
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _verify_claim(self, payload: dict) -> AgentTaskResult:
        if self.evidence_registry is None:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type="VERIFY_CLAIM", reason="nenhum EvidenceRegistry real conectado",
            )
        evidence_ids = payload.get("evidence_ids")
        if not evidence_ids:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="VERIFY_CLAIM",
                reason="evidence_ids é obrigatório — VERITAS nunca opina sem evidência apontada",
            )
        strategy_id = payload.get("strategy_id")
        metric_name = payload.get("metric_name")

        statuses = []
        for evidence_id in evidence_ids:
            if strategy_id or metric_name:
                status, _binding_reason = self.evidence_registry.resolve_status_for(
                    evidence_id, strategy_id=strategy_id, metric_name=metric_name,
                )
            else:
                status = self.evidence_registry.resolve_status(evidence_id)
            statuses.append((evidence_id, status))

        verified = [eid for eid, s in statuses if s == VerificationStatus.VERIFIED]
        missing = [eid for eid, s in statuses if s == VerificationStatus.MISSING]
        invalid_or_other = [eid for eid, s in statuses if s not in (VerificationStatus.VERIFIED, VerificationStatus.MISSING)]

        total = len(statuses)
        verified_ratio = len(verified) / total if total else 0.0

        if not verified and not invalid_or_other:
            verdict = "UNVERIFIABLE"
            confidence = 0.0
        elif verified_ratio == 1.0:
            verdict = "SUPPORTED"
            confidence = 1.0
        elif verified_ratio > 0:
            verdict = "PARTIALLY_SUPPORTED"
            confidence = verified_ratio
        else:
            verdict = "UNSUPPORTED"
            confidence = 0.0

        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="VERIFY_CLAIM",
            output={
                "verdict": verdict, "verified_count": len(verified), "missing_count": len(missing),
                "other_count": len(invalid_or_other), "total_evidence_checked": total,
            },
            evidence_ids=list(evidence_ids), confidence=confidence,
        )

    def _cross_reference(self, payload: dict) -> AgentTaskResult:
        if self.evidence_registry is None:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type="CROSS_REFERENCE_EVIDENCE",
                reason="nenhum EvidenceRegistry real conectado",
            )
        evidence_id_groups = payload.get("evidence_id_groups")
        if not evidence_id_groups or len(evidence_id_groups) < 2:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="CROSS_REFERENCE_EVIDENCE",
                reason="evidence_id_groups precisa ter ao menos 2 grupos para cruzar",
            )
        group_results = []
        for group in evidence_id_groups:
            verified = sum(1 for eid in group if self.evidence_registry.resolve_status(eid) == VerificationStatus.VERIFIED)
            group_results.append({"size": len(group), "verified": verified})

        all_agree = all(g["verified"] == g["size"] and g["size"] > 0 for g in group_results)
        any_verified = any(g["verified"] > 0 for g in group_results)
        agreement = "CORROBORATED" if all_agree else ("MIXED" if any_verified else "NO_SUPPORT")

        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="CROSS_REFERENCE_EVIDENCE",
            output={"agreement": agreement, "groups": group_results},
            evidence_ids=[eid for group in evidence_id_groups for eid in group],
            confidence=1.0 if all_agree else (0.5 if any_verified else 0.0),
        )


def register_veritas_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="VERITAS", capability=Capability.QUALITY_CONTROL, level=CompetencyLevel.ESPECIALISTA,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
    capability_registry.register(
        agent="VERITAS", capability=Capability.REASONING, level=CompetencyLevel.ESPECIALISTA,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
