"""
forge.models
==============
FORGE — estados do pipeline obrigatório (CORRIGIDO):

    DETECTED -> DIAGNOSING -> ROOT_CAUSE_FOUND -> FIX_PROPOSED
    -> RISK_ASSESSED -> [BLOCKED, quando necessário] -> FIX_APPROVED
    -> FIX_APPLYING -> FIX_APPLIED -> TESTING -> NON_REGRESSION_TESTING
    -> VALIDATED -> RELEASED
    (ROLLED_BACK alcançável a partir de qualquer estado não-terminal)

CORREÇÃO: `BLOCKED` NÃO é mais estado morto — é alcançável a partir de
QUALQUER etapa (quando a etapa falha ou exige revisão), e RETOMA o
pipeline (via `ForgeEngineer.resume_from_block`, sempre com autorização
explícita) de volta a uma etapa adiante. Só `RELEASED` e `ROLLED_BACK`
são terminais.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class DiagnosticStatus(str, Enum):
    DETECTED = "DETECTED"
    DIAGNOSING = "DIAGNOSING"
    ROOT_CAUSE_FOUND = "ROOT_CAUSE_FOUND"
    FIX_PROPOSED = "FIX_PROPOSED"
    RISK_ASSESSED = "RISK_ASSESSED"
    BLOCKED = "BLOCKED"
    FIX_APPROVED = "FIX_APPROVED"
    FIX_APPLYING = "FIX_APPLYING"
    FIX_APPLIED = "FIX_APPLIED"
    TESTING = "TESTING"
    NON_REGRESSION_TESTING = "NON_REGRESSION_TESTING"
    VALIDATED = "VALIDATED"
    RELEASED = "RELEASED"
    ROLLED_BACK = "ROLLED_BACK"
    # CORREÇÃO (microcorreção final — item 4.2): estado alcançado
    # quando uma tentativa de rollback NÃO conseguiu restaurar o
    # recurso de verdade — nunca tratado como sucesso silencioso.
    # Nunca terminal: a operação continua bloqueada e precisa de
    # intervenção (nova tentativa de rollback ou escalonamento).
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    # CORREÇÃO (microcorreção finalíssima): rollback solicitado sobre
    # um diagnóstico onde NENHUMA correção real chegou a ser aplicada
    # — não existe checkpoint porque não havia nada para desfazer.
    # Semanticamente DIFERENTE de ROLLED_BACK (que exige restauração
    # real e verificada) — nunca finge que algo foi restaurado.
    NO_ROLLBACK_NEEDED = "NO_ROLLBACK_NEEDED"


class BlockClassification(str, Enum):
    """CORREÇÃO (item 4.1): só bloqueios ADMINISTRATIVE podem usar o
    fluxo de liberação simplificado (autorização + justificativa).
    Bloqueios TECHNICAL (padrão — mais seguro) exigem o pipeline de
    correção completo até VALIDATED antes de `release_operation_block`
    aceitar liberar."""
    TECHNICAL = "TECHNICAL"
    ADMINISTRATIVE = "ADMINISTRATIVE"


# CORREÇÃO (microcorreção finalíssima): NO_ROLLBACK_NEEDED também é
# terminal — representa "nada havia para desfazer", um fim de linha
# legítimo para este diagnóstico (distinto de ROLLED_BACK, que exige
# restauração real).
_TERMINAL_STATUSES = {
    DiagnosticStatus.RELEASED, DiagnosticStatus.ROLLED_BACK, DiagnosticStatus.NO_ROLLBACK_NEEDED,
}

# Transições "de avanço normal" permitidas — cada etapa também pode,
# em caso de falha, ir para BLOCKED (tratado à parte em
# `forge.engineer`, não precisa estar listado aqui).
FORWARD_TRANSITIONS: dict = {
    DiagnosticStatus.DETECTED: {DiagnosticStatus.DIAGNOSING},
    DiagnosticStatus.DIAGNOSING: {DiagnosticStatus.ROOT_CAUSE_FOUND},
    DiagnosticStatus.ROOT_CAUSE_FOUND: {DiagnosticStatus.FIX_PROPOSED},
    DiagnosticStatus.FIX_PROPOSED: {DiagnosticStatus.RISK_ASSESSED},
    DiagnosticStatus.RISK_ASSESSED: {DiagnosticStatus.FIX_APPROVED},
    DiagnosticStatus.FIX_APPROVED: {DiagnosticStatus.FIX_APPLYING},
    DiagnosticStatus.FIX_APPLYING: {DiagnosticStatus.FIX_APPLIED},
    DiagnosticStatus.FIX_APPLIED: {DiagnosticStatus.TESTING},
    DiagnosticStatus.TESTING: {DiagnosticStatus.NON_REGRESSION_TESTING},
    DiagnosticStatus.NON_REGRESSION_TESTING: {DiagnosticStatus.VALIDATED},
    DiagnosticStatus.VALIDATED: {DiagnosticStatus.RELEASED},
    DiagnosticStatus.RELEASED: set(),
    DiagnosticStatus.ROLLED_BACK: set(),
    # BLOCKED só avança via `resume_from_block` (autorização explícita
    # obrigatória) — nunca por `_advance` genérico.
    DiagnosticStatus.BLOCKED: set(),
}

# Para onde `resume_from_block` pode retomar, a partir de BLOCKED —
# qualquer etapa "adiante" no pipeline normal (nunca RELEASED
# diretamente, que continua exigindo passar por VALIDATED).
RESUMABLE_FROM_BLOCKED = {
    DiagnosticStatus.DIAGNOSING, DiagnosticStatus.ROOT_CAUSE_FOUND, DiagnosticStatus.FIX_PROPOSED,
    DiagnosticStatus.RISK_ASSESSED, DiagnosticStatus.FIX_APPROVED, DiagnosticStatus.FIX_APPLYING,
    DiagnosticStatus.TESTING, DiagnosticStatus.NON_REGRESSION_TESTING,
}


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


@dataclass
class Diagnostic:
    diagnostic_id: str
    issue_summary: str
    root_cause: str | None = None
    proposed_fix: str | None = None
    risk_level: RiskLevel = RiskLevel.UNKNOWN
    severity: Severity = Severity.UNKNOWN
    status: DiagnosticStatus = DiagnosticStatus.DETECTED
    evidence: dict = field(default_factory=dict)
    blocked_operation: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def new_id() -> str:
        return f"diag_{uuid.uuid4().hex[:12]}"
