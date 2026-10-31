"""
atlas.sentinel
================
EXPANSÃO — item 10 (Quality Gates / SENTINEL).

SENTINEL é o nome oficial do responsável pelo Controle de Qualidade do
Growth Studio. Não reimplementa NENHUM gate — envolve
`atlas.quality_supervisor.QualitySupervisor` (já existente, já
testado) e traduz seu resultado para o vocabulário genérico exigido
nesta expansão: PASS / FAIL / BLOCK / WARNING, com critérios,
thresholds e evidências explícitos.

SENTINEL pode bloquear publicação, entrega, campanha, integração,
deploy ou operação — mas, como sempre, só RECOMENDA o bloqueio; quem
efetivamente impede a ação é a ausência de autoridade do ATLAS/
NAVIGATOR para executá-la sem aprovação (ver `atlas.director.
AtlasDirector.request_critical_action`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from atlas.quality_supervisor import QualitySupervisor, SupervisionDecision
from core.quality.gates import Verdict


class SentinelVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCK = "BLOCK"
    WARNING = "WARNING"


_GATE_VERDICT_TO_SENTINEL: dict[Verdict, SentinelVerdict] = {
    Verdict.APPROVED: SentinelVerdict.PASS,
    Verdict.BLOCKED: SentinelVerdict.BLOCK,
    Verdict.REJECTED: SentinelVerdict.FAIL,
    Verdict.REWORK_REQUIRED: SentinelVerdict.WARNING,
    Verdict.REVIEW_REQUIRED: SentinelVerdict.WARNING,
    Verdict.UNKNOWN: SentinelVerdict.WARNING,
}

# Operações que o SENTINEL pode recomendar bloquear (Seção 10).
BLOCKABLE_OPERATION_KINDS = frozenset({
    "publication", "delivery", "campaign", "integration", "deploy", "operation",
})

_REVIEW_METHOD_BY_CHECK_KIND = {
    "video": "review_video",
    "commercial_scaling": "review_commercial_scaling",
    "affiliate_compliance": "review_affiliate_compliance",
    "opportunity": "review_opportunity",
}


@dataclass
class SentinelCheck:
    check_kind: str
    verdict: SentinelVerdict
    reasons: list = field(default_factory=list)
    criteria: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


class Sentinel:
    def __init__(self, quality_supervisor: QualitySupervisor | None = None) -> None:
        self.quality_supervisor = quality_supervisor or QualitySupervisor()

    def check(self, *, check_kind: str, subject: dict) -> SentinelCheck:
        method_name = _REVIEW_METHOD_BY_CHECK_KIND.get(check_kind)
        if method_name is None:
            raise ValueError(
                f"tipo de checagem desconhecido: '{check_kind}' — "
                f"esperado um de {sorted(_REVIEW_METHOD_BY_CHECK_KIND)}"
            )
        outcome = getattr(self.quality_supervisor, method_name)(subject)
        verdict = _GATE_VERDICT_TO_SENTINEL[outcome.gate_result.verdict]
        return SentinelCheck(
            check_kind=check_kind, verdict=verdict, reasons=list(outcome.gate_result.reasons),
            criteria=dict(outcome.gate_result.criteria), evidence={"gate": outcome.gate_name},
        )

    def can_proceed(self, *, check_kind: str, subject: dict) -> bool:
        return self.check(check_kind=check_kind, subject=subject).verdict == SentinelVerdict.PASS

    def should_block(self, *, operation_kind: str, check_kind: str, subject: dict) -> bool:
        if operation_kind not in BLOCKABLE_OPERATION_KINDS:
            raise ValueError(
                f"tipo de operação bloqueável desconhecido: '{operation_kind}' — "
                f"esperado um de {sorted(BLOCKABLE_OPERATION_KINDS)}"
            )
        result = self.check(check_kind=check_kind, subject=subject)
        return result.verdict in (SentinelVerdict.BLOCK, SentinelVerdict.FAIL)
