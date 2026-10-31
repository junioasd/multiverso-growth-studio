"""
core.evidence.governance_context
====================================
CORREÇÃO (round 9 — GATE ZERO: fechar definitivamente a fabricação de
contexto/registry/auth_service).

`ApplicationGovernanceContext.bootstrap_for_app()` é o ÚNICO caminho
para montar uma cadeia de confiança "trusted" — e agora:

    1. NUNCA aceita um `auth_service` arbitrário do chamador (item
       1/2) — exige que tenha sido criado por
       `core.security.auth.create_canonical_auth_service()` (marcador
       de processo verificado via `is_canonical_auth_service()`); caso
       contrário, `UNTRUSTED_AUTH_SERVICE`.
    2. Marca o próprio contexto resultante com um marcador de
       canonicidade (item 6/20) — `is_canonical_governance_context()`
       distingue um contexto genuinamente bootstrapado de um objeto
       `ApplicationGovernanceContext(...)` construído manualmente por
       um atacante.
    3. Registra `application_id`/`tenant_id`/`governance_context_id`
       em TODOS os registries que cria (item 7/8) — checáveis via
       `require_same_context()` (`REGISTRY_CONTEXT_MISMATCH` quando
       não batem).
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field

from core.evidence.authority_registry import (
    SCOPE_REGISTER_SOURCE, SCOPE_REGISTER_VERIFIER, AuthorityRegistry,
)
from core.evidence.claim_registry import ClaimRegistry
from core.evidence.owner_principal import issue_owner_principal
from core.evidence.source_registry import SourceRegistry
from core.evidence.verifier_registry import VerifierRegistry

# CORREÇÃO (item 6/20): marcador de processo para distinguir um
# `ApplicationGovernanceContext` genuinamente emitido por
# `bootstrap_for_app()` de um objeto fabricado manualmente pelo
# chamador (mesmo com campos idênticos).
_RUNTIME_CANONICAL_CONTEXT_MARKER = secrets.token_hex(32)


class GovernanceContextError(Exception):
    """Levantado quando um registry/contexto/auth_service apresentado
    não é genuinamente canônico — `UNTRUSTED_AUTH_SERVICE`,
    `UNTRUSTED_GOVERNANCE_CONTEXT`, `AUTHORITY_REGISTRY_REJECTED`,
    `REGISTRY_CONTEXT_MISMATCH` (todas usam esta mesma exceção, com o
    código específico na mensagem — ver `error_code`)."""

    def __init__(self, message: str, *, error_code: str = "GOVERNANCE_DENIED"):
        super().__init__(message)
        self.error_code = error_code


@dataclass
class ApplicationGovernanceContext:
    context_id: str
    application_id: str
    tenant_id: str
    authority_registry: AuthorityRegistry
    verifier_registry: VerifierRegistry
    source_registry: SourceRegistry
    claim_registry: ClaimRegistry
    owner_id: str
    auth_service_id: str
    created_at: float = field(default_factory=time.time)
    _canonical_marker: str | None = field(default=None, repr=False)
    _bootstrap_verifier_grant_id: str | None = field(default=None, repr=False)
    _bootstrap_source_grant_id: str | None = field(default=None, repr=False)

    @classmethod
    def bootstrap_for_app(
        cls, *, auth_service, session_token: str, bootstrap_reason: str,
        application_id: str = "default", tenant_id: str = "default", db_path: str | None = None,
    ) -> "ApplicationGovernanceContext":
        """CORREÇÃO (itens 1/2/3 — GATE ZERO): recusa
        (`GovernanceContextError`, `error_code='UNTRUSTED_AUTH_SERVICE'`)
        qualquer `auth_service` que não tenha sido criado por
        `create_canonical_auth_service()` — mesmo uma implementação
        IDÊNTICA criada externamente pelo chamador. Também confirma
        `application_id`/`tenant_id` batem com os do próprio
        `auth_service` (item 24/25 — isolamento)."""
        from core.security.auth import is_canonical_auth_service

        if not is_canonical_auth_service(auth_service):
            raise GovernanceContextError(
                "UNTRUSTED_AUTH_SERVICE: auth_service não foi criado pela fábrica canônica do runtime "
                "(core.security.auth.create_canonical_auth_service) — nunca aceito como fonte de confiança",
                error_code="UNTRUSTED_AUTH_SERVICE",
            )
        if getattr(auth_service, "application_id", None) != application_id or getattr(auth_service, "tenant_id", None) != tenant_id:
            raise GovernanceContextError(
                "REGISTRY_CONTEXT_MISMATCH: auth_service pertence a application_id/tenant_id diferentes",
                error_code="REGISTRY_CONTEXT_MISMATCH",
            )

        principal = issue_owner_principal(auth_service=auth_service, session_token=session_token)
        if not principal.is_genuinely_authenticated():
            raise GovernanceContextError(
                "não foi possível montar o contexto de governança: Owner não autenticado (sessão inválida/"
                "expirada/revogada, ou papel diferente de OWNER)",
                error_code="BOOTSTRAP_DENIED",
            )

        authority_registry = AuthorityRegistry(db_path=db_path)
        authority_registry.application_id = application_id
        authority_registry.tenant_id = tenant_id
        root_grant = authority_registry.bootstrap_root_authority(
            owner_principal=principal, auth_service=auth_service, bootstrap_reason=bootstrap_reason,
        )
        v_grant = authority_registry.issue_grant(
            issuer_grant_id=root_grant.grant_id, subject_actor_id=principal.owner_id, authority_level="OWNER",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="delegação inicial de verificação (bootstrap)",
        )
        s_grant = authority_registry.issue_grant(
            issuer_grant_id=root_grant.grant_id, subject_actor_id=principal.owner_id, authority_level="OWNER",
            scopes=[SCOPE_REGISTER_SOURCE], issuance_reason="delegação inicial de fontes (bootstrap)",
        )

        # CORREÇÃO (round 9 — continuidade real após restart): usa
        # `authority_registry.registry_id` como `context_id` — o MESMO
        # identificador estável restaurado por `restore_for_app()` após
        # um restart (nunca um UUID novo gerado a cada bootstrap/
        # restore, que quebraria a continuidade do `context_id`).
        context_id = authority_registry.registry_id
        verifier_registry = VerifierRegistry(authority_registry=authority_registry, db_path=db_path)
        source_registry = SourceRegistry(authority_registry=authority_registry, db_path=db_path)
        claim_registry = ClaimRegistry(
            db_path=db_path, application_id=application_id, tenant_id=tenant_id, governance_context_id=context_id,
        )
        for registry in (authority_registry, verifier_registry, source_registry, claim_registry):
            registry.application_id = application_id
            registry.tenant_id = tenant_id
            registry.governance_context_id = context_id

        context = cls(
            context_id=context_id, application_id=application_id, tenant_id=tenant_id,
            authority_registry=authority_registry, verifier_registry=verifier_registry,
            source_registry=source_registry, claim_registry=claim_registry, owner_id=principal.owner_id,
            auth_service_id=auth_service.auth_service_id,
        )
        context._canonical_marker = _RUNTIME_CANONICAL_CONTEXT_MARKER
        context._bootstrap_verifier_grant_id = v_grant.grant_id
        context._bootstrap_source_grant_id = s_grant.grant_id
        return context

    # Compatibilidade com a rodada anterior — delega para o método
    # canônico atual (nunca reintroduz o parâmetro auth_service livre
    # como suficiente: a checagem de canonicidade acontece igual).
    bootstrap = bootstrap_for_app

    @classmethod
    def restore_for_app(
        cls, *, auth_service, session_token: str, bootstrap_reason: str = "restauração após restart",
        application_id: str = "default", tenant_id: str = "default", db_path: str | None = None,
    ) -> "ApplicationGovernanceContext":
        """CORREÇÃO (round 9 — continuidade real após restart): caminho
        dedicado para RESTAURAR um contexto de governança já
        bootstrapado anteriormente (persistido em `db_path`) — nunca
        tenta criar um novo bootstrap (que seria corretamente recusado
        por `AuthorityDenied`). Exige a MESMA disciplina de autenticação
        de `bootstrap_for_app()` (auth_service canônico + login real) —
        restaurar a governança após um restart continua exigindo que o
        Owner se autentique de novo, nunca um bypass silencioso.
        `NOT_BOOTSTRAPPED` quando não há nada persistido para restaurar
        (nesse caso, `bootstrap_for_app()` é o caminho certo)."""
        from core.security.auth import is_canonical_auth_service

        if not is_canonical_auth_service(auth_service):
            raise GovernanceContextError(
                "UNTRUSTED_AUTH_SERVICE: auth_service não foi criado pela fábrica canônica do runtime",
                error_code="UNTRUSTED_AUTH_SERVICE",
            )
        principal = issue_owner_principal(auth_service=auth_service, session_token=session_token)
        if not principal.is_genuinely_authenticated():
            raise GovernanceContextError(
                "não foi possível restaurar o contexto de governança: Owner não autenticado",
                error_code="BOOTSTRAP_DENIED",
            )

        authority_registry = AuthorityRegistry(db_path=db_path)
        if not authority_registry.is_root_bootstrapped:
            raise GovernanceContextError(
                "NOT_BOOTSTRAPPED: nenhuma autoridade raiz persistida para restaurar — use "
                "bootstrap_for_app() para o primeiro bootstrap",
                error_code="NOT_BOOTSTRAPPED",
            )

        # MICROCORREÇÃO BF3 (isolamento multi-tenant real — falha real
        # reproduzida por auditoria independente): antes, o
        # `application_id`/`tenant_id` do CHAMADOR era aceito sem
        # nenhuma validação contra o que foi genuinamente persistido
        # em `authority_root_bootstrap` — um appB/tenantB conseguia
        # `restore_for_app()` sobre o MESMO `db_path` de um
        # appA/tenantA e "herdar" silenciosamente toda a governança
        # (incluindo claims) de A. Agora SEMPRE revalida: o
        # `application_id`/`tenant_id` persistido (a fonte da verdade
        # sobre a quem este banco pertence) precisa bater EXATAMENTE
        # com o que o chamador está pedindo restaurar — fail-closed,
        # nunca aceita o dado do chamador sozinho.
        persisted_scope = authority_registry.persisted_bootstrap_scope()
        if persisted_scope is None or persisted_scope != (application_id, tenant_id):
            persisted_app, persisted_tenant = persisted_scope if persisted_scope else (None, None)
            raise GovernanceContextError(
                f"REGISTRY_CONTEXT_MISMATCH: este banco foi bootstrapado para "
                f"application_id='{persisted_app}'/tenant_id='{persisted_tenant}' — restauração recusada para "
                f"application_id='{application_id}'/tenant_id='{tenant_id}'",
                error_code="REGISTRY_CONTEXT_MISMATCH",
            )
        authority_registry.application_id = application_id
        authority_registry.tenant_id = tenant_id

        context_id = authority_registry.registry_id  # ESTÁVEL entre restarts — mesmo id de antes
        verifier_registry = VerifierRegistry(authority_registry=authority_registry, db_path=db_path)
        source_registry = SourceRegistry(authority_registry=authority_registry, db_path=db_path)
        claim_registry = ClaimRegistry(
            db_path=db_path, application_id=application_id, tenant_id=tenant_id, governance_context_id=context_id,
        )
        for registry in (authority_registry, verifier_registry, source_registry, claim_registry):
            registry.application_id = application_id
            registry.tenant_id = tenant_id
            registry.governance_context_id = context_id

        context = cls(
            context_id=context_id, application_id=application_id, tenant_id=tenant_id,
            authority_registry=authority_registry, verifier_registry=verifier_registry,
            source_registry=source_registry, claim_registry=claim_registry, owner_id=principal.owner_id,
            auth_service_id=auth_service.auth_service_id,
        )
        context._canonical_marker = _RUNTIME_CANONICAL_CONTEXT_MARKER
        return context

    def is_canonical(self) -> bool:
        """CORREÇÃO (item 6/20): `True` somente para um contexto
        genuinamente emitido por `bootstrap_for_app()` — um
        `ApplicationGovernanceContext(...)` construído manualmente
        (sem passar pelo classmethod) nunca tem o marcador certo."""
        return self._canonical_marker == _RUNTIME_CANONICAL_CONTEXT_MARKER

    def require_canonical_registry(self, registry) -> None:
        """CORREÇÃO (item 6/7/8/21 — Teste attacker root registry):
        levanta `GovernanceContextError` (`AUTHORITY_REGISTRY_REJECTED`)
        se `registry` não for genuinamente o canônico deste contexto —
        nunca aceita uma instância "parecida" criada por fora."""
        if not self.is_canonical():
            raise GovernanceContextError(
                "UNTRUSTED_GOVERNANCE_CONTEXT: este contexto não é canônico (não foi emitido por "
                "bootstrap_for_app()) — nenhuma operação trusted pode ser realizada a partir dele",
                error_code="UNTRUSTED_GOVERNANCE_CONTEXT",
            )
        candidate_id = getattr(registry, "registry_id", None)
        if candidate_id is None and hasattr(registry, "authority_registry"):
            candidate_id = getattr(registry.authority_registry, "registry_id", None)
        if candidate_id != self.authority_registry.registry_id:
            raise GovernanceContextError(
                "AUTHORITY_REGISTRY_REJECTED: o registry apresentado não é a instância canônica deste "
                "ApplicationGovernanceContext",
                error_code="AUTHORITY_REGISTRY_REJECTED",
            )

    def require_same_tenant_app(self, registry) -> None:
        """CORREÇÃO (itens 8/24/25 — isolamento de tenant/app):
        `REGISTRY_CONTEXT_MISMATCH` quando o registry pertence a outro
        `application_id`/`tenant_id`/`governance_context_id`."""
        app_id = getattr(registry, "application_id", None)
        tenant_id = getattr(registry, "tenant_id", None)
        ctx_id = getattr(registry, "governance_context_id", None)
        if app_id != self.application_id or tenant_id != self.tenant_id or ctx_id != self.context_id:
            raise GovernanceContextError(
                f"REGISTRY_CONTEXT_MISMATCH: registry pertence a application_id='{app_id}'/"
                f"tenant_id='{tenant_id}'/governance_context_id='{ctx_id}', não a "
                f"'{self.application_id}'/'{self.tenant_id}'/'{self.context_id}'",
                error_code="REGISTRY_CONTEXT_MISMATCH",
            )


def is_canonical_governance_context(context) -> bool:
    """Checagem externa equivalente a `context.is_canonical()` — útil
    para código que só tem uma referência genérica."""
    return isinstance(context, ApplicationGovernanceContext) and context.is_canonical()
