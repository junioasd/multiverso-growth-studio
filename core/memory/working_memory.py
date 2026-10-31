"""
core.memory.working_memory
==============================
EXPANSÃO — item 6 (memória de trabalho e aprendizado).

Separa explicitamente:
- memória de CURTO PRAZO (volátil, por sessão de trabalho de um agente)
- CONTEXTO DA TAREFA (ligado a uma mission_id/task_id específica)
- memória OPERACIONAL (fatos persistentes sobre o funcionamento do
  sistema — ex: "endpoint X costuma expirar em 30s")
- HISTÓRICO DE DECISÕES (o que foi decidido, por quem, quando, por quê)
- APRENDIZADOS VALIDADOS (só entram aqui depois de um processo de
  validação — nunca um aprendizado cru vira "conhecimento" sozinho)

Tudo passa por `sanitize_dict` antes de armazenar — nunca guarda senha,
segredo, token em texto puro ou dado sensível desnecessário (mesma
função já usada em `atlas/escalation.py` e no restante do sistema,
reaproveitada aqui sem duplicar lógica de sanitização).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.security.sanitize import sanitize_dict


@dataclass
class MemoryEntry:
    key: str
    value: dict
    recorded_at: float = field(default_factory=time.time)


class WorkingMemory:
    def __init__(self) -> None:
        # curto prazo: volátil, esvaziada por `clear_short_term()` —
        # nunca persistida em disco.
        self._short_term: dict[str, MemoryEntry] = {}
        # contexto de tarefa: por (mission_id, task_id)
        self._task_context: dict[tuple, dict] = {}
        # operacional: fatos duradouros sobre o funcionamento do sistema
        self._operational: dict[str, MemoryEntry] = {}
        # histórico de decisões: lista append-only
        self._decision_history: list[dict] = []
        # aprendizados validados: só populado via `validate_learning()`
        self._validated_learnings: dict[str, MemoryEntry] = {}
        # candidatos a aprendizado, ainda não validados
        self._pending_learnings: dict[str, MemoryEntry] = {}

    # ------------------------------------------------------------------
    # Curto prazo
    # ------------------------------------------------------------------

    def remember_short_term(self, key: str, value: dict) -> None:
        self._short_term[key] = MemoryEntry(key=key, value=sanitize_dict(value))

    def recall_short_term(self, key: str) -> dict | None:
        entry = self._short_term.get(key)
        return entry.value if entry else None

    def clear_short_term(self) -> None:
        self._short_term.clear()

    # ------------------------------------------------------------------
    # Contexto de tarefa
    # ------------------------------------------------------------------

    def set_task_context(self, mission_id: str, task_id: str, context: dict) -> None:
        self._task_context[(mission_id, task_id)] = sanitize_dict(context)

    def get_task_context(self, mission_id: str, task_id: str) -> dict | None:
        return self._task_context.get((mission_id, task_id))

    # ------------------------------------------------------------------
    # Operacional
    # ------------------------------------------------------------------

    def record_operational_fact(self, key: str, value: dict) -> None:
        self._operational[key] = MemoryEntry(key=key, value=sanitize_dict(value))

    def get_operational_fact(self, key: str) -> dict | None:
        entry = self._operational.get(key)
        return entry.value if entry else None

    # ------------------------------------------------------------------
    # Histórico de decisões
    # ------------------------------------------------------------------

    def record_decision(self, *, agent: str, decision: str, reason: str, context: dict | None = None) -> None:
        self._decision_history.append({
            "agent": agent, "decision": decision, "reason": reason,
            "context": sanitize_dict(context or {}), "recorded_at": time.time(),
        })

    def decision_history(self) -> list:
        return list(self._decision_history)

    # ------------------------------------------------------------------
    # Aprendizado — só vira conhecimento reutilizável após validação
    # ------------------------------------------------------------------

    def propose_learning(self, key: str, value: dict) -> None:
        self._pending_learnings[key] = MemoryEntry(key=key, value=sanitize_dict(value))

    def validate_learning(self, key: str, *, validated_by: str) -> MemoryEntry:
        if key not in self._pending_learnings:
            raise KeyError(f"nenhum aprendizado pendente com a chave '{key}'")
        entry = self._pending_learnings.pop(key)
        entry.value = {**entry.value, "_validated_by": validated_by, "_validated_at": time.time()}
        self._validated_learnings[key] = entry
        return entry

    def get_validated_learning(self, key: str) -> dict | None:
        entry = self._validated_learnings.get(key)
        return entry.value if entry else None

    def pending_learnings(self) -> list:
        return list(self._pending_learnings.values())

    def validated_learnings(self) -> list:
        return list(self._validated_learnings.values())
