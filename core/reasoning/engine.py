"""
core.reasoning.engine
========================
CORREÇÃO (item 4 — raciocínio: integração operacional real).

A camada de raciocínio deixa de ser apenas um registro passivo
(`ReasoningTrace`) e passa a ser uma camada OPERACIONAL que DECOMPÕE
objetivos, SELECIONA ferramentas, EXECUTA (via um executor real
injetável — NAVIGATOR, FORGE, ou qualquer outro), VERIFICA o
resultado, CRITICA, e aciona autocorreção/replanejamento — sem alegar
consciência, AGI real ou "raciocínio mágico".

Interfaces SEPARADAS (Planner, ToolSelector, Executor, Verifier,
Critic) — cada uma pode ser trocada por uma implementação real no
futuro. Quando nenhum provider/model real está configurado, as
implementações padrão são DETERMINÍSTICAS e claramente rotuladas MOCK
— nunca inventam decisão, subtarefa ou resultado: usam exatamente o
que o chamador forneceu (tarefas pré-definidas, critérios explícitos),
e um executor real de verdade (quando injetado) produz o único
resultado genuíno.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from core.reasoning.cycle import ReasoningStage, ReasoningTrace
from core.reasoning.self_correction import SelfCorrectionLoop


class Planner(Protocol):
    def decompose(self, *, objective: str, context: dict) -> list: ...


class ToolSelector(Protocol):
    def select(self, *, task: dict, available_tools: list) -> str | None: ...


class Executor(Protocol):
    def execute(self, *, task: dict, tool: str | None) -> dict: ...


class Verifier(Protocol):
    def verify(self, *, task: dict, result: dict) -> bool: ...


class Critic(Protocol):
    def critique(self, *, task: dict, result: dict, verified: bool) -> str: ...


class FixedTaskPlanner:
    """DETERMINÍSTICO — nunca inventa subtarefas: usa EXATAMENTE a
    lista de tarefas fornecida na construção (ou em
    `context['predefined_tasks']`), ou, na ausência de qualquer uma
    das duas, decompõe em uma única tarefa "executar o objetivo
    diretamente" (o mínimo honesto, sem fingir decomposição real)."""

    def __init__(self, tasks: list | None = None) -> None:
        self.tasks = tasks

    def decompose(self, *, objective: str, context: dict) -> list:
        if self.tasks is not None:
            return list(self.tasks)
        predefined = context.get("predefined_tasks")
        if predefined:
            return list(predefined)
        return [{"task_id": "t1", "description": objective}]


class PreferredToolSelector:
    """DETERMINÍSTICO — usa `task['preferred_tool']` se disponível
    entre `available_tools`; senão a primeira disponível; nunca
    inventa uma ferramenta que não foi oferecida."""

    def select(self, *, task: dict, available_tools: list) -> str | None:
        preferred = task.get("preferred_tool")
        if preferred and preferred in available_tools:
            return preferred
        return available_tools[0] if available_tools else None


class NullExecutor:
    """MOCK explícito — usado só quando nenhum executor real
    (NAVIGATOR/FORGE/outro) é injetado. Nunca finge um resultado além
    de rotular claramente que nada real foi executado."""

    def execute(self, *, task: dict, tool: str | None) -> dict:
        return {"mode": "MOCK", "note": "nenhum executor real conectado a este ciclo de raciocínio"}


class ChecklistVerifier:
    """Verifica contra `task['completion_criteria']` (lista de chaves
    que devem estar presentes e truthy em `result`) — sem critério
    definido, só confirma que HÁ resultado (nunca declara verificado
    sem nada para checar)."""

    def verify(self, *, task: dict, result: dict) -> bool:
        criteria = task.get("completion_criteria") or []
        if not criteria:
            return bool(result)
        return all(result.get(c) for c in criteria)


class SimpleCritic:
    def critique(self, *, task: dict, result: dict, verified: bool) -> str:
        if verified:
            return f"tarefa '{task.get('task_id')}': resultado atende aos critérios definidos"
        return f"tarefa '{task.get('task_id')}': resultado NÃO atende aos critérios — replanejamento necessário"


@dataclass
class ReasoningEngine:
    """Orquestra o ciclo OBSERVE -> UNDERSTAND -> PLAN -> DECOMPOSE ->
    SELECT_TOOLS -> EXECUTE -> VERIFY -> CRITIQUE -> CORRECT -> REPLAN
    -> REEXECUTE -> VERIFY (quando necessário) -> LEARN -> REPORT.
    Componentes trocáveis — padrão determinístico/MOCK, mas qualquer
    um pode ser substituído por uma integração real (ex:
    `core.reasoning.adapters.NavigatorTaskExecutor` para rodar de
    verdade via NAVIGATOR).

    CORREÇÃO (microcorreção final — item 3): REPLAN deixa de ser
    apenas um registro textual — quando VERIFY falha, o motor
    genuinamente critica, replaneja (tenta uma ferramenta diferente
    dentre as ainda permitidas) e REEXECUTA, até `max_replan_attempts`
    (nunca loop infinito). Cada tentativa fica registrada
    separadamente no `ReasoningTrace`. Ao esgotar o limite: FAILED
    (`verified=False`) — quando a falha é TÉCNICA (exceção) em vez de
    apenas "não verificado", o resultado fica `escalated=True` (via
    `SelfCorrectionLoop`, que já distingue erro de permissão —
    nunca contornável por replan — de erro técnico/transitório)."""

    planner: Planner = field(default_factory=FixedTaskPlanner)
    tool_selector: ToolSelector = field(default_factory=PreferredToolSelector)
    executor: Executor = field(default_factory=NullExecutor)
    verifier: Verifier = field(default_factory=ChecklistVerifier)
    critic: Critic = field(default_factory=SimpleCritic)
    self_correction: SelfCorrectionLoop = field(default_factory=SelfCorrectionLoop)
    max_replan_attempts: int = 3

    def run(
        self, *, agent: str, objective: str, context: dict | None = None, available_tools: list | None = None,
    ) -> tuple:
        context = context or {}
        available_tools = list(available_tools or [])
        trace = ReasoningTrace.start(agent=agent, objective=objective)
        trace.record(ReasoningStage.UNDERSTAND, f"contexto recebido com {len(context)} chave(s)")

        subtasks = self.planner.decompose(objective=objective, context=context)
        trace.record(ReasoningStage.PLAN, f"{len(subtasks)} subtarefa(s) planejada(s)")
        trace.record(ReasoningStage.DECOMPOSE, f"subtarefas: {[t.get('task_id') for t in subtasks]}")

        results = [self._run_task_with_replan(trace=trace, task=task, available_tools=available_tools) for task in subtasks]

        trace.record(ReasoningStage.LEARN, "ciclo concluído — resultados disponíveis para aprendizado validado")
        trace.record(ReasoningStage.REPORT, f"{len(results)} tarefa(s) processada(s)")
        return trace, results

    def _run_task_with_replan(self, *, trace: ReasoningTrace, task: dict, available_tools: list) -> dict:
        """Loop REPLAN -> REEXECUTE -> VERIFY, limitado a
        `max_replan_attempts` — nunca infinito. Cada rodada registra
        sua própria tentativa no trace (histórico separado por
        tentativa).

        CORREÇÃO (microcorreção V3 — item 1): gera um `execution_id`
        único para esta rodada de replan (agrupa todas as tentativas)
        e um `attempt_id` distinto por rodada
        (`<execution_id>_attempt_<n>`), injetados na tarefa passada ao
        executor — permite que integrações reais (ex:
        `core.reasoning.adapters.NavigatorTaskExecutor` -> NAVIGATOR)
        tratem cada rodada como uma tentativa GENUINAMENTE nova
        (idempotência não bloqueia retry legítimo de um FAILED
        anterior), preservando ao mesmo tempo que a MESMA tentativa
        nunca é executada duas vezes."""
        tried_tools: list = []
        current_task = task
        last_result = None
        task_id = task.get("task_id")
        execution_id = f"exec_{uuid.uuid4().hex[:10]}"

        for attempt_number in range(1, self.max_replan_attempts + 1):
            remaining_tools = [t for t in available_tools if t not in tried_tools] or available_tools
            tool = self.tool_selector.select(task=current_task, available_tools=remaining_tools)
            trace.record(
                ReasoningStage.SELECT_TOOLS,
                f"tentativa {attempt_number}/{self.max_replan_attempts} — ferramenta para '{task_id}': "
                f"{tool or 'nenhuma disponível'}",
            )
            if tool is not None:
                tried_tools.append(tool)

            attempt_id = f"{execution_id}_attempt_{attempt_number}"
            task_with_attempt = {**current_task, "execution_id": execution_id, "attempt_id": attempt_id}

            def attempt(task=task_with_attempt, tool=tool):
                return self.executor.execute(task=task, tool=tool)

            # dentro de CADA tentativa de replan, o self_correction ainda
            # cobre retries TÉCNICOS/transitórios (respeitando seu
            # próprio limite — nunca somado ilimitadamente ao replan).
            strategies = [attempt] * self.self_correction.max_attempts
            outcome = self.self_correction.run(trace, strategies)

            if not outcome.succeeded:
                if outcome.error_class is not None and outcome.error_class.value == "PERMISSION":
                    # erro de permissão nunca é contornável por replan —
                    # escala imediatamente, sem gastar rodadas restantes.
                    trace.record(
                        ReasoningStage.REPORT,
                        f"tarefa '{task_id}' escalada na tentativa {attempt_number}: {outcome.escalate_reason}",
                    )
                    return {
                        "task": task, "result": None, "verified": False, "escalated": True,
                        "attempts": attempt_number,
                    }
                if attempt_number < self.max_replan_attempts:
                    trace.record(
                        ReasoningStage.REPLAN,
                        f"tentativa {attempt_number} falhou tecnicamente ({outcome.escalate_reason}) — "
                        "replanejando com nova estratégia",
                    )
                    current_task = self._adjust_task_for_replan(current_task)
                    continue
                trace.record(
                    ReasoningStage.REPORT,
                    f"tarefa '{task_id}' escalada após {attempt_number} tentativa(s): {outcome.escalate_reason}",
                )
                return {
                    "task": task, "result": None, "verified": False, "escalated": True, "attempts": attempt_number,
                }

            result = outcome.result
            last_result = result
            trace.record(ReasoningStage.EXECUTE, f"tentativa {attempt_number}: tarefa '{task_id}' executada")

            verified = self.verifier.verify(task=current_task, result=result)
            feedback = self.critic.critique(task=current_task, result=result, verified=verified)
            trace.record(ReasoningStage.CRITIQUE, feedback)

            if verified:
                trace.mark_verified(evidence={"task_id": task_id, "result": result, "attempts": attempt_number})
                return {
                    "task": task, "result": result, "verified": True, "escalated": False, "attempts": attempt_number,
                }

            # CORREÇÃO PÓS-AUDITORIA: um resultado que o executor JÁ reportou
            # como SUCCESS mas que não pôde ser verificado (ex.: simulação MOCK)
            # NUNCA é re-executado — repetir ação dada como concluída é vetor de
            # duplicação externa, e repetir simulação não a torna real.
            if isinstance(result, dict) and result.get("execution_result") == "SUCCESS":
                trace.record(
                    ReasoningStage.REPORT,
                    f"tarefa '{task_id}' executada mas NÃO verificável (ex.: simulação) — não será repetida",
                )
                return {"task": task, "result": result, "verified": False, "escalated": False,
                        "attempts": attempt_number}

            if attempt_number < self.max_replan_attempts:
                trace.record(
                    ReasoningStage.REPLAN,
                    f"tentativa {attempt_number}: verificação falhou — replanejando (nova ferramenta quando disponível)",
                )
                current_task = self._adjust_task_for_replan(current_task)
            else:
                trace.record(
                    ReasoningStage.REPORT,
                    f"tarefa '{task_id}' não verificada após {attempt_number} tentativa(s) de replanejamento",
                )

        return {
            "task": task, "result": last_result, "verified": False, "escalated": False,
            "attempts": self.max_replan_attempts,
        }

    @staticmethod
    def _adjust_task_for_replan(task: dict) -> dict:
        """Ajuste MÍNIMO e honesto entre rodadas de replan: remove a
        ferramenta preferida anterior (se houver) para que a próxima
        seleção considere uma alternativa dentre `available_tools` —
        nunca inventa uma tarefa/estratégia nova do zero."""
        adjusted = dict(task)
        adjusted.pop("preferred_tool", None)
        return adjusted
