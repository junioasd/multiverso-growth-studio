"""
core.agents.runtime.libra
=============================
BLOCO FINAL 1 (item 1) — LIBRA: agente REAL de curadoria da
BIBLIOTECA MESTRE. Wrapper fino sobre `core.library.master_library.
MasterLibrary` (já real, já persistido) — nunca reimplementa a lógica
de validação/revisão; nunca declara uma entrada `VALIDATED` sem que
`MasterLibrary.validate()` tenha sido genuinamente chamado por uma
autoridade.
"""

from __future__ import annotations

from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability
from core.library.master_library import LibraryEntryNotFound, MasterLibrary, RevisionNotAllowed


class LibraAgent(RealAgentContract):
    agent_id = "LIBRA"
    primary_task_types = frozenset({"ADD_ENTRY", "VALIDATE_ENTRY", "SEARCH_ENTRIES", "PROPOSE_REVISION"})
    all_rounder_task_types = frozenset({"SEARCH_ENTRIES"})
    max_input_items = 200

    def __init__(self, *, library: MasterLibrary | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.library = library

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if self.library is None:
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type=task_type, reason="nenhuma MasterLibrary real conectada",
            )
        if task_type == "ADD_ENTRY":
            return self._add_entry(payload)
        if task_type == "VALIDATE_ENTRY":
            return self._validate_entry(payload)
        if task_type == "SEARCH_ENTRIES":
            return self._search_entries(payload)
        if task_type == "PROPOSE_REVISION":
            return self._propose_revision(payload)
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _add_entry(self, payload: dict) -> AgentTaskResult:
        required = ("title", "area", "content", "origin", "responsible_agent")
        missing = [k for k in required if k not in payload]
        if missing:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="ADD_ENTRY", reason=f"campos obrigatórios ausentes: {missing}",
            )
        entry = self.library.add(
            title=payload["title"], area=payload["area"], content=payload["content"],
            origin=payload["origin"], responsible_agent=payload["responsible_agent"], tags=payload.get("tags"),
        )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="ADD_ENTRY", output={"entry_id": entry.entry_id, "validity": entry.validity.value},
            evidence_ids=[entry.entry_id], confidence=1.0,
        )

    def _validate_entry(self, payload: dict) -> AgentTaskResult:
        if "entry_id" not in payload or "validated_by" not in payload:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="VALIDATE_ENTRY", reason="entry_id e validated_by são obrigatórios",
            )
        try:
            entry = self.library.validate(payload["entry_id"], validated_by=payload["validated_by"])
        except LibraryEntryNotFound as exc:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type="VALIDATE_ENTRY", reason=str(exc))
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="VALIDATE_ENTRY",
            output={"entry_id": entry.entry_id, "validity": entry.validity.value},
            evidence_ids=[entry.entry_id], confidence=1.0,
        )

    def _search_entries(self, payload: dict) -> AgentTaskResult:
        entries = self.library.search(
            area=payload.get("area"), tag=payload.get("tag"), only_validated=payload.get("only_validated", True),
        )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="SEARCH_ENTRIES",
            output={"entries": [{"entry_id": e.entry_id, "title": e.title, "validity": e.validity.value} for e in entries]},
            evidence_ids=[e.entry_id for e in entries], confidence=1.0,
        )

    def _propose_revision(self, payload: dict) -> AgentTaskResult:
        required = ("entry_id", "new_content", "proposed_by")
        missing = [k for k in required if k not in payload]
        if missing:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="PROPOSE_REVISION", reason=f"campos obrigatórios ausentes: {missing}",
            )
        try:
            revision = self.library.propose_revision(
                payload["entry_id"], new_content=payload["new_content"], proposed_by=payload["proposed_by"],
            )
        except (LibraryEntryNotFound, RevisionNotAllowed) as exc:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type="PROPOSE_REVISION", reason=str(exc))
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="PROPOSE_REVISION",
            output={"revision_entry_id": revision.entry_id, "version": revision.version},
            evidence_ids=[revision.entry_id], confidence=1.0,
        )


def register_libra_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="LIBRA", capability=Capability.REASONING, level=CompetencyLevel.MASTER,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
