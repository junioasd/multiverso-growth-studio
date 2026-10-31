"""
core.coordination.delegation
================================
CORREÇÃO (item 1 — agentic-first: executor deve respeitar o agente
designado).

Antes desta correção, `AtlasDirector.run_mission_with_reasoning()`
sempre executava via NAVIGATOR, mesmo quando `mission.agent="FORGE"` —
a missão terminava `VERIFIED` como se FORGE tivesse executado. Isso
nunca pode acontecer.

`DelegationRegistry` é o ÚNICO mecanismo pelo qual um agente diferente
do `mission.agent` designado pode executar uma tarefa — sempre
explícito, sempre auditável (`original_agent`, `delegated_agent`,
`reason`, `permissions`, `capabilities`, `task_scope`, `evidence`).
Sem uma `DelegationRecord` válida, `resolve_authorized_executor()`
levanta `InvalidExecutorError` — nunca silenciosamente permite.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


class InvalidExecutorError(Exception):
    """Levantado quando o agente que tentaria executar uma missão/
    tarefa NÃO é o `mission.agent` designado e NÃO existe uma
    delegação válida — a execução NUNCA prossegue silenciosamente."""


@dataclass
class DelegationRecord:
    delegation_id: str
    mission_id: str
    task_id: str
    original_agent: str
    delegated_agent: str
    reason: str
    permissions: list = field(default_factory=list)
    capabilities: list = field(default_factory=list)
    task_scope: str = ""
    evidence: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def new_id() -> str:
        return f"deleg_{uuid.uuid4().hex[:12]}"


class DelegationRegistry:
    def __init__(self) -> None:
        self._records: list = []

    def delegate(
        self, *, mission_id: str, task_id: str, original_agent: str, delegated_agent: str, reason: str,
        permissions: list | None = None, capabilities: list | None = None, task_scope: str = "",
        evidence: dict | None = None,
    ) -> DelegationRecord:
        if not reason or not reason.strip():
            raise ValueError("delegação exige `reason` explícito — nunca delegação silenciosa")
        record = DelegationRecord(
            delegation_id=DelegationRecord.new_id(), mission_id=mission_id, task_id=task_id,
            original_agent=original_agent, delegated_agent=delegated_agent, reason=reason,
            permissions=list(permissions or []), capabilities=list(capabilities or []), task_scope=task_scope,
            evidence=dict(evidence or {}),
        )
        self._records.append(record)
        return record

    def find_valid_delegation(self, *, mission_id: str, task_id: str, executing_agent: str) -> DelegationRecord | None:
        for record in reversed(self._records):  # a delegação mais recente prevalece
            if record.mission_id == mission_id and record.task_id == task_id and record.delegated_agent == executing_agent:
                return record
        return None

    def for_mission(self, mission_id: str, task_id: str) -> list:
        return [r for r in self._records if r.mission_id == mission_id and r.task_id == task_id]

    def all(self) -> list:
        return list(self._records)


def resolve_authorized_executor(
    *, designated_agent: str, executing_agent: str, mission_id: str, task_id: str,
    delegation_registry: DelegationRegistry,
) -> str:
    """Devolve `'DIRECT'` quando `executing_agent` É o agente designado
    da missão, ou `'DELEGATED'` quando existe uma `DelegationRecord`
    válida autorizando `executing_agent` a executar em nome do
    designado. Levanta `InvalidExecutorError` em qualquer outro caso —
    NUNCA permite `VERIFIED` com um executor incompatível e sem
    delegação."""
    if designated_agent == executing_agent:
        return "DIRECT"
    delegation = delegation_registry.find_valid_delegation(
        mission_id=mission_id, task_id=task_id, executing_agent=executing_agent,
    )
    if delegation is not None:
        return "DELEGATED"
    raise InvalidExecutorError(
        f"missão ({mission_id}, {task_id}) designada para '{designated_agent}' não pode ser executada por "
        f"'{executing_agent}' sem delegação explícita e auditável — DELEGATION_REQUIRED"
    )
