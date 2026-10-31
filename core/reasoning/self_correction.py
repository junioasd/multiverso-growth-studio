"""
core.reasoning.self_correction
==================================
EXPANSÃO — item 11 (autocorreção controlada).

Fluxo obrigatório quando um agente falha:
    identificar erro -> classificar erro -> verificar correção segura
    -> tentar estratégia alternativa -> validar resultado
    -> registrar aprendizado.

Limite de tentativas explícito — NUNCA loop infinito. Depois do limite,
escala (para FORGE, ATLAS ou Owner, conforme a natureza da falha) em
vez de continuar tentando silenciosamente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.reasoning.cycle import ReasoningStage, ReasoningTrace


class ErrorClass(str, Enum):
    TRANSIENT = "TRANSIENT"          # provavelmente some sozinho num retry (timeout, rate limit)
    LOGIC = "LOGIC"                   # erro na abordagem — precisa de estratégia alternativa
    PERMISSION = "PERMISSION"         # falta de autoridade — nunca contornável por retry
    DATA = "DATA"                     # dado ausente/inconsistente — precisa de evidência melhor
    TECHNICAL = "TECHNICAL"           # bug/infra — candidato a escalar para FORGE
    UNKNOWN = "UNKNOWN"


class RetryLimitExceeded(Exception):
    """Levantado quando o limite de tentativas é atingido — o chamador
    DEVE escalar (FORGE/ATLAS/Owner), nunca continuar tentando."""


def classify_error(exc: Exception) -> ErrorClass:
    """Classificação best-effort e conservadora — na dúvida, UNKNOWN
    (tratado com a mesma cautela de um erro técnico, nunca otimista)."""
    name = type(exc).__name__.lower()
    if "permission" in name or "accessdenied" in name or "authority" in name or "hierarchy" in name:
        return ErrorClass.PERMISSION
    if "timeout" in name or "temporar" in str(exc).lower() or "ratelimit" in name:
        return ErrorClass.TRANSIENT
    if "notfound" in name or "keyerror" in name or "valueerror" in name:
        return ErrorClass.DATA
    if "runtimeerror" in name or "ioerror" in name or "oserror" in name or "sqlite" in name:
        return ErrorClass.TECHNICAL
    return ErrorClass.UNKNOWN


@dataclass
class SelfCorrectionOutcome:
    succeeded: bool
    attempts: int
    error_class: ErrorClass | None
    escalate: bool
    escalate_reason: str | None = None
    result: object = None


@dataclass
class SelfCorrectionLoop:
    """`strategies`: lista de callables (sem argumento) tentados em
    ordem — cada um representa uma "estratégia alternativa". Nunca
    tenta indefinidamente: para em `max_attempts` (contando a tentativa
    original) e sinaliza escalonamento."""

    max_attempts: int = 3

    def run(self, trace: ReasoningTrace, strategies: list) -> SelfCorrectionOutcome:
        if not strategies:
            raise ValueError("pelo menos uma estratégia (a tentativa original) é obrigatória")

        last_error_class: ErrorClass | None = None
        attempts = 0

        for attempt_index, strategy in enumerate(strategies[: self.max_attempts]):
            attempts += 1
            try:
                result = strategy()
            except Exception as exc:  # cada falha é classificada e registrada, nunca silenciosa
                last_error_class = classify_error(exc)
                trace.record_correction(
                    summary=f"tentativa {attempts} falhou ({last_error_class.value}): {exc}",
                    evidence={"attempt": attempts, "error_class": last_error_class.value},
                )
                if last_error_class == ErrorClass.PERMISSION:
                    # nunca contornável por retry/estratégia alternativa —
                    # escala imediatamente, sem gastar as tentativas restantes.
                    return SelfCorrectionOutcome(
                        succeeded=False, attempts=attempts, error_class=last_error_class,
                        escalate=True, escalate_reason="falha de permissão — requer decisão de autoridade",
                    )
                continue
            else:
                if attempts > 1:
                    # correção genuína (uma tentativa ANTERIOR falhou e
                    # esta recuperou) — informação que só o
                    # self_correction tem, nunca duplicada pelo
                    # chamador. Na primeira tentativa bem-sucedida
                    # (attempts == 1) nada é gravado aqui — o chamador
                    # (ex: `ReasoningEngine`) já registra EXECUTE com
                    # mais contexto (ex: número da rodada de replan).
                    trace.record(
                        ReasoningStage.CORRECT, f"tentativa {attempts} bem-sucedida", {"attempt": attempts},
                    )
                return SelfCorrectionOutcome(
                    succeeded=True, attempts=attempts, error_class=None, escalate=False, result=result,
                )

        # esgotou as estratégias disponíveis (ou o limite) sem sucesso
        reason = (
            "limite de tentativas esgotado — escalado para revisão "
            f"(última classe de erro: {last_error_class.value if last_error_class else 'UNKNOWN'})"
        )
        trace.record(ReasoningStage.REPORT, reason, {"attempts": attempts})
        return SelfCorrectionOutcome(
            succeeded=False, attempts=attempts, error_class=last_error_class,
            escalate=True, escalate_reason=reason,
        )
