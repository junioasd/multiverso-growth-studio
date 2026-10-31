"""
core.reasoning.adapters
===========================
CORREÇÃO (item 4): adapta o `ReasoningEngine` genérico para executores
REAIS já existentes (NAVIGATOR, FORGE) — nenhum resultado é inventado;
o `ReasoningEngine.executor` chama o executor de verdade, e o
`Verifier` só confirma sucesso com base no resultado GENUÍNO devolvido.
"""

from __future__ import annotations

from forge.engineer import ForgeEngineer
from forge.models import DiagnosticStatus
from navigator.executor import NavigatorExecutor
from navigator.models import ExecutionResult, NavigatorAction


class NavigatorTaskExecutor:
    """Adapta `NavigatorExecutor.execute()` para a interface `Executor`
    do `ReasoningEngine`. Cada `task` precisa trazer `platform`,
    `action` (um `NavigatorAction`) e opcionalmente `payload`.

    CORREÇÃO (item 1 — tools_allowed de ponta a ponta): `tools_allowed`
    é propagado desta camada (ATLAS/coordenação multiagente) até o
    NAVIGATOR/BRIDGE — fecha a cadeia
    ATLAS -> MULTIAGENT COORDINATION -> NAVIGATOR -> BRIDGE ->
    EXECUTION PATH -> ADAPTER/EXECUTOR REAL."""

    def __init__(
        self, navigator: NavigatorExecutor, *, mission_id: str | None, task_id: str | None,
        channel_id: str | None = None, account_context: str | None = None, tools_allowed: list | None = None,
    ) -> None:
        self.navigator = navigator
        self.mission_id = mission_id
        self.task_id = task_id
        self.channel_id = channel_id
        self.account_context = account_context
        self.tools_allowed = tools_allowed

    def execute(self, *, task: dict, tool: str | None) -> dict:
        entry = self.navigator.execute(
            platform=task["platform"], action=task["action"], payload=task.get("payload", {}),
            mission_id=self.mission_id, task_id=self.task_id, channel_id=self.channel_id,
            account_context=self.account_context, tools_allowed=self.tools_allowed,
            execution_id=task.get("execution_id"), attempt_id=task.get("attempt_id"),
            retry_safe_override=task.get("retry_safe"),
        )
        return {
            "execution_result": entry.result, "evidence": entry.evidence, "entry_id": entry.entry_id,
            "error": entry.error, "approval_used": entry.approval_used, "attempt_id": entry.attempt_id,
            "execution_id": entry.execution_id,
        }


class NavigatorResultVerifier:
    """Só verifica SUCCESS genuíno — ESCALATED/BLOCKED/FAILED nunca são
    tratados como verificados (evita declarar sucesso sobre uma ação
    ainda pendente de aprovação, ou que falhou)."""

    def verify(self, *, task: dict, result: dict) -> bool:
        """CORREÇÃO PÓS-AUDITORIA (bloqueador A — reproduzido): SUCCESS
        vindo de `MOCK_FALLBACK` (modo MOCK, "nenhuma chamada real") era
        verificado e a missão terminava VERIFIED sem efeito externo real.
        Simulação NUNCA é verificação de execução real."""
        if result.get("execution_result") != ExecutionResult.SUCCESS.value:
            return False
        evidence = result.get("evidence") or {}
        if evidence.get("mode") == "MOCK" or evidence.get("selected_mechanism") == "MOCK_FALLBACK":
            return False
        return True


class ForgeDiagnosticExecutor:
    """Adapta o pipeline do FORGE para a interface `Executor` — a
    `task` precisa trazer `issue_summary` e opcionalmente `evidence`;
    devolve o `diagnostic_id` e o status alcançado (o ReasoningEngine
    não avança o pipeline sozinho além de `detect()` — as demais
    etapas continuam exigindo autorização explícita, conforme
    `forge.engineer`)."""

    def __init__(self, forge: ForgeEngineer) -> None:
        self.forge = forge

    def execute(self, *, task: dict, tool: str | None) -> dict:
        diagnostic = self.forge.detect(
            issue_summary=task.get("issue_summary", "diagnóstico via ciclo de raciocínio"),
            evidence=task.get("evidence", {}),
        )
        return {"diagnostic_id": diagnostic.diagnostic_id, "status": diagnostic.status.value}


class ForgeDetectionVerifier:
    def verify(self, *, task: dict, result: dict) -> bool:
        return result.get("status") == DiagnosticStatus.DETECTED.value
