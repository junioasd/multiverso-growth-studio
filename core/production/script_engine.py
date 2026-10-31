"""
core.production.script_engine
================================
Roteiro (Seção 9). Prioriza originalidade, valor real, storytelling,
clareza, curiosidade, retenção, progressão, entrega da promessa,
naturalidade, CTA contextual. Fact-check quando necessário; se
informação não for confiável => UNKNOWN ou REVIEW_REQUIRED.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

_GENERIC_FILLER_PATTERNS = [
    r"\bem um mundo cada vez mais\b",
    r"\bvocê já parou para pensar\b",
    r"\bsem mais delongas\b",
    r"\bcomo todos sabemos\b",
]


class FactCheckStatus(str, Enum):
    OK = "OK"
    UNKNOWN = "UNKNOWN"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


@dataclass
class FactClaim:
    claim: str
    source: str | None = None
    confidence: float | None = None  # 0-1, None = não avaliado


@dataclass
class FactCheckReport:
    status: FactCheckStatus
    unresolved_claims: list[str] = field(default_factory=list)


class ScriptQualityIssue(Enum):
    GENERIC_FILLER = "generic_filler"
    NO_CTA = "no_cta"
    NO_HOOK = "no_hook"


@dataclass
class ScriptAssessment:
    score: float
    issues: list[str] = field(default_factory=list)


def fact_check(claims: list[FactClaim], *, min_confidence: float = 0.7) -> FactCheckReport:
    unresolved = []
    any_failed = False
    for claim in claims:
        if claim.source is None or claim.confidence is None:
            unresolved.append(claim.claim)
            continue
        if claim.confidence < min_confidence:
            unresolved.append(claim.claim)
        if claim.confidence < 0.2:
            any_failed = True

    if any_failed:
        return FactCheckReport(status=FactCheckStatus.FAILED, unresolved_claims=unresolved)
    if unresolved:
        return FactCheckReport(status=FactCheckStatus.REVIEW_REQUIRED, unresolved_claims=unresolved)
    return FactCheckReport(status=FactCheckStatus.OK, unresolved_claims=[])


def assess_script(script_text: str, *, has_cta: bool, has_hook_in_first_lines: bool) -> ScriptAssessment:
    issues: list[str] = []
    score = 1.0

    for pattern in _GENERIC_FILLER_PATTERNS:
        if re.search(pattern, script_text, re.IGNORECASE):
            issues.append(f"frase genérica/robótica detectada: '{pattern}'")
            score -= 0.1

    if not has_cta:
        issues.append("roteiro sem CTA contextual")
        score -= 0.15

    if not has_hook_in_first_lines:
        issues.append("sem gancho (hook) nas primeiras linhas")
        score -= 0.2

    word_count = len(script_text.split())
    if word_count < 30:
        issues.append("roteiro muito curto para entregar valor real")
        score -= 0.2

    return ScriptAssessment(score=max(0.0, score), issues=issues)
