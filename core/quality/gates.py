"""
core.quality.gates
===================
Regra Suprema de Qualidade (Seção 3): SEM QUALIDADE COMPROVADA E
SUSTENTÁVEL, NÃO HÁ AVANÇO.

Framework genérico de Quality Gate reutilizado por vídeo, comercial,
afiliados etc. Cada gate concreto registra critérios avaliados e razões.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


@dataclass
class GateResult:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    criteria: dict[str, str] = field(default_factory=dict)  # criterio -> status

    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.APPROVED


class QualityGate:
    """Classe base. Subclasses implementam `evaluate(subject) -> GateResult`."""

    name: str = "generic_quality_gate"

    def evaluate(self, subject: dict) -> GateResult:  # pragma: no cover - abstrato
        raise NotImplementedError

    @staticmethod
    def _combine(results: list[GateResult]) -> GateResult:
        """Combina vários subresultados em um veredito final.

        Prioridade de severidade: BLOCKED > REJECTED > REWORK_REQUIRED
        > REVIEW_REQUIRED > UNKNOWN > APPROVED.
        """
        priority = [
            Verdict.BLOCKED,
            Verdict.REJECTED,
            Verdict.REWORK_REQUIRED,
            Verdict.REVIEW_REQUIRED,
            Verdict.UNKNOWN,
            Verdict.APPROVED,
        ]
        reasons: list[str] = []
        criteria: dict[str, str] = {}
        worst = Verdict.APPROVED
        for r in results:
            reasons.extend(r.reasons)
            criteria.update(r.criteria)
            if priority.index(r.verdict) < priority.index(worst):
                worst = r.verdict
        return GateResult(verdict=worst, reasons=reasons, criteria=criteria)
