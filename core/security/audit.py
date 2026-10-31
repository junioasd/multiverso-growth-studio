"""
core.security.audit
====================
Audit Trail (Seção 58). Toda entrada é sanitizada antes de persistir —
nenhum segredo pode chegar ao log de auditoria.

MICROCORREÇÃO 2 (auditoria):
- PERSISTÊNCIA: opcional via `db_path` — eventos de auditoria sobrevivem
  a reinício do processo (write-through + reload no boot).
- Ver `core.governance.facade` para a disciplina de QUANDO gravar
  SUCCESS/DENIED/ERROR (este módulo apenas grava o que lhe é pedido,
  sanitizado; a lógica de "só SUCCESS após conclusão real" vive na
  fachada, que é quem decide o outcome e quando chamar `record`).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.models import db as db_module
from core.security.sanitize import sanitize_dict


@dataclass
class AuditEvent:
    event_id: str
    timestamp: float
    actor: str
    action: str
    resource: str
    outcome: str  # SUCCESS | DENIED | ERROR
    details: dict[str, Any]


class AuditTrail:
    """Audit trail com persistência opcional (adapter local). Em
    produção real, poderia ser substituído por um adapter de
    armazenamento durável externo — mantendo a mesma sanitização."""

    def __init__(self, *, db_path: str | None = None) -> None:
        self._events: list[AuditEvent] = []
        self._db_path = db_path
        if self._db_path:
            self._load_from_db()

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM audit_events ORDER BY timestamp ASC").fetchall():
                self._events.append(
                    AuditEvent(
                        event_id=row["event_id"], timestamp=row["timestamp"], actor=row["actor"],
                        action=row["action"], resource=row["resource"], outcome=row["outcome"],
                        details=json.loads(row["details"]) if row["details"] else {},
                    )
                )

    def _persist(self, event: AuditEvent) -> None:
        if not self._db_path:
            return
        with db_module.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO audit_events "
                "(event_id, timestamp, actor, action, resource, outcome, details) VALUES (?,?,?,?,?,?,?)",
                (event.event_id, event.timestamp, event.actor, event.action, event.resource,
                 event.outcome, json.dumps(event.details, default=str)),
            )

    def record(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        safe_details = sanitize_dict(details or {})
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp=time.time(),
            actor=actor,
            action=action,
            resource=resource,
            outcome=outcome,
            details=safe_details,
        )
        self._events.append(event)
        self._persist(event)
        return event

    def all(self) -> list[AuditEvent]:
        return list(self._events)

    def for_resource(self, resource: str) -> list[AuditEvent]:
        return [e for e in self._events if e.resource == resource]
