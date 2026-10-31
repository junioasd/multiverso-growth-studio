"""
atlas.escalation
==================
ATLAS+RADAR — item 3 (PERSISTÊNCIA DE APROVAÇÕES DO OWNER).

`EscalationDesk` é a ÚNICA porta de saída do ATLAS para algo que exija
autoridade que ele não tem (Seção 15/16 do escopo original do ATLAS).
Com `db_path` configurado, cada solicitação é persistida em SQLite
(`owner_approval_requests`) — sobrevive a reinício, restart de serviço,
nova sessão e falha controlada. Nenhuma solicitação é considerada
aprovada apenas porque o processo reiniciou: o status persistido
default é sempre `PENDING_OWNER_APPROVAL`, e só uma decisão explícita
do Owner (com `require_owner()`, a mesma checagem usada em todo o
resto do sistema) muda isso.

Idempotência: quando o chamador informa `idempotency_key`, um índice
único parcial em `owner_approval_requests.idempotency_key` garante que
a MESMA solicitação nunca gera duas linhas — reutilização com o mesmo
conteúdo devolve a solicitação existente; reutilização com conteúdo
incompatível levanta `IdempotencyConflict`. O padrão de transação
atômica (BEGIN IMMEDIATE / commit dentro do try / rollback + close em
qualquer falha) é o MESMO já corrigido e testado em
`commercial/crm.py`, `sales_tracking.py`, `finance.py` e
`core/jobs/queue.py` — nenhuma máquina de transação nova foi inventada.

Decisão (aprovar/rejeitar/cancelar/expirar) usa UPDATE condicionado a
`status = 'PENDING_OWNER_APPROVAL'`: uma segunda tentativa de decidir a
MESMA solicitação (retry, replay) com a MESMA decisão é idempotente
(devolve o estado já decidido); com uma decisão DIFERENTE, é recusada
(`AlreadyDecided`) — nunca silenciosamente sobrescrita.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.governance.idempotency import IdempotencyConflict, stable_fingerprint
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.rbac import Principal, require_owner
from core.security.sanitize import sanitize_dict


class EscalationNotFound(KeyError):
    pass


class AlreadyDecided(Exception):
    """Levantado quando se tenta decidir (aprovar/rejeitar/cancelar) uma
    solicitação que já foi decidida com uma decisão DIFERENTE — nunca
    sobrescreve silenciosamente uma decisão anterior do Owner."""


class ApprovalStatus(str, Enum):
    PENDING_OWNER_APPROVAL = "PENDING_OWNER_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


_TERMINAL_STATUSES = {
    ApprovalStatus.APPROVED, ApprovalStatus.REJECTED,
    ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED,
}
_DOMAIN = "owner_approval"


@dataclass
class EscalationItem:
    escalation_id: str
    operation: str
    origin: str
    reason: str
    risk: str = "UNKNOWN"
    evidence: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING_OWNER_APPROVAL
    required_authority: str = "OWNER"
    decision: str | None = None
    decided_by: str | None = None
    decided_at: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float | None = None

    # Compatibilidade com a API da versão anterior do ATLAS (ver
    # docs/ATLAS.md) — `resolved`/`resolution`/`resolved_by` continuam
    # funcionando exatamente como antes, agora derivados do novo campo
    # `status`, que carrega a informação real (5 estados, não um booleano).
    @property
    def resolved(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    @property
    def resolution(self) -> str | None:
        return self.decision

    @property
    def resolved_by(self) -> str | None:
        return self.decided_by


def _snapshot(item: EscalationItem) -> EscalationItem:
    """MICROCORREÇÃO (IMMUTABLE SECURITY STATE — falha crítica real
    reproduzida por auditoria independente): TODA API pública de
    `EscalationDesk` que devolve um `EscalationItem` devolve uma
    CÓPIA PROFUNDA aqui, nunca a referência ao objeto interno
    autoritativo armazenado em `self._items`. Antes desta correção,
    era possível obter o objeto via `get()` e mutar `.status`/
    `.context` diretamente em memória, alterando o estado interno
    real usado por decisões de segurança subsequentes (bypass total
    de Owner Approval). `copy.deepcopy()` também copia profundamente
    `context`/`evidence` (dicts aninhados) — mutar qualquer campo,
    incluindo chaves aninhadas desses dicts, no objeto retornado
    NUNCA mais afeta o estado interno."""
    return copy.deepcopy(item)


def _snapshot_list(items: list) -> list:
    return [_snapshot(item) for item in items]


class EscalationDesk:
    def __init__(self, audit: AuditTrail, *, db_path: str | None = None) -> None:
        self._items: dict[str, EscalationItem] = {}
        self._idempotency_index: dict[str, str] = {}  # idempotency_key -> escalation_id (modo em memória)
        self.audit = audit
        self._db_path = db_path
        self._lock = threading.RLock()
        if self._db_path:
            self._load_from_db()

    # ------------------------------------------------------------------
    # Persistência / leitura
    # ------------------------------------------------------------------

    def _row_to_item(self, row: sqlite3.Row) -> EscalationItem:
        return EscalationItem(
            escalation_id=row["request_id"], operation=row["operation"], origin=row["origin"],
            reason=row["reason"], risk=row["risk"],
            evidence=json.loads(row["evidence"]) if row["evidence"] else {},
            context=json.loads(row["context"]) if row["context"] else {},
            status=ApprovalStatus(row["status"]), required_authority=row["required_authority"],
            decision=row["decision"], decided_by=row["decided_by"], decided_at=row["decided_at"],
            created_at=row["created_at"], updated_at=row["updated_at"], expires_at=row["expires_at"],
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM owner_approval_requests").fetchall():
                item = self._row_to_item(row)
                self._items[item.escalation_id] = item

    @staticmethod
    def _insert_item_sql(conn: sqlite3.Connection, item: EscalationItem, idempotency_key: str | None) -> None:
        conn.execute(
            "INSERT INTO owner_approval_requests "
            "(request_id, operation, origin, reason, risk, evidence, context, status, required_authority, "
            " decision, decided_by, decided_at, idempotency_key, created_at, updated_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item.escalation_id, item.operation, item.origin, item.reason, item.risk,
             json.dumps(item.evidence, default=str), json.dumps(item.context, default=str),
             item.status.value, item.required_authority, item.decision, item.decided_by, item.decided_at,
             idempotency_key, item.created_at, item.updated_at, item.expires_at),
        )

    def _fetch_item_from_db(self, escalation_id: str) -> EscalationItem | None:
        with db_module.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM owner_approval_requests WHERE request_id = ?", (escalation_id,)
            ).fetchone()
        return self._row_to_item(row) if row is not None else None

    def _fetch_item_by_idempotency_key(self, idempotency_key: str) -> EscalationItem | None:
        with db_module.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM owner_approval_requests WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._row_to_item(row) if row is not None else None

    def _audit_escalate(self, item: EscalationItem) -> None:
        self.audit.record(
            actor=item.origin, action="atlas_escalate_to_owner", resource=item.escalation_id,
            outcome="SUCCESS", details={"reason": item.reason, "operation": item.operation, "risk": item.risk},
        )

    # ------------------------------------------------------------------
    # Criação de solicitação
    # ------------------------------------------------------------------

    @staticmethod
    def _fingerprint_for(
        *, operation: str, origin: str, reason: str, risk: str, context: dict, evidence: dict,
        expires_at: float | None,
    ) -> str:
        """Identidade IMUTÁVEL completa de uma solicitação crítica —
        MICROCORREÇÃO ATLAS+RADAR (idempotência das aprovações): o
        fingerprint anterior só considerava (operation, reason, risk,
        actor), então a MESMA idempotency_key podia ser reutilizada com
        `context`/`details`/`evidence` DIFERENTES sem gerar conflito
        (ex: `spend_money amount=100` e depois `amount=9999` com a
        mesma chave passavam despercebidos). Agora `context` e
        `evidence` completos entram no fingerprint, e `stable_fingerprint`
        já canonicaliza a ordem das chaves de qualquer dict (recursivamente,
        via `json.dumps(..., sort_keys=True)`) — um dict semanticamente
        igual com chaves em ordem diferente produz o MESMO fingerprint
        (nunca um falso conflito), enquanto qualquer diferença de
        conteúdo real produz um fingerprint diferente (sempre conflito)."""
        return stable_fingerprint(operation, origin, reason, risk, context, evidence, expires_at)

    def escalate(
        self,
        *,
        reason: str,
        context: dict,
        actor: str,
        operation: str = "generic",
        risk: str = "UNKNOWN",
        evidence: dict | None = None,
        idempotency_key: str | None = None,
        expires_at: float | None = None,
    ) -> EscalationItem:
        item = EscalationItem(
            escalation_id=f"esc_{uuid.uuid4().hex[:12]}", operation=operation, origin=actor, reason=reason, risk=risk,
            # MICROCORREÇÃO (DEFENSIVE COPY ON INPUT — falha real
            # reproduzida por auditoria independente): `sanitize_dict()`
            # recria dicts recursivamente (protege contra
            # compartilhamento de referência para valores `dict`
            # aninhados), mas devolve `list`/`tuple`/`set` (e qualquer
            # coisa dentro deles, ex: `list[dict]`) por REFERÊNCIA
            # direta — mutar a lista/dict ORIGINAL que o chamador ainda
            # segura, depois de `escalate()` retornar, mutava o estado
            # interno através dessa referência compartilhada.
            # `copy.deepcopy()` aplicado no resultado JÁ sanitizado
            # elimina QUALQUER referência mutável remanescente
            # (list/tuple/set e estruturas aninhadas), sem alterar a
            # redação de segredos já feita por `sanitize_dict()`.
            evidence=copy.deepcopy(sanitize_dict(evidence or {})), context=copy.deepcopy(sanitize_dict(context)),
            expires_at=expires_at,
        )

        with self._lock:
            if not idempotency_key:
                if self._db_path:
                    with db_module.connect(self._db_path) as conn:
                        self._insert_item_sql(conn, item, None)
                self._items[item.escalation_id] = item
                self._audit_escalate(item)
                return _snapshot(item)

            # Fingerprint calculado sobre os valores JÁ sanitizados do
            # item (item.context/item.evidence) — os MESMOS valores que
            # serão persistidos e relidos depois, garantindo que a
            # verificação de conflito seja sempre feita contra
            # exatamente o que está armazenado.
            fingerprint = self._fingerprint_for(
                operation=operation, origin=actor, reason=reason, risk=risk,
                context=item.context, evidence=item.evidence, expires_at=expires_at,
            )

            if self._db_path:
                return _snapshot(self._escalate_durable(item, idempotency_key, fingerprint))

            existing_id = self._idempotency_index.get(idempotency_key)
            if existing_id is not None:
                existing = self._items[existing_id]
                existing_fp = self._fingerprint_for(
                    operation=existing.operation, origin=existing.origin, reason=existing.reason,
                    risk=existing.risk, context=existing.context, evidence=existing.evidence,
                    expires_at=existing.expires_at,
                )
                if existing_fp != fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency_key '{idempotency_key}' já usada com conteúdo diferente — "
                        "reutilização incompatível recusada"
                    )
                return _snapshot(existing)
            self._items[item.escalation_id] = item
            self._idempotency_index[idempotency_key] = item.escalation_id
            self._audit_escalate(item)
            return _snapshot(item)

    def _escalate_durable(self, item: EscalationItem, idempotency_key: str, fingerprint: str) -> EscalationItem:
        conn = db_module.get_autocommit_connection(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_item_sql(conn, item, idempotency_key)
            conn.commit()  # commit DENTRO do try (Microcorreção 5, item 2)
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            finally:
                conn.close()
            return self._resolve_escalate_conflict(idempotency_key, fingerprint)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass  # não mascara o erro original
            finally:
                conn.close()
            raise
        else:
            conn.close()
            self._items[item.escalation_id] = item
            self._audit_escalate(item)
            return item

    def _resolve_escalate_conflict(self, idempotency_key: str, fingerprint: str) -> EscalationItem:
        with db_module.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM owner_approval_requests WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        if row is None:
            raise IdempotencyConflict(
                f"idempotency_key '{idempotency_key}' colidiu mas a solicitação correspondente "
                "não foi encontrada — estado inesperado, abortando com segurança"
            )
        existing = self._row_to_item(row)
        existing_fingerprint = self._fingerprint_for(
            operation=existing.operation, origin=existing.origin, reason=existing.reason,
            risk=existing.risk, context=existing.context, evidence=existing.evidence,
            expires_at=existing.expires_at,
        )
        if existing_fingerprint != fingerprint:
            raise IdempotencyConflict(
                f"idempotency_key '{idempotency_key}' já usada com conteúdo diferente — "
                "reutilização incompatível recusada"
            )
        self._items[existing.escalation_id] = existing
        return existing

    # ------------------------------------------------------------------
    # Decisão (aprovar / rejeitar / cancelar / expirar)
    # ------------------------------------------------------------------

    def _decide(self, escalation_id: str, *, new_status: ApprovalStatus, decision: str, decided_by: str) -> EscalationItem:
        with self._lock:
            if self._db_path:
                return self._decide_durable(escalation_id, new_status=new_status, decision=decision, decided_by=decided_by)

            if escalation_id not in self._items:
                raise EscalationNotFound(escalation_id)
            item = self._items[escalation_id]
            if item.status in _TERMINAL_STATUSES:
                if item.status == new_status and item.decision == decision:
                    return item  # retry idempotente — mesma decisão
                raise AlreadyDecided(
                    f"solicitação '{escalation_id}' já foi decidida ({item.status.value}) — "
                    "não pode ser decidida novamente com um resultado diferente"
                )
            item.status = new_status
            item.decision = decision
            item.decided_by = decided_by
            item.decided_at = time.time()
            item.updated_at = item.decided_at
            return item

    def _decide_durable(self, escalation_id: str, *, new_status: ApprovalStatus, decision: str, decided_by: str) -> EscalationItem:
        now = time.time()
        conn = db_module.get_autocommit_connection(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE owner_approval_requests SET status=?, decision=?, decided_by=?, decided_at=?, updated_at=? "
                "WHERE request_id=? AND status=?",
                (new_status.value, decision, decided_by, now, now, escalation_id, ApprovalStatus.PENDING_OWNER_APPROVAL.value),
            )
            conn.commit()
            matched = cursor.rowcount > 0
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            finally:
                conn.close()
            raise
        else:
            conn.close()

        if matched:
            updated = self._fetch_item_from_db(escalation_id)
            self._items[escalation_id] = updated
            return updated

        # não casou -- ou não existe, ou já foi decidida antes.
        current = self._fetch_item_from_db(escalation_id)
        if current is None:
            raise EscalationNotFound(escalation_id)
        self._items[escalation_id] = current
        if current.status == new_status and current.decision == decision:
            return current  # retry idempotente
        raise AlreadyDecided(
            f"solicitação '{escalation_id}' já foi decidida ({current.status.value}) — "
            "não pode ser decidida novamente com um resultado diferente"
        )

    def approve(self, escalation_id: str, *, decision: str, resolved_by: Principal) -> EscalationItem:
        require_owner(resolved_by)
        item = self._decide(escalation_id, new_status=ApprovalStatus.APPROVED, decision=decision, decided_by=resolved_by.subject)
        self.audit.record(
            actor=resolved_by.subject, action="approve_escalation", resource=escalation_id,
            outcome="SUCCESS", details={},
        )
        return _snapshot(item)

    def reject(self, escalation_id: str, *, decision: str, resolved_by: Principal) -> EscalationItem:
        require_owner(resolved_by)
        item = self._decide(escalation_id, new_status=ApprovalStatus.REJECTED, decision=decision, decided_by=resolved_by.subject)
        self.audit.record(
            actor=resolved_by.subject, action="reject_escalation", resource=escalation_id,
            outcome="SUCCESS", details={},
        )
        return _snapshot(item)

    def cancel(self, escalation_id: str, *, resolved_by: Principal) -> EscalationItem:
        require_owner(resolved_by)
        item = self._decide(
            escalation_id, new_status=ApprovalStatus.CANCELLED, decision="cancelado pelo Owner",
            decided_by=resolved_by.subject,
        )
        self.audit.record(
            actor=resolved_by.subject, action="cancel_escalation", resource=escalation_id,
            outcome="SUCCESS", details={},
        )
        return _snapshot(item)

    def expire_stale(self, *, ttl_seconds: float) -> list[EscalationItem]:
        """Marca como EXPIRED as solicitações pendentes mais antigas que
        `ttl_seconds` — nunca as trata como aprovadas por decurso de
        prazo."""
        now = time.time()
        expired: list[EscalationItem] = []
        for item in list(self.pending()):
            if now - item.created_at > ttl_seconds:
                try:
                    updated = self._decide(
                        item.escalation_id, new_status=ApprovalStatus.EXPIRED,
                        decision="expirado por decurso de prazo", decided_by="system",
                    )
                    expired.append(_snapshot(updated))
                    self.audit.record(
                        actor="system", action="expire_escalation", resource=item.escalation_id,
                        outcome="SUCCESS", details={},
                    )
                except AlreadyDecided:
                    pass  # decidido por outra via nesse meio-tempo — nada a fazer
        return expired

    # Mantido por compatibilidade com a versão anterior do ATLAS — mapeia
    # para `approve()` (uma "resolução" genérica sempre significou
    # aprovação nas versões anteriores do ATLAS).
    def resolve(self, escalation_id: str, *, resolution: str, resolved_by: Principal) -> EscalationItem:
        return self.approve(escalation_id, decision=resolution, resolved_by=resolved_by)

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    def get(self, escalation_id: str) -> EscalationItem:
        """MICROCORREÇÃO (IMMUTABLE SECURITY STATE): devolve sempre uma
        CÓPIA PROFUNDA — mutar o objeto retornado (status, context,
        evidence, requested_amount dentro de context, etc.) nunca mais
        afeta o estado interno autoritativo usado por decisões de
        segurança subsequentes (Owner Approval, SpendGuard)."""
        if self._db_path:
            item = self._fetch_item_from_db(escalation_id)
            if item is None:
                raise EscalationNotFound(escalation_id)
            self._items[escalation_id] = item
            return _snapshot(item)
        if escalation_id not in self._items:
            raise EscalationNotFound(escalation_id)
        return _snapshot(self._items[escalation_id])

    def pending(self) -> list[EscalationItem]:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT * FROM owner_approval_requests WHERE status = ?",
                    (ApprovalStatus.PENDING_OWNER_APPROVAL.value,),
                ).fetchall()
            return _snapshot_list([self._row_to_item(r) for r in rows])
        return _snapshot_list([item for item in self._items.values() if item.status == ApprovalStatus.PENDING_OWNER_APPROVAL])

    def all(self) -> list[EscalationItem]:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM owner_approval_requests").fetchall()
            return _snapshot_list([self._row_to_item(r) for r in rows])
        return _snapshot_list(list(self._items.values()))

    def find_by_idempotency_key(self, idempotency_key: str) -> EscalationItem | None:
        """CORREÇÃO (NAVIGATOR — consumo real de Owner Approval): permite
        que um chamador (o NAVIGATOR) LOCALIZE uma solicitação já
        existente pela sua identidade — inclusive já `APPROVED`,
        `REJECTED`, `EXPIRED` ou `CANCELLED` — para decidir se pode
        consumir, precisa aguardar, ou nunca deve executar. Sempre lê o
        estado FRESCO (do banco, quando persistido), nunca uma cópia em
        cache potencialmente obsoleta.

        MICROCORREÇÃO (IMMUTABLE SECURITY STATE): devolve uma CÓPIA
        PROFUNDA — nunca a referência interna."""
        if self._db_path:
            item = self._fetch_item_by_idempotency_key(idempotency_key)
            if item is not None:
                self._items[item.escalation_id] = item
                return _snapshot(item)
            return None
        existing_id = self._idempotency_index.get(idempotency_key)
        found = self._items.get(existing_id) if existing_id else None
        return _snapshot(found) if found is not None else None
