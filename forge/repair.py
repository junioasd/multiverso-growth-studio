"""
forge.repair
==============
FORGE — executor de reparo controlado (CORREÇÃO: `apply_fix()` não
pode ser apenas mudança de estado).

`RepairExecutor` opera sobre um "resource store" explícito — um mapa
nome->valor cujos nomes precisam estar em `allowed_resources` (menor
privilégio: FORGE só pode tocar recursos explicitamente permitidos, e
NUNCA silenciosamente). Suporta: plano de correção (`FixPlan`),
checkpoint/backup antes de qualquer mudança, dry-run (pré-visualização
sem aplicar), aplicação real, diff/evidência, rollback, e tratamento de
falha parcial (reverte automaticamente o que já havia sido alterado).

Este lote não toca o código-fonte do próprio sistema em runtime — o
"resource store" é fornecido pelo chamador (ex: um dict representando
configuração operacional, feature flags, ou qualquer estado que o
Owner/ATLAS autorizem a FORGE gerenciar). A interface é real e
totalmente testável; nenhuma alteração de arquivo em disco é feita
nesta entrega.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


class RepairNotPermitted(Exception):
    """Levantado quando um plano de correção toca um recurso fora de
    `allowed_resources` — FORGE nunca altera algo não autorizado."""


@dataclass
class FixPlan:
    plan_id: str
    diagnostic_id: str
    executor_name: str
    changes: dict  # resource_name -> novo_valor proposto
    notes: str = ""

    @staticmethod
    def new(*, diagnostic_id: str, executor_name: str, changes: dict, notes: str = "") -> "FixPlan":
        return FixPlan(
            plan_id=f"fixplan_{uuid.uuid4().hex[:12]}", diagnostic_id=diagnostic_id,
            executor_name=executor_name, changes=dict(changes), notes=notes,
        )


@dataclass
class RepairResult:
    plan_id: str
    applied: bool
    dry_run: bool
    backup: dict = field(default_factory=dict)
    diff: dict = field(default_factory=dict)
    error: str | None = None
    applied_at: float | None = None


class RepairExecutor:
    def __init__(self, *, resource_store: dict, allowed_resources: set) -> None:
        self.resource_store = resource_store
        self.allowed_resources = set(allowed_resources)

    def _assert_permitted(self, resource_names) -> None:
        for name in resource_names:
            if name not in self.allowed_resources:
                raise RepairNotPermitted(
                    f"recurso '{name}' não está em allowed_resources — FORGE nunca altera "
                    "algo fora do escopo explicitamente autorizado"
                )

    def checkpoint(self, resource_names) -> dict:
        """Backup explícito ANTES de qualquer mudança — sempre chamado
        internamente por `dry_run`/`apply`, mas também exposto para o
        chamador tirar um checkpoint isolado quando quiser."""
        self._assert_permitted(resource_names)
        return {name: self.resource_store.get(name) for name in resource_names}

    def dry_run(self, plan: FixPlan) -> RepairResult:
        self._assert_permitted(plan.changes.keys())
        backup = self.checkpoint(list(plan.changes.keys()))
        diff = {name: {"before": backup[name], "after": new} for name, new in plan.changes.items()}
        return RepairResult(plan_id=plan.plan_id, applied=False, dry_run=True, backup=backup, diff=diff)

    def apply(self, plan: FixPlan) -> RepairResult:
        self._assert_permitted(plan.changes.keys())
        backup = self.checkpoint(list(plan.changes.keys()))
        applied_so_far: dict = {}
        try:
            for name, new_value in plan.changes.items():
                self.resource_store[name] = new_value
                applied_so_far[name] = new_value
        except Exception as exc:
            # falha parcial: reverte TUDO que já tinha sido alterado
            # nesta tentativa, mesmo o que aplicou com sucesso antes da
            # falha — nunca deixa um estado misto.
            for name, old_value in backup.items():
                self.resource_store[name] = old_value
            return RepairResult(plan_id=plan.plan_id, applied=False, dry_run=False, backup=backup, diff={}, error=str(exc))

        diff = {name: {"before": backup[name], "after": self.resource_store[name]} for name in plan.changes}
        return RepairResult(
            plan_id=plan.plan_id, applied=True, dry_run=False, backup=backup, diff=diff, applied_at=time.time(),
        )

    def rollback(self, result: RepairResult) -> None:
        """Restaura o resource store para o estado do `backup` de um
        `RepairResult` — usado tanto para reverter uma aplicação bem-
        sucedida (decisão posterior de rollback) quanto para limpeza
        manual."""
        for name, old_value in result.backup.items():
            self.resource_store[name] = old_value
