"""
core.evidence.source_registry
=================================
CORREÇÃO (item 1 desta rodada — Authority Root-of-Trust): mesma
disciplina de `verifier_registry.py` — `register()` nunca mais aceita
`authority_level` como string; exige `authority_grant_id` resolvendo
para um `AuthorityGrant` real com o escopo `REGISTER_SOURCE`. Uma
fonte `TRUSTED_OFFICIAL`/`TRUSTED_FIRST_PARTY` exige adicionalmente
que o GRANT (não uma string alegada) tenha `authority_level` em
`OWNER`/`GOVERNANCE`.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.capabilities.registry import CapabilityHealth
from core.evidence.authority_registry import SCOPE_REGISTER_SOURCE, AuthorityRegistry
from core.evidence.registry import VerificationStatus


class TrustClass(str, Enum):
    TRUSTED_FIRST_PARTY = "TRUSTED_FIRST_PARTY"
    TRUSTED_OFFICIAL = "TRUSTED_OFFICIAL"
    VERIFIED_PUBLIC = "VERIFIED_PUBLIC"
    BENCHMARK = "BENCHMARK"
    USER_PROVIDED = "USER_PROVIDED"
    MOCK = "MOCK"
    UNKNOWN = "UNKNOWN"
    UNTRUSTED = "UNTRUSTED"


NEVER_SUSTAINS_FACT = frozenset({TrustClass.MOCK, TrustClass.UNKNOWN, TrustClass.UNTRUSTED})
_HIGH_TRUST_CLASSES = frozenset({TrustClass.TRUSTED_OFFICIAL, TrustClass.TRUSTED_FIRST_PARTY})
_HIGH_TRUST_REQUIRES_LEVELS = frozenset({"OWNER", "GOVERNANCE"})


class SourceType(str, Enum):
    OFFICIAL = "OFFICIAL"
    FIRST_PARTY = "FIRST_PARTY"
    PLATFORM_API = "PLATFORM_API"
    OWN_OPERATION_DATA = "OWN_OPERATION_DATA"
    PUBLIC_WEB = "PUBLIC_WEB"
    BENCHMARK = "BENCHMARK"
    USER_PROVIDED = "USER_PROVIDED"
    MOCK = "MOCK"
    UNKNOWN = "UNKNOWN"


class SourceRegistrationDenied(Exception):
    """Levantado quando uma tentativa de registro de fonte não tem um
    `AuthorityGrant` válido com o escopo `REGISTER_SOURCE` — mesmo que
    o objeto declare `TRUSTED_OFFICIAL`/`VERIFIED`/`HEALTHY`."""


@dataclass
class SourceRegistration:
    source_id: str
    source_type: SourceType
    source_reference: str
    registered_by: str | None = None
    authority_grant_id: str | None = None
    requested_trust_class: TrustClass = TrustClass.UNKNOWN
    requested_verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    trust_class: TrustClass = field(init=False, default=TrustClass.UNKNOWN)
    verification_status: VerificationStatus = field(init=False, default=VerificationStatus.UNVERIFIED)
    health: CapabilityHealth = CapabilityHealth.UNKNOWN
    verification_method: str | None = None
    observed_at: float = field(default_factory=time.time)
    last_checked: float = field(default_factory=time.time)
    scope: str = "GENERAL"
    capabilities: list = field(default_factory=list)
    integrity_reference: str | None = None
    approval_id: str | None = None
    audit_id: str | None = None

    def __post_init__(self) -> None:
        self.trust_class = TrustClass.UNKNOWN
        self.verification_status = VerificationStatus.UNVERIFIED

    def can_sustain_fact(self) -> bool:
        if self.trust_class in NEVER_SUSTAINS_FACT:
            return False
        if self.verification_status != VerificationStatus.VERIFIED:
            return False
        if self.health != CapabilityHealth.HEALTHY:
            return False
        return True


class SourceRegistry:
    """Fonte de verdade para resolução de fontes. VAZIO por padrão.

    CORREÇÃO (item 1): `register()` exige `authority_grant_id`
    resolvendo para um `AuthorityGrant` ATIVO com o escopo
    `REGISTER_SOURCE`. `TRUSTED_OFFICIAL`/`TRUSTED_FIRST_PARTY` exige
    adicionalmente que o `authority_level` DO GRANT esteja em
    `OWNER`/`GOVERNANCE`."""

    def __init__(self, *, authority_registry: AuthorityRegistry | None = None, escalation_desk=None, db_path: str | None = None) -> None:
        self._sources: dict = {}
        self.audit_log: list = []
        self.authority_registry = authority_registry if authority_registry is not None else AuthorityRegistry()
        # CORREÇÃO (item 12 — Owner Approval integration real): quando
        # fornecido (`atlas.escalation.EscalationDesk`, o mecanismo já
        # existente — nunca um approval paralelo), `TRUSTED_OFFICIAL`
        # exige `approval_id` resolvendo para uma `EscalationItem`
        # genuinamente `APPROVED` — nunca `REJECTED`/`EXPIRED`/
        # `CANCELLED`/pendente.
        self.escalation_desk = escalation_desk
        # CORREÇÃO (round 9 — continuidade após restart): registros de
        # source são decisões críticas de governança tanto quanto os
        # grants — precisam sobreviver a restart do mesmo jeito.
        self.db_path = db_path
        if db_path:
            self._load_persisted_sources()

    def _load_persisted_sources(self) -> None:
        import json

        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM authority_registered_sources").fetchall()
        for row in rows:
            registration = SourceRegistration(
                source_id=row["source_id"], source_type=SourceType(row["source_type"]),
                source_reference=row["source_reference"], registered_by=row["registered_by"],
                authority_grant_id=row["authority_grant_id"],
                requested_trust_class=TrustClass(row["requested_trust_class"]),
                requested_verification_status=VerificationStatus(row["requested_verification_status"]),
                health=CapabilityHealth(row["health"]), verification_method=row["verification_method"],
                observed_at=row["observed_at"], last_checked=row["last_checked"], scope=row["scope"],
                capabilities=json.loads(row["capabilities"]), integrity_reference=row["integrity_reference"],
                approval_id=row["approval_id"], audit_id=row["audit_id"],
            )
            # os campos EFETIVOS (`trust_class`/`verification_status`) são
            # `field(init=False)` — restaurados aqui exatamente como
            # persistidos (nunca recalculados/re-concedidos "de graça").
            registration.trust_class = TrustClass(row["trust_class"])
            registration.verification_status = VerificationStatus(row["verification_status"])
            self._sources[registration.source_id] = registration

    def _persist_source(self, registration: SourceRegistration) -> None:
        if not self.db_path:
            return
        import json

        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO authority_registered_sources "
                "(source_id, source_type, source_reference, registered_by, authority_grant_id, "
                "requested_trust_class, requested_verification_status, trust_class, verification_status, "
                "health, verification_method, observed_at, last_checked, scope, capabilities, "
                "integrity_reference, approval_id, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    registration.source_id, registration.source_type.value, registration.source_reference,
                    registration.registered_by, registration.authority_grant_id,
                    registration.requested_trust_class.value, registration.requested_verification_status.value,
                    registration.trust_class.value, registration.verification_status.value,
                    registration.health.value, registration.verification_method, registration.observed_at,
                    registration.last_checked, registration.scope, json.dumps(registration.capabilities),
                    registration.integrity_reference, registration.approval_id, registration.audit_id,
                ),
            )

    def register(
        self, registration: SourceRegistration, *, authority_grant_id: str, registration_reason: str,
        approval_id: str | None = None,
    ) -> SourceRegistration:
        """CORREÇÃO (item 6/12 — Teste self-trust): recusa
        (`SourceRegistrationDenied`) qualquer registro sem
        `authority_grant_id`/`registration_reason`, ou cujo grant não
        resolva/não esteja ATIVO/não tenha o escopo `REGISTER_SOURCE`.
        Para `TRUSTED_OFFICIAL`/`TRUSTED_FIRST_PARTY`, exige
        adicionalmente que o GRANT (nunca uma string alegada) tenha
        `authority_level` em `OWNER`/`GOVERNANCE`. CORREÇÃO (item 12):
        quando `self.escalation_desk` está configurado, `TRUSTED_OFFICIAL`
        exige adicionalmente `approval_id` resolvendo para uma
        aprovação Owner genuinamente `APPROVED` (consumida uma única
        vez — ver `atlas.escalation.EscalationDesk`)."""
        audit_id = f"audit_{uuid.uuid4().hex[:12]}"
        denied_reason = None
        grant = self.authority_registry.resolve_grant(authority_grant_id)
        if not authority_grant_id or not registration_reason:
            denied_reason = "authority_grant_id e registration_reason são obrigatórios"
        elif not self.authority_registry.validate_grant(authority_grant_id, required_scope=SCOPE_REGISTER_SOURCE):
            denied_reason = (
                f"authority_grant_id '{authority_grant_id}' inválido/ausente/expirado/revogado ou sem "
                f"escopo '{SCOPE_REGISTER_SOURCE}'"
            )
        elif registration.requested_trust_class in _HIGH_TRUST_CLASSES and (
            grant is None or grant.authority_level not in _HIGH_TRUST_REQUIRES_LEVELS
        ):
            denied_reason = (
                f"trust_class '{registration.requested_trust_class.value}' exige grant com authority_level "
                f"OWNER/GOVERNANCE (grant tem: '{grant.authority_level if grant else None}')"
            )
        elif (
            registration.requested_trust_class == TrustClass.TRUSTED_OFFICIAL and self.escalation_desk is not None
        ):
            if not approval_id:
                denied_reason = "trust_class TRUSTED_OFFICIAL exige approval_id (Owner Approval) quando escalation_desk está configurado"
            else:
                try:
                    item = self.escalation_desk.get(approval_id)
                    if item.status.value != "APPROVED":
                        denied_reason = f"approval '{approval_id}' não está APPROVED (status={item.status.value})"
                except KeyError:
                    denied_reason = f"approval_id '{approval_id}' não existe no EscalationDesk"

        self.audit_log.append({
            "audit_id": audit_id, "source_id": registration.source_id,
            "actor_id": grant.subject_actor_id if grant else None, "authority_grant_id": authority_grant_id,
            "registration_reason": registration_reason,
            "requested_trust_class": registration.requested_trust_class.value,
            "requested_verification_status": registration.requested_verification_status.value,
            "approval_id": approval_id, "outcome": "DENIED" if denied_reason else "REGISTERED",
            "denied_reason": denied_reason, "created_at": time.time(),
        })
        if denied_reason:
            raise SourceRegistrationDenied(f"registro de source '{registration.source_id}' recusado: {denied_reason}")

        registration.registered_by = grant.subject_actor_id
        registration.authority_grant_id = authority_grant_id
        registration.approval_id = approval_id
        registration.audit_id = audit_id
        registration.trust_class = registration.requested_trust_class
        registration.verification_status = registration.requested_verification_status
        self._sources[registration.source_id] = registration
        self._persist_source(registration)
        return registration

    def resolve(self, source_id: str) -> SourceRegistration | None:
        return self._sources.get(source_id)

    def can_sustain_fact(self, source_id: str) -> bool:
        """CORREÇÃO (item 19 — revalidação de root-of-trust): além da
        checagem de `can_sustain_fact()` do próprio registro, o grant
        que autorizou este registro precisa CONTINUAR ativo agora — se
        foi revogado depois, a fonte deixa de sustentar FACT
        imediatamente."""
        registration = self.resolve(source_id)
        if registration is None:
            return False
        if not self.authority_registry.validate_grant(registration.authority_grant_id):
            return False
        return registration.can_sustain_fact()

    def all(self) -> list:
        return list(self._sources.values())
