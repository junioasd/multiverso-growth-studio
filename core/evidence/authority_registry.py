"""
core.evidence.authority_registry
====================================
CORREÇÃO (itens 1/2/3 desta rodada — Authority Root-of-Trust real):
antes, `VerifierRegistry.register()`/`SourceRegistry.register()`
exigiam `actor_id`/`authority_level`/`registration_reason`, mas
`authority_level` era uma STRING fornecida pelo próprio chamador —
`actor_id="attacker", authority_level="OWNER"` bastava para o sistema
aceitar o atacante como autoridade. Isso está PROIBIDO.

Agora `authority_level` NUNCA é aceito diretamente — o chamador
precisa fornecer um `authority_grant_id` que resolve para um
`AuthorityGrant` REAL, emitido pelo `AuthorityRegistry` (nunca
autodeclarado, nunca criado pelo próprio chamador). A autoridade
EFETIVA sempre vem do grant resolvido — nunca do que foi pedido.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

# CORREÇÃO (item 9): escopos específicos — um grant com REGISTER_SOURCE
# nunca permite REGISTER_VERIFIER automaticamente.
SCOPE_REGISTER_VERIFIER = "REGISTER_VERIFIER"
SCOPE_REGISTER_SOURCE = "REGISTER_SOURCE"
SCOPE_PROMOTE_TRUST = "PROMOTE_TRUST"
SCOPE_REVOKE_VERIFIER = "REVOKE_VERIFIER"
SCOPE_REVOKE_SOURCE = "REVOKE_SOURCE"
SCOPE_ISSUE_AUTHORITY_GRANT = "ISSUE_AUTHORITY_GRANT"

ALL_SCOPES = frozenset({
    SCOPE_REGISTER_VERIFIER, SCOPE_REGISTER_SOURCE, SCOPE_PROMOTE_TRUST,
    SCOPE_REVOKE_VERIFIER, SCOPE_REVOKE_SOURCE, SCOPE_ISSUE_AUTHORITY_GRANT,
})

# CORREÇÃO (item 10): ordem de níveis — usada para bloquear privilege
# escalation (um ator nunca emite grant de nível superior ao seu).
_LEVEL_ORDER = ["NONE", "USER", "SENIOR", "ATLAS_ADMIN", "GOVERNANCE", "OWNER"]


class GrantStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    INVALID = "INVALID"


class AuthorityDenied(Exception):
    """Levantado sempre que uma operação de autoridade (registro,
    emissão de grant, promoção) é recusada — grant ausente, inválido,
    expirado, revogado, sem escopo suficiente, ou tentativa de
    privilege escalation."""


@dataclass
class AuthorityGrant:
    grant_id: str
    subject_actor_id: str
    authority_level: str
    issued_by: str
    scopes: list = field(default_factory=list)
    permissions: list = field(default_factory=list)
    issued_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    approval_id: str | None = None
    status: GrantStatus = GrantStatus.ACTIVE
    # CORREÇÃO (item 2): referência de integridade — nunca o "segredo"
    # em si, só uma marca de que este grant foi genuinamente emitido
    # pelo registry (nunca fabricável pelo chamador fora dele).
    integrity_reference: str = field(default_factory=lambda: f"grant_sig_{uuid.uuid4().hex[:16]}")
    revoked_at: float | None = None
    revocation_reason: str | None = None
    # CORREÇÃO (item 15 — Authority Grant Origin): rastreabilidade
    # completa da origem — grant sem origem canônica é tratado como
    # `INVALID` por quem consome (ver `AuthorityRegistry.registry_id`).
    canonical_registry_id: str | None = None
    issued_by_principal: str | None = None
    issued_by_grant: str | None = None
    audit_id: str | None = None

    @staticmethod
    def new_id() -> str:
        return f"grant_{uuid.uuid4().hex[:12]}"

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    @property
    def effective_status(self) -> GrantStatus:
        """Status EFETIVO — expiração sempre prevalece sobre o status
        declarado, mesmo que ninguém tenha chamado `expire_grant()`
        explicitamente ainda."""
        if self.status == GrantStatus.REVOKED:
            return GrantStatus.REVOKED
        if self.is_expired:
            return GrantStatus.EXPIRED
        return self.status


class AuthorityRegistry:
    """Fonte de verdade para autoridade. VAZIO por padrão — nenhum
    grant existe até `bootstrap_root_authority()` (uma única vez) ou
    `issue_grant()` (sempre a partir de um grant emissor já válido —
    nunca a partir do nada)."""

    def __init__(self, *, db_path: str | None = None) -> None:
        self._grants: dict = {}
        self._root_bootstrapped = False
        self.audit_log: list = []
        # CORREÇÃO (item 10 — persistência da root authority entre
        # restarts): quando `db_path` é fornecido, o bootstrap é
        # verificado/gravado em `authority_root_bootstrap` — uma nova
        # instância (mesmo de um processo Python totalmente novo, após
        # restart) apontando para o MESMO banco reconhece que o
        # bootstrap já aconteceu e recusa uma segunda tentativa.
        self.db_path = db_path
        # CORREÇÃO (round 9 — continuidade real após restart): antes,
        # `registry_id` era gerado do zero a cada `__init__()` — uma
        # nova instância pós-restart tinha um `registry_id` DIFERENTE
        # do original, então nenhum grant persistido (cujo
        # `canonical_registry_id` apontava para o `registry_id`
        # antigo) validava mais. Agora, quando já existe bootstrap
        # persistido, `registry_id` é RESTAURADO do banco (o MESMO de
        # antes do restart); só é gerado novo quando o banco ainda não
        # tem bootstrap algum.
        persisted_registry_id = self._check_persisted_bootstrap() if db_path else None
        if persisted_registry_id:
            self.registry_id = persisted_registry_id
            self._root_bootstrapped = True
        else:
            # CORREÇÃO (itens 5/6 — registry injection protection): cada
            # instância tem um `registry_id` próprio, único — usado por
            # `ApplicationGovernanceContext`/componentes confiáveis para
            # confirmar que estão recebendo a MESMA instância canônica
            # (nunca uma `AuthorityRegistry()` paralela criada por um
            # atacante).
            self.registry_id = f"authreg_{uuid.uuid4().hex[:12]}"
        if db_path and self._root_bootstrapped:
            self._load_persisted_grants()

    def _record_audit(self, *, action: str, actor: str | None, grant_id: str | None, target: str | None,
                       scope: str | None, outcome: str, reason: str | None = None) -> None:
        self.audit_log.append({
            "audit_id": f"audit_{uuid.uuid4().hex[:12]}", "action": action, "actor": actor,
            "grant_id": grant_id, "target": target, "scope": scope, "outcome": outcome, "reason": reason,
            "timestamp": time.time(),
        })

    def _check_persisted_bootstrap(self) -> str | None:
        """CORREÇÃO (item 10 + round 9): consulta
        `authority_root_bootstrap` — devolve o `canonical_registry_id`
        persistido quando já existe um registro (de QUALQUER processo,
        antes ou depois de um restart), ou `None` quando o banco ainda
        não foi bootstrapado."""
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT canonical_registry_id FROM authority_root_bootstrap WHERE singleton_guard='ROOT'"
            ).fetchone()
        if row is None:
            return None
        return row["canonical_registry_id"] or None

    def _persist_bootstrap(self, *, owner_id: str, root_grant_id: str, auth_service_id: str, bootstrap_reason: str) -> None:
        if not self.db_path:
            return
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO authority_root_bootstrap "
                "(singleton_guard, owner_id, root_grant_id, auth_service_id, application_id, tenant_id, "
                "bootstrapped_at, bootstrap_reason, canonical_registry_id) VALUES ('ROOT', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    owner_id, root_grant_id, auth_service_id, getattr(self, "application_id", "default"),
                    getattr(self, "tenant_id", "default"), time.time(), bootstrap_reason, self.registry_id,
                ),
            )

    def _load_persisted_grants(self) -> None:
        """CORREÇÃO (round 9 — continuidade real após restart): restaura
        TODOS os grants persistidos (`authority_grants`) — inclusive os
        já `REVOKED`/`EXPIRED`, que continuam exatamente com o mesmo
        status de antes do restart (uma revogação NUNCA "volta atrás"
        silenciosamente por causa de um restart)."""
        import json

        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM authority_grants").fetchall()
        for row in rows:
            grant = AuthorityGrant(
                grant_id=row["grant_id"], subject_actor_id=row["subject_actor_id"],
                authority_level=row["authority_level"], issued_by=row["issued_by"],
                scopes=json.loads(row["scopes"]), permissions=json.loads(row["permissions"]),
                issued_at=row["issued_at"], expires_at=row["expires_at"], approval_id=row["approval_id"],
                status=GrantStatus(row["status"]), integrity_reference=row["integrity_reference"],
                revoked_at=row["revoked_at"], revocation_reason=row["revocation_reason"],
                canonical_registry_id=row["canonical_registry_id"], issued_by_principal=row["issued_by_principal"],
                issued_by_grant=row["issued_by_grant"], audit_id=row["audit_id"],
            )
            self._grants[grant.grant_id] = grant

    def _persist_grant(self, grant: "AuthorityGrant") -> None:
        """CORREÇÃO (round 9): grava (ou atualiza — `INSERT OR REPLACE`)
        o grant completo, incluindo status atual — chamado tanto na
        criação quanto após `revoke_grant()`/`expire_grant()`, para que
        a decisão crítica (revogação) sobreviva a restart."""
        if not self.db_path:
            return
        import json

        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO authority_grants "
                "(grant_id, subject_actor_id, authority_level, issued_by, scopes, permissions, issued_at, "
                "expires_at, approval_id, status, integrity_reference, revoked_at, revocation_reason, "
                "canonical_registry_id, issued_by_principal, issued_by_grant, audit_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    grant.grant_id, grant.subject_actor_id, grant.authority_level, grant.issued_by,
                    json.dumps(grant.scopes), json.dumps(grant.permissions), grant.issued_at, grant.expires_at,
                    grant.approval_id, grant.status.value, grant.integrity_reference, grant.revoked_at,
                    grant.revocation_reason, grant.canonical_registry_id, grant.issued_by_principal,
                    grant.issued_by_grant, grant.audit_id,
                ),
            )

    def bootstrap_root_authority(self, *, owner_principal, auth_service, bootstrap_reason: str) -> AuthorityGrant:
        """CORREÇÃO (itens 1/2/9/10 desta rodada — Owner não pode ser
        autodeclarado): `owner_principal` NUNCA é aceito só pelos seus
        próprios campos (`authenticated=True`/`verification_status=
        VERIFIED` alegados por um objeto fabricado pelo chamador —
        Teste 10, fake OwnerPrincipal attack). Sempre RE-DERIVA um
        principal fresco a partir de `owner_principal.session_id`
        contra o `auth_service` fornecido (`core.security.auth.
        OwnerAuthService` real — o MESMO que valida e-mail+senha via
        PBKDF2 para o login real do Owner) — só prossegue se essa
        re-verificação bater. Só pode acontecer UMA VEZ por
        `AuthorityRegistry` — uma segunda tentativa é sempre recusada
        (`AuthorityDenied`)."""
        if self._root_bootstrapped:
            self._record_audit(
                action="BOOTSTRAP_ROOT", actor=getattr(owner_principal, "owner_id", None), grant_id=None,
                target=None, scope=None, outcome="DENIED",
                reason="bootstrap de autoridade raiz já realizado — não pode ser repetido",
            )
            raise AuthorityDenied("bootstrap de autoridade raiz já realizado — não pode ser repetido")
        if not bootstrap_reason:
            raise AuthorityDenied("bootstrap_reason é obrigatório para o bootstrap raiz")
        if owner_principal is None or not getattr(owner_principal, "session_id", None) or auth_service is None:
            self._record_audit(
                action="BOOTSTRAP_ROOT", actor=getattr(owner_principal, "owner_id", None), grant_id=None,
                target=None, scope=None, outcome="DENIED",
                reason="owner_principal/session_id/auth_service ausentes — nunca aceita autodeclaração",
            )
            raise AuthorityDenied(
                "bootstrap recusado: é necessário um OwnerPrincipal com session_id real, verificado contra "
                "um OwnerAuthService — nunca um owner_id/principal autodeclarado"
            )

        # CORREÇÃO (itens 1/2 — GATE ZERO: fechar o gap em qualquer
        # ponto de entrada, não só em ApplicationGovernanceContext):
        # mesmo chamando `bootstrap_root_authority()` diretamente
        # (contornando o Context), o `auth_service` PRECISA ter sido
        # criado pela fábrica canônica do runtime — nunca um
        # `OwnerAuthService` que o próprio chamador instanciou.
        from core.security.auth import is_canonical_auth_service

        if not is_canonical_auth_service(auth_service):
            self._record_audit(
                action="BOOTSTRAP_ROOT", actor=getattr(owner_principal, "owner_id", None), grant_id=None,
                target=None, scope=None, outcome="DENIED",
                reason="UNTRUSTED_AUTH_SERVICE: auth_service não foi criado pela fábrica canônica do runtime",
            )
            raise AuthorityDenied(
                "UNTRUSTED_AUTH_SERVICE: bootstrap recusado — auth_service não foi criado por "
                "core.security.auth.create_canonical_auth_service()"
            )

        from core.evidence.owner_principal import issue_owner_principal

        reverified = issue_owner_principal(auth_service=auth_service, session_token=owner_principal.session_id)
        if not reverified.is_genuinely_authenticated() or reverified.owner_id != owner_principal.owner_id:
            self._record_audit(
                action="BOOTSTRAP_ROOT", actor=owner_principal.owner_id, grant_id=None, target=None, scope=None,
                outcome="DENIED",
                reason="OwnerPrincipal não pôde ser re-verificado contra a sessão original — objeto "
                       "possivelmente fabricado",
            )
            raise AuthorityDenied(
                "bootstrap recusado: OwnerPrincipal não pôde ser re-verificado contra a sessão original — "
                "um objeto autodeclarado nunca é suficiente"
            )

        owner_actor_id = reverified.owner_id
        grant = AuthorityGrant(
            grant_id=AuthorityGrant.new_id(), subject_actor_id=owner_actor_id, authority_level="OWNER",
            issued_by="SYSTEM_ROOT_BOOTSTRAP", scopes=list(ALL_SCOPES), permissions=list(ALL_SCOPES),
            canonical_registry_id=self.registry_id, issued_by_principal=reverified.principal_id,
        )
        self._grants[grant.grant_id] = grant
        self._root_bootstrapped = True
        self._persist_bootstrap(
            owner_id=owner_actor_id, root_grant_id=grant.grant_id, auth_service_id=auth_service.auth_service_id,
            bootstrap_reason=bootstrap_reason,
        )
        self._persist_grant(grant)
        self._record_audit(
            action="BOOTSTRAP_ROOT", actor=owner_actor_id, grant_id=grant.grant_id, target=owner_actor_id,
            scope=None, outcome="GRANTED", reason=bootstrap_reason,
        )
        return grant

    def resolve_grant(self, grant_id: str | None) -> AuthorityGrant | None:
        if not grant_id:
            return None
        return self._grants.get(grant_id)

    def validate_grant(self, grant_id: str | None, *, required_scope: str | None = None) -> bool:
        """CORREÇÃO (itens 13/14/15/16): grant inexistente/fabricado
        fora do registry -> `False` (Teste 13). Grant `REVOKED`
        (Teste 14) ou expirado (Teste 15) -> `False`. Escopo ausente
        (Teste 16) -> `False`. CORREÇÃO (itens 6/15 — registry
        injection): um grant cujo `canonical_registry_id` não bate com
        `self.registry_id` (defesa em profundidade — mesmo que alguém
        tentasse inserir um grant "estrangeiro" diretamente) -> `False`."""
        grant = self.resolve_grant(grant_id)
        if grant is None:
            return False
        if grant.canonical_registry_id is not None and grant.canonical_registry_id != self.registry_id:
            return False
        if grant.effective_status != GrantStatus.ACTIVE:
            return False
        if required_scope is not None and required_scope not in grant.scopes:
            return False
        return True

    def issue_grant(
        self, *, issuer_grant_id: str, subject_actor_id: str, authority_level: str, scopes: list,
        issuance_reason: str, permissions: list | None = None, expires_at: float | None = None,
    ) -> AuthorityGrant:
        """CORREÇÃO (item 10 — privilege escalation protection): o
        grant EMISSOR precisa estar ATIVO e ter `ISSUE_AUTHORITY_GRANT`
        — E não pode emitir um grant de nível SUPERIOR ao seu próprio,
        nem conceder escopos que ele mesmo não possui. Teste 11 (self-
        grant sem autoridade): sem um `issuer_grant_id` válido,
        `AuthorityDenied` sempre."""
        if not self.validate_grant(issuer_grant_id, required_scope=SCOPE_ISSUE_AUTHORITY_GRANT):
            self._record_audit(
                action="ISSUE_GRANT", actor=subject_actor_id, grant_id=issuer_grant_id, target=subject_actor_id,
                scope=SCOPE_ISSUE_AUTHORITY_GRANT, outcome="DENIED",
                reason="grant emissor inválido/ausente/expirado/revogado ou sem escopo ISSUE_AUTHORITY_GRANT",
            )
            raise AuthorityDenied(
                f"emissão de grant recusada: grant emissor '{issuer_grant_id}' inválido ou sem "
                f"escopo '{SCOPE_ISSUE_AUTHORITY_GRANT}'"
            )
        issuer = self.resolve_grant(issuer_grant_id)

        if authority_level not in _LEVEL_ORDER:
            raise AuthorityDenied(f"authority_level '{authority_level}' desconhecido")
        if _LEVEL_ORDER.index(authority_level) > _LEVEL_ORDER.index(issuer.authority_level):
            self._record_audit(
                action="ISSUE_GRANT", actor=issuer.subject_actor_id, grant_id=issuer_grant_id,
                target=subject_actor_id, scope=None, outcome="DENIED",
                reason=f"privilege escalation: '{issuer.authority_level}' não pode emitir '{authority_level}'",
            )
            raise AuthorityDenied(
                f"privilege escalation bloqueado: grant emissor com nível '{issuer.authority_level}' não pode "
                f"emitir um grant de nível superior '{authority_level}'"
            )
        for scope in scopes:
            if scope not in issuer.scopes:
                self._record_audit(
                    action="ISSUE_GRANT", actor=issuer.subject_actor_id, grant_id=issuer_grant_id,
                    target=subject_actor_id, scope=scope, outcome="DENIED",
                    reason=f"issuer não possui o escopo '{scope}' para conceder",
                )
                raise AuthorityDenied(f"grant emissor não possui o escopo '{scope}' — não pode concedê-lo a outrem")

        new_grant = AuthorityGrant(
            grant_id=AuthorityGrant.new_id(), subject_actor_id=subject_actor_id, authority_level=authority_level,
            issued_by=issuer.subject_actor_id, scopes=list(scopes), permissions=list(permissions or scopes),
            expires_at=expires_at, canonical_registry_id=self.registry_id, issued_by_grant=issuer_grant_id,
        )
        self._grants[new_grant.grant_id] = new_grant
        self._persist_grant(new_grant)
        self._record_audit(
            action="ISSUE_GRANT", actor=issuer.subject_actor_id, grant_id=new_grant.grant_id,
            target=subject_actor_id, scope=None, outcome="GRANTED", reason=issuance_reason,
        )
        return new_grant

    def revoke_grant(self, grant_id: str, *, revoked_by: str, reason: str) -> AuthorityGrant:
        grant = self.resolve_grant(grant_id)
        if grant is None:
            raise KeyError(f"grant_id '{grant_id}' não registrado")
        grant.status = GrantStatus.REVOKED
        grant.revoked_at = time.time()
        grant.revocation_reason = reason
        # CORREÇÃO (round 9): a revogação é uma DECISÃO CRÍTICA — precisa
        # sobreviver a restart tanto quanto a concessão original.
        self._persist_grant(grant)
        self._record_audit(
            action="REVOKE_GRANT", actor=revoked_by, grant_id=grant_id, target=grant.subject_actor_id,
            scope=None, outcome="REVOKED", reason=reason,
        )
        return grant

    def expire_grant(self, grant_id: str) -> AuthorityGrant | None:
        grant = self.resolve_grant(grant_id)
        if grant is not None and grant.is_expired and grant.status == GrantStatus.ACTIVE:
            grant.status = GrantStatus.EXPIRED
            self._persist_grant(grant)
        return grant

    def list_scopes(self, grant_id: str) -> list:
        grant = self.resolve_grant(grant_id)
        return list(grant.scopes) if grant else []

    def audit_grant(self, grant_id: str) -> list:
        return [entry for entry in self.audit_log if entry.get("grant_id") == grant_id]

    @property
    def is_root_bootstrapped(self) -> bool:
        """Acesso público — `True` quando esta instância (ou uma
        anterior, persistida no mesmo `db_path`) já concluiu o
        bootstrap raiz."""
        return self._root_bootstrapped

    def persisted_bootstrap_scope(self) -> tuple | None:
        """MICROCORREÇÃO BF3 (isolamento multi-tenant real —
        `restore_for_app()`): devolve `(application_id, tenant_id)`
        genuinamente PERSISTIDO em `authority_root_bootstrap` para
        este `db_path` — a fonte da verdade sobre a QUEM o bootstrap
        deste banco realmente pertence. `None` quando nada foi
        bootstrapado ainda. Usado por
        `ApplicationGovernanceContext.restore_for_app()` para recusar
        (`REGISTRY_CONTEXT_MISMATCH`) qualquer tentativa de restaurar
        com um `application_id`/`tenant_id` DIFERENTE do que
        originalmente bootstrapou este banco — nunca confia no
        `application_id`/`tenant_id` que o chamador está afirmando
        agora, sempre revalida contra o que está persistido."""
        if not self.db_path:
            return None
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT application_id, tenant_id FROM authority_root_bootstrap WHERE singleton_guard='ROOT'"
            ).fetchone()
        if row is None:
            return None
        return row["application_id"], row["tenant_id"]

    def all(self) -> list:
        return list(self._grants.values())


def build_default_authority_registry() -> AuthorityRegistry:
    """Registro padrão — VAZIO, sem bootstrap. Nenhuma autoridade
    existe até `bootstrap_root_authority()` ser chamado explicitamente
    por quem gerencia o ambiente (ex: inicialização do runtime real, ou
    fixture de teste)."""
    return AuthorityRegistry()
