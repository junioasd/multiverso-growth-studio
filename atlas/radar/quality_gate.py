"""
atlas.radar.quality_gate
===========================
ATLAS+RADAR — item 5: o Radar Comercial integra os Quality Gates
existentes. `OpportunityEvidenceGate` reutiliza a MESMA base
(`core.quality.gates.QualityGate`/`GateResult`/`Verdict`) usada por
`VideoQualityGate`/`CommercialQualityGate` — nenhum framework de
qualidade paralelo foi criado.

Bloqueia (BLOCKED) diante de política incompatível ou risco elevado
comprovado; exige revisão (REVIEW_REQUIRED) diante de evidência
insuficiente, dado contraditório, custo sem justificativa, ou margem/
viabilidade desconhecida quando essenciais à decisão — nunca aprova por
omissão.
"""

from __future__ import annotations

from atlas.radar.models import CommercialOpportunity, EvidenceLevel
from core.quality.gates import GateResult, QualityGate, Verdict


class OpportunityEvidenceGate(QualityGate):
    name = "opportunity_evidence_gate"

    _ESSENTIAL_SIGNALS = ("margin_potential", "cost", "compliance")

    def evaluate(self, subject: dict) -> GateResult:
        opportunity: CommercialOpportunity = subject["opportunity"]
        policy_compatible = subject.get("policy_compatible")
        risk_level = subject.get("risk_level")
        contradictory_data = subject.get("contradictory_data", False)

        reasons: list[str] = []
        criteria: dict[str, str] = {}
        verdict = Verdict.APPROVED

        order = [
            Verdict.BLOCKED, Verdict.REJECTED, Verdict.REWORK_REQUIRED,
            Verdict.REVIEW_REQUIRED, Verdict.UNKNOWN, Verdict.APPROVED,
        ]

        def worsen(new_verdict: Verdict, reason: str, key: str, status: str) -> None:
            nonlocal verdict
            reasons.append(reason)
            criteria[key] = status
            if order.index(new_verdict) < order.index(verdict):
                verdict = new_verdict

        if policy_compatible is False:
            worsen(Verdict.BLOCKED, "política incompatível com a oportunidade", "policy", "FAIL")
        elif policy_compatible is None:
            worsen(Verdict.REVIEW_REQUIRED, "compatibilidade de política não verificada", "policy", "UNKNOWN")
        else:
            criteria["policy"] = "OK"

        if risk_level == "HIGH":
            worsen(Verdict.BLOCKED, "risco elevado identificado", "risk", "FAIL")
        elif risk_level is None:
            worsen(Verdict.REVIEW_REQUIRED, "risco não avaliado", "risk", "UNKNOWN")
        else:
            criteria["risk"] = "OK"

        if contradictory_data:
            worsen(Verdict.REVIEW_REQUIRED, "dado contraditório encontrado na oportunidade", "data_consistency", "FAIL")
        else:
            criteria["data_consistency"] = "OK"

        for key in self._ESSENTIAL_SIGNALS:
            signal = opportunity.signal(key)
            if signal.evidence == EvidenceLevel.UNKNOWN:
                worsen(
                    Verdict.REVIEW_REQUIRED, f"sinal essencial '{key}' sem evidência suficiente",
                    key, "UNKNOWN",
                )
            else:
                criteria[key] = signal.evidence.value

        return GateResult(verdict=verdict, reasons=reasons, criteria=criteria)
