"""
core.reasoning.cycle
========================
EXPANSÃO — item 3 (fortalecimento de raciocínio de todas as IAs).

Ciclo padrão que qualquer agente do Growth Studio (ATLAS, NAVIGATOR,
FORGE, ou um especialista futuro) usa para estruturar seu trabalho:

    OBSERVAR -> COMPREENDER -> PLANEJAR -> EXECUTAR -> VERIFICAR
    -> CORRIGIR -> APRENDER -> REPORTAR

Não é um "motor de IA" — é uma estrutura de REGISTRO E DISCIPLINA:
`ReasoningTrace` força que toda alegação de sucesso venha com
evidência (`mark_verified` recusa evidência vazia), e mantém um
histórico auditável de cada etapa. Nenhuma IA "declara sucesso" sem
passar por aqui.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class ReasoningStage(str, Enum):
    OBSERVE = "OBSERVE"
    UNDERSTAND = "UNDERSTAND"
    PLAN = "PLAN"
    DECOMPOSE = "DECOMPOSE"
    SELECT_TOOLS = "SELECT_TOOLS"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    CRITIQUE = "CRITIQUE"
    CORRECT = "CORRECT"
    REPLAN = "REPLAN"
    LEARN = "LEARN"
    REPORT = "REPORT"


class UnverifiedSuccessClaim(Exception):
    """Levantado quando algo tenta marcar uma tarefa como concluída/
    verificada sem fornecer evidência — nenhuma IA pode simplesmente
    declarar sucesso."""


@dataclass
class ReasoningStep:
    stage: ReasoningStage
    summary: str
    evidence: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class ReasoningTrace:
    trace_id: str
    agent: str
    objective: str
    steps: list = field(default_factory=list)
    verified: bool = False
    correction_attempts: int = 0

    @staticmethod
    def start(*, agent: str, objective: str) -> "ReasoningTrace":
        trace = ReasoningTrace(trace_id=f"trace_{uuid.uuid4().hex[:12]}", agent=agent, objective=objective)
        trace.record(ReasoningStage.OBSERVE, f"objetivo recebido: {objective}")
        return trace

    def record(self, stage: ReasoningStage, summary: str, evidence: dict | None = None) -> ReasoningStep:
        step = ReasoningStep(stage=stage, summary=summary, evidence=evidence or {})
        self.steps.append(step)
        return step

    def mark_verified(self, *, evidence: dict, summary: str = "verificado com evidência") -> None:
        if not evidence:
            raise UnverifiedSuccessClaim(
                f"trace '{self.trace_id}' ({self.agent}): tentativa de marcar verificado "
                "sem evidência — nunca é permitido declarar sucesso sem prova"
            )
        self.verified = True
        self.record(ReasoningStage.VERIFY, summary, evidence)

    def record_correction(self, *, summary: str, evidence: dict | None = None) -> None:
        self.correction_attempts += 1
        self.record(ReasoningStage.CORRECT, summary, evidence)

    def steps_by_stage(self, stage: ReasoningStage) -> list:
        return [s for s in self.steps if s.stage == stage]
