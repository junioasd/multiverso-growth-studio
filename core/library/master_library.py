"""
core.library.master_library
===============================
EXPANSÃO — item 7 (Biblioteca Mestre).

Base central de conhecimento/processos/estratégias/padrões/
documentação/referências/aprendizados/playbooks/políticas consultável
por ATLAS e demais agentes antes de tarefas relevantes.

Cada entrada é versionada (`version`, `supersedes`) e carrega origem,
data, confiança, validade, tags, área e agente responsável. Uma nova
entrada NUNCA sobrescreve conhecimento já `VALIDATED` silenciosamente —
precisa passar por `propose_revision()` + `approve_revision()`
(processo de revisão explícito).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.models import db as db_module


class ConfidenceLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ValidityStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    VALIDATED = "VALIDATED"
    DEPRECATED = "DEPRECATED"
    REJECTED = "REJECTED"


class LibraryEntryNotFound(KeyError):
    pass


class RevisionNotAllowed(Exception):
    """Levantado ao tentar sobrescrever uma entrada VALIDATED sem passar
    pelo processo formal de revisão (`propose_revision`)."""


@dataclass
class LibraryEntry:
    entry_id: str
    title: str
    area: str
    content: str
    origin: str
    responsible_agent: str
    tags: list = field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    validity: ValidityStatus = ValidityStatus.PENDING_REVIEW
    version: int = 1
    supersedes: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # COMANDO 5/6: validade temporal (None = conhecimento sem prazo, ex.:
    # processo interno). Vencido nunca governa decisão.
    valid_until: float | None = None


class MasterLibrary:
    def __init__(self, *, db_path: str | None = None) -> None:
        self._entries: dict[str, LibraryEntry] = {}
        self._db_path = db_path
        if self._db_path:
            self._load_from_db()

    def _row_to_entry(self, row) -> LibraryEntry:
        import json
        return LibraryEntry(
            entry_id=row["entry_id"], title=row["title"], area=row["area"], content=row["content"],
            tags=json.loads(row["tags"]), origin=row["origin"], responsible_agent=row["responsible_agent"],
            confidence=ConfidenceLevel(row["confidence"]), validity=ValidityStatus(row["validity"]),
            version=row["version"], supersedes=row["supersedes"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            valid_until=row["valid_until"] if "valid_until" in row.keys() else None,
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM master_library_entries").fetchall():
                entry = self._row_to_entry(row)
                self._entries[entry.entry_id] = entry

    def _persist(self, entry: LibraryEntry) -> None:
        if not self._db_path:
            return
        import json
        with db_module.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO master_library_entries "
                "(entry_id, title, area, content, tags, origin, responsible_agent, confidence, validity, "
                " version, supersedes, created_at, updated_at, valid_until) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry.entry_id, entry.title, entry.area, entry.content, json.dumps(entry.tags, default=str),
                 entry.origin, entry.responsible_agent, entry.confidence.value, entry.validity.value,
                 entry.version, entry.supersedes, entry.created_at, entry.updated_at, entry.valid_until),
            )

    # ------------------------------------------------------------------

    def add(
        self, *, title: str, area: str, content: str, origin: str, responsible_agent: str,
        tags: list | None = None, confidence: ConfidenceLevel = ConfidenceLevel.LOW,
        valid_until: float | None = None,
    ) -> LibraryEntry:
        entry = LibraryEntry(
            entry_id=f"lib_{uuid.uuid4().hex[:12]}", title=title, area=area, content=content,
            origin=origin, responsible_agent=responsible_agent, tags=tags or [], confidence=confidence,
            valid_until=valid_until,
        )
        self._entries[entry.entry_id] = entry
        self._persist(entry)
        return entry

    def get(self, entry_id: str) -> LibraryEntry:
        if entry_id not in self._entries:
            raise LibraryEntryNotFound(entry_id)
        return self._entries[entry_id]

    def validate(self, entry_id: str, *, validated_by: str) -> LibraryEntry:
        entry = self.get(entry_id)
        entry.validity = ValidityStatus.VALIDATED
        entry.responsible_agent = validated_by
        entry.updated_at = time.time()
        self._persist(entry)
        return entry

    def propose_revision(
        self, entry_id: str, *, new_content: str, proposed_by: str,
    ) -> LibraryEntry:
        """Cria uma NOVA entrada (PENDING_REVIEW) que referencia a
        anterior via `supersedes` — nunca sobrescreve a validada
        existente diretamente."""
        original = self.get(entry_id)
        revision = LibraryEntry(
            entry_id=f"lib_{uuid.uuid4().hex[:12]}", title=original.title, area=original.area,
            content=new_content, origin=proposed_by, responsible_agent=proposed_by, tags=list(original.tags),
            confidence=ConfidenceLevel.LOW, validity=ValidityStatus.PENDING_REVIEW,
            version=original.version + 1, supersedes=original.entry_id,
        )
        self._entries[revision.entry_id] = revision
        self._persist(revision)
        return revision

    def approve_revision(self, revision_entry_id: str, *, approved_by: str) -> LibraryEntry:
        revision = self.get(revision_entry_id)
        if revision.supersedes is None:
            raise RevisionNotAllowed(f"entrada '{revision_entry_id}' não é uma revisão de nada")
        original = self.get(revision.supersedes)
        original.validity = ValidityStatus.DEPRECATED
        original.updated_at = time.time()
        self._persist(original)
        revision.validity = ValidityStatus.VALIDATED
        revision.responsible_agent = approved_by
        revision.updated_at = time.time()
        self._persist(revision)
        return revision

    def search(self, *, area: str | None = None, tag: str | None = None, only_validated: bool = True) -> list:
        results = list(self._entries.values())
        if area is not None:
            results = [e for e in results if e.area == area]
        if tag is not None:
            results = [e for e in results if tag in e.tags]
        if only_validated:
            results = [e for e in results if e.validity == ValidityStatus.VALIDATED]
        return results

    def governing(self, *, area: str, topic: str, now: float | None = None) -> dict:
        """COMANDO 5/6 — o que pode GOVERNAR uma decisão sobre `topic`
        (tag `topic:<x>`): só VALIDATED, não vencido e não substituído
        por versão validada mais nova. Duas entradas vigentes com conteúdo
        diferente => CONFLICT (nenhuma governa)."""
        now = time.time() if now is None else now
        tag = f"topic:{topic}"
        candidates = [e for e in self._entries.values() if e.area == area and tag in e.tags]
        superseded = {e.supersedes for e in candidates if e.supersedes and e.validity == ValidityStatus.VALIDATED}
        expired = [e for e in candidates if e.validity == ValidityStatus.VALIDATED and e.valid_until is not None
                   and now >= e.valid_until]
        live = [e for e in candidates if e.validity == ValidityStatus.VALIDATED and e not in expired
                and e.entry_id not in superseded]
        if not live:
            return {"status": "EXPIRED" if expired else "UNKNOWN", "entry": None, "expired": expired,
                    "conflicting": []}
        if len({e.content for e in live}) > 1:
            return {"status": "CONFLICT", "entry": None, "expired": expired, "conflicting": live}
        return {"status": "GOVERNING", "entry": max(live, key=lambda e: e.version), "expired": expired,
                "conflicting": []}

    def history_for(self, entry_id: str) -> list:
        """Cadeia de versões (mais antiga -> mais nova) que terminam em
        `entry_id`, seguindo `supersedes`."""
        chain = [self.get(entry_id)]
        current = chain[0]
        while current.supersedes is not None:
            current = self.get(current.supersedes)
            chain.append(current)
        return list(reversed(chain))
