"""
atlas.advanced_planning
==========================
EXPANSÃO — item 4 (planejador avançado).

Fortalece o planejamento além do checklist simples de
`atlas.planning.ObjectivePlanner` (preservado, inalterado). Suporta
subobjetivos/tarefas com dependências, execução paralela/sequencial,
prioridade, prazo, recursos, ferramentas, riscos, critérios de
conclusão, fallback, retry, rollback, checkpoints e referência a
Quality Gate — e pode ser RECALCULADO durante a execução
(`replan()`) quando surge informação nova.

Puramente estrutural: nenhuma IA "de verdade" aqui — organiza dados
que o ATLAS (ou um especialista) usa para decidir a ordem de trabalho.
Nunca inventa prazos/recursos que o chamador não informou.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


class CyclicDependencyError(Exception):
    """Levantado quando as dependências entre tarefas formam um ciclo —
    nunca é possível ordenar um plano assim."""


class UnknownDependencyError(Exception):
    pass


@dataclass
class PlanTask:
    task_id: str
    description: str
    depends_on: list = field(default_factory=list)
    priority: int = 3  # 1 (mais alta) .. 5 (mais baixa)
    deadline: float | None = None
    resources: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    completion_criteria: list = field(default_factory=list)
    fallback: str | None = None
    max_retries: int = 3
    checkpoints: list = field(default_factory=list)
    quality_gate: str | None = None


@dataclass
class AdvancedPlan:
    plan_id: str
    objective: str
    tasks: dict = field(default_factory=dict)  # task_id -> PlanTask
    created_at: float = field(default_factory=time.time)
    revision: int = 0

    def add_task(self, task: PlanTask) -> None:
        for dep in task.depends_on:
            if dep not in self.tasks and dep != task.task_id:
                # dependência pode ainda não existir se for adicionada
                # fora de ordem — validação completa ocorre em
                # `topological_order()`.
                pass
        self.tasks[task.task_id] = task

    def topological_order(self) -> list:
        """Ordena respeitando dependências (Kahn). Levanta
        `CyclicDependencyError`/`UnknownDependencyError` em vez de
        devolver uma ordem inválida silenciosamente."""
        for task in self.tasks.values():
            for dep in task.depends_on:
                if dep not in self.tasks:
                    raise UnknownDependencyError(
                        f"tarefa '{task.task_id}' depende de '{dep}', que não existe no plano"
                    )

        in_degree = {tid: 0 for tid in self.tasks}
        dependents: dict[str, list] = {tid: [] for tid in self.tasks}
        for task in self.tasks.values():
            for dep in task.depends_on:
                in_degree[task.task_id] += 1
                dependents[dep].append(task.task_id)

        queue = sorted(
            [tid for tid, deg in in_degree.items() if deg == 0],
            key=lambda tid: self.tasks[tid].priority,
        )
        ordered = []
        while queue:
            queue.sort(key=lambda tid: self.tasks[tid].priority)
            current = queue.pop(0)
            ordered.append(self.tasks[current])
            for dependent in dependents[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(ordered) != len(self.tasks):
            raise CyclicDependencyError("dependências entre tarefas formam um ciclo — plano inválido")
        return ordered

    def parallel_groups(self) -> list:
        """Agrupa tarefas em "levels" — tarefas do mesmo grupo não
        dependem umas das outras e podem rodar em paralelo; grupos
        posteriores dependem de grupos anteriores terem concluído."""
        ordered = self.topological_order()  # valida ciclo/dependência antes
        level_of: dict[str, int] = {}
        for task in ordered:
            if not task.depends_on:
                level_of[task.task_id] = 0
            else:
                level_of[task.task_id] = 1 + max(level_of[dep] for dep in task.depends_on)

        groups: dict[int, list] = {}
        for task in ordered:
            groups.setdefault(level_of[task.task_id], []).append(task)
        return [groups[level] for level in sorted(groups)]


class AdvancedPlanner:
    def build_plan(self, *, objective: str, tasks: list) -> AdvancedPlan:
        """`tasks`: lista de dicts com pelo menos `task_id`/`description`
        — demais campos opcionais, mapeados diretamente para `PlanTask`."""
        if not objective or not objective.strip():
            raise ValueError("objetivo vazio — não é possível planejar")
        plan = AdvancedPlan(plan_id=f"advplan_{uuid.uuid4().hex[:12]}", objective=objective.strip())
        for spec in tasks:
            plan.add_task(PlanTask(**spec))
        plan.topological_order()  # valida o plano inteiro na construção
        return plan

    def replan(self, plan: AdvancedPlan, *, add_tasks: list | None = None, remove_task_ids: list | None = None) -> AdvancedPlan:
        """Recalcula o plano quando surge informação nova durante a
        execução — adiciona e/ou remove tarefas, preservando as
        demais. Nunca remove uma tarefa da qual outra ainda depende sem
        também removê-la explicitamente (isso levantaria
        `UnknownDependencyError` na validação)."""
        for task_id in remove_task_ids or []:
            plan.tasks.pop(task_id, None)
        for spec in add_tasks or []:
            plan.add_task(PlanTask(**spec))
        plan.revision += 1
        plan.topological_order()  # revalida após a mudança
        return plan
