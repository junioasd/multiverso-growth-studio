"""
core.evidence.verifier_registry
===================================
CORREÇÃO (item 1 desta rodada — Authority Root-of-Trust: não aceitar
`authority_level` autodeclarado): antes, `register()` exigia
`actor_id`/`authority_level`/`registration_reason`, mas
`authority_level` era uma STRING fornecida pelo próprio chamador —
`actor_id="attacker", authority_level="OWNER"` bastava para o sistema
aceitar o atacante como autoridade. Isso está PROIBIDO.

Agora `register()` exige `authority_grant_id` — resolvido de verdade
contra um `AuthorityRegistry` (`core.evidence.authority_registry`).
A autoridade EFETIVA (nível, escopos) sempre vem do
`AuthorityGrant` resolvido — nunca de uma string enviada pelo
chamador. Grant ausente/inválido/expirado/revogado/sem o escopo
`REGISTER_VERIFIER` -> `RegistrationDenied` sempre.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.capabilities.registry import CapabilityHealth
from core.evidence.authority_registry import SCOPE_REGISTER_VERIFIER, AuthorityRegistry


class TrustLevel(str, Enum):
    UNTRUSTED = "UNTRUSTED"
    LOW = "LOW"
    STANDARD = "STANDARD"
    HIGH = "HIGH"


_TRUST_ORDER = [TrustLevel.UNTRUSTED, TrustLevel.LOW, TrustLevel.STANDARD, TrustLevel.HIGH]


class RegistrationDenied(Exception):
    """Levantado quando uma tentativa de registro/promoção de verifier
    não tem um `AuthorityGrant` válido com o escopo necessário —
    inclui especificamente a tentativa de um componente se
    autorregistrar como `'self'`/`authorized=True` sem grant real,
    MESMO que um `authority_level` "convincente" seja alegado (nunca
    mais aceito como prova)."""


@dataclass
class VerifierRegistration:
    verifier_id: str
    verifier_type: str
    authorized: bool = False
    capabilities: list = field(default_factory=list)
    verification_methods: list = field(default_factory=list)
    health: CapabilityHealth = CapabilityHealth.UNKNOWN
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    scope: str = "GENERAL"
    # CORREÇÃO (item 1): trilha de auditoria do próprio registro —
    # `authority_grant_id` é a PROVA (resolvida contra o
    # AuthorityRegistry), nunca uma string de nível autodeclarada.
    registered_by: str | None = None
    authority_grant_id: str | None = None
    registration_reason: str | None = None
    approval_id: str | None = None
    audit_id: str | None = None
    created_at: float = field(default_factory=time.time)


class VerifierRegistry:
    """Registro de verificadores autorizados. VAZIO por padrão — NENHUM
    verifier é confiável até ser genuinamente registrado.

    CORREÇÃO (item 1): `register()` é o ÚNICO caminho público de
    escrita — SEMPRE exige `authority_grant_id` resolvendo para um
    `AuthorityGrant` ATIVO com o escopo `REGISTER_VERIFIER`. Nunca
    aceita `authority_level` como string."""

    def __init__(self, *, authority_registry: AuthorityRegistry | None = None, db_path: str | None = None) -> None:
        self._verifiers: dict = {}
        self.audit_log: list = []
        self.authority_registry = authority_registry if authority_registry is not None else AuthorityRegistry()
        # CORREÇÃO (round 9 — continuidade após restart): registros de
        # verifier são decisões críticas de governança tanto quanto os
        # grants — precisam sobreviver a restart do mesmo jeito.
        self.db_path = db_path
        if db_path:
            self._load_persisted_verifiers()

    def _load_persisted_verifiers(self) -> None:
        import json

        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM authority_registered_verifiers").fetchall()
        for row in rows:
            registration = VerifierRegistration(
                verifier_id=row["verifier_id"], verifier_type=row["verifier_type"],
                authorized=bool(row["authorized"]), capabilities=json.loads(row["capabilities"]),
                verification_methods=json.loads(row["verification_methods"]), health=CapabilityHealth(row["health"]),
                trust_level=TrustLevel(row["trust_level"]), scope=row["scope"], registered_by=row["registered_by"],
                authority_grant_id=row["authority_grant_id"], registration_reason=row["registration_reason"],
                approval_id=row["approval_id"], audit_id=row["audit_id"], created_at=row["created_at"],
            )
            self._verifiers[registration.verifier_id] = registration

    def _persist_verifier(self, registration: VerifierRegistration) -> None:
        if not self.db_path:
            return
        import json

        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO authority_registered_verifiers "
                "(verifier_id, verifier_type, authorized, capabilities, verification_methods, health, "
                "trust_level, scope, registered_by, authority_grant_id, registration_reason, approval_id, "
                "audit_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    registration.verifier_id, registration.verifier_type, int(registration.authorized),
                    json.dumps(registration.capabilities), json.dumps(registration.verification_methods),
                    registration.health.value, registration.trust_level.value, registration.scope,
                    registration.registered_by, registration.authority_grant_id, registration.registration_reason,
                    registration.approval_id, registration.audit_id, registration.created_at,
                ),
            )

    def register(
        self, registration: VerifierRegistration, *, authority_grant_id: str, registration_reason: str,
        approval_id: str | None = None,
    ) -> VerifierRegistration:
        """CORREÇÃO (item 1 — Teste 12/21): recusa (`RegistrationDenied`)
        qualquer registro sem `authority_grant_id`/`registration_reason`
        preenchidos, ou cujo grant não resolva, não esteja ATIVO, ou
        não tenha o escopo `REGISTER_VERIFIER` — `registration.
        authorized=True` fornecido pelo chamador dentro do objeto
        NUNCA basta sozinho."""
        audit_id = f"audit_{uuid.uuid4().hex[:12]}"
        denied_reason = None
        if not authority_grant_id or not registration_reason:
            denied_reason = "authority_grant_id e registration_reason são obrigatórios"
        elif not self.authority_registry.validate_grant(authority_grant_id, required_scope=SCOPE_REGISTER_VERIFIER):
            denied_reason = (
                f"authority_grant_id '{authority_grant_id}' inválido/ausente/expirado/revogado ou sem "
                f"escopo '{SCOPE_REGISTER_VERIFIER}'"
            )

        grant = self.authority_registry.resolve_grant(authority_grant_id)
        actor_id = grant.subject_actor_id if grant else None

        self.audit_log.append({
            "audit_id": audit_id, "verifier_id": registration.verifier_id, "actor_id": actor_id,
            "authority_grant_id": authority_grant_id, "registration_reason": registration_reason,
            "approval_id": approval_id, "outcome": "DENIED" if denied_reason else "REGISTERED",
            "denied_reason": denied_reason, "created_at": time.time(),
        })
        if denied_reason:
            raise RegistrationDenied(f"registro de verifier '{registration.verifier_id}' recusado: {denied_reason}")

        registration.registered_by = actor_id
        registration.authority_grant_id = authority_grant_id
        registration.registration_reason = registration_reason
        registration.approval_id = approval_id
        registration.audit_id = audit_id
        self._verifiers[registration.verifier_id] = registration
        self._persist_verifier(registration)
        return registration

    def resolve(self, verifier_id: str) -> VerifierRegistration | None:
        return self._verifiers.get(verifier_id)

    def is_authorized(
        self, verifier_id: str | None, *, verification_method: str | None = None,
        min_trust_level: TrustLevel = TrustLevel.STANDARD, allow_degraded: bool = False,
        required_scope: str | None = None, required_capability: str | None = None,
    ) -> bool:
        """Verifier desconhecido — incluindo `None`/vazio/qualquer nome
        nunca registrado, como `'self'` — NUNCA autorizado. Além de
        `authorized=True`: `health` precisa ser `HEALTHY` (`DEGRADED`
        só com `allow_degraded=True` explícito); `trust_level` precisa
        atingir `min_trust_level`; `verification_method`/`scope`/
        `capability`, quando informados, precisam ser compatíveis.
        Adicionalmente, CORREÇÃO (item 19 — revalidação de root-of-
        trust): o `authority_grant_id` do registro ainda precisa
        resolver para um grant ATIVO agora — um grant revogado DEPOIS
        do registro derruba a autorização imediatamente, mesmo sem
        ninguém ter tocado no `VerifierRegistration` em si."""
        if not verifier_id:
            return False
        registration = self.resolve(verifier_id)
        if registration is None or not registration.authorized:
            return False
        if not self.authority_registry.validate_grant(registration.authority_grant_id):
            return False
        if registration.health == CapabilityHealth.HEALTHY:
            pass
        elif registration.health == CapabilityHealth.DEGRADED and allow_degraded:
            pass
        else:
            return False
        if _TRUST_ORDER.index(registration.trust_level) < _TRUST_ORDER.index(min_trust_level):
            return False
        if verification_method is not None and registration.verification_methods:
            if verification_method not in registration.verification_methods:
                return False
        if required_scope is not None and registration.scope not in ("GENERAL", required_scope):
            return False
        if required_capability is not None and registration.capabilities:
            if required_capability not in registration.capabilities:
                return False
        return True

    def all(self) -> list:
        return list(self._verifiers.values())


def build_default_verifier_registry() -> VerifierRegistry:
    """Registro padrão — VAZIO. Nenhum verifier confiável até registro
    explícito com um `AuthorityGrant` genuíno."""
    return VerifierRegistry()
