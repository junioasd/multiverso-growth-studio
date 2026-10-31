"""
atlas.quality_supervisor
===========================
ATLAS — itens 13-14: trabalhar integrado ao sistema de Quality Gates, e
bloquear ou escalar operações quando qualidade, segurança, compliance
ou evidência forem insuficientes.

Não reimplementa NENHUM gate — reutiliza `core.quality.video_quality`,
`core.quality.commercial_quality` e `commercial.affiliate_compliance`
tal como já existem e já são testados. `QualitySupervisor` apenas
traduz o veredito de um gate numa DECISÃO OPERACIONAL do ATLAS:

    APPROVED                                    -> PROCEED
    BLOCKED / REJECTED                          -> BLOCK
    REVIEW_REQUIRED / REWORK_REQUIRED / UNKNOWN  -> ESCALATE

"UNKNOWN" nunca vira PROCEED por omissão — evidência insuficiente é
tratada como precisando de decisão humana (ESCALATE), nunca como
"provavelmente está tudo bem".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from atlas.radar.quality_gate import OpportunityEvidenceGate
from commercial.affiliate_compliance import AffiliateComplianceGate
from core.quality.commercial_quality import CommercialQualityGate
from core.quality.gates import GateResult, Verdict
from core.quality.video_quality import VideoQualityGate


class SupervisionDecision(str, Enum):
    PROCEED = "PROCEED"
    BLOCK = "BLOCK"
    ESCALATE = "ESCALATE"


_VERDICT_TO_DECISION: dict[Verdict, SupervisionDecision] = {
    Verdict.APPROVED: SupervisionDecision.PROCEED,
    Verdict.BLOCKED: SupervisionDecision.BLOCK,
    Verdict.REJECTED: SupervisionDecision.BLOCK,
    Verdict.REWORK_REQUIRED: SupervisionDecision.ESCALATE,
    Verdict.REVIEW_REQUIRED: SupervisionDecision.ESCALATE,
    Verdict.UNKNOWN: SupervisionDecision.ESCALATE,
}


@dataclass
class SupervisionOutcome:
    decision: SupervisionDecision
    gate_result: GateResult
    gate_name: str


class QualitySupervisor:
    def __init__(self) -> None:
        self.video_gate = VideoQualityGate()
        self.commercial_gate = CommercialQualityGate()
        self.affiliate_compliance_gate = AffiliateComplianceGate()
        self.opportunity_gate = OpportunityEvidenceGate()

    @staticmethod
    def _decide(gate_result: GateResult) -> SupervisionDecision:
        return _VERDICT_TO_DECISION[gate_result.verdict]

    def review_video(self, subject: dict) -> SupervisionOutcome:
        result = self.video_gate.evaluate(subject)
        return SupervisionOutcome(self._decide(result), result, "video_quality_gate")

    def review_commercial_scaling(self, subject: dict) -> SupervisionOutcome:
        result = self.commercial_gate.evaluate(subject)
        return SupervisionOutcome(self._decide(result), result, "commercial_quality_gate")

    def review_affiliate_compliance(self, subject: dict) -> SupervisionOutcome:
        result = self.affiliate_compliance_gate.evaluate(subject)
        return SupervisionOutcome(self._decide(result), result, "affiliate_compliance_gate")

    def review_opportunity(self, subject: dict) -> SupervisionOutcome:
        """item 5 (Radar Comercial integrado aos Quality Gates). `subject`
        no formato esperado por `atlas.radar.quality_gate.OpportunityEvidenceGate`."""
        result = self.opportunity_gate.evaluate(subject)
        return SupervisionOutcome(self._decide(result), result, "opportunity_evidence_gate")
