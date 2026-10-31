"""
core.evidence.owner_principal
=================================
CORREÇÃO (itens 1/2/9/10 desta rodada — bootstrap root authority: Owner
não pode ser autodeclarado): antes, `bootstrap_root_authority(owner_actor_id=...)`
aceitava qualquer string não vazia — um atacante podia simplesmente
`bootstrap_root_authority("attacker")` e virar OWNER. Isso está
PROIBIDO.

`OwnerPrincipal` só pode ser emitido por `issue_owner_principal()` —
que exige um token de sessão REAL, emitido por
`core.security.auth.OwnerAuthService.login()` (assinado HMAC, com a
senha real verificada via PBKDF2) — nunca um `owner_id` alegado
diretamente. Um `OwnerPrincipal` construído manualmente pelo chamador
(`authenticated=True`, `verification_status=VERIFIED`) NUNCA passa
pela checagem de `bootstrap_root_authority()`, porque esta SEMPRE
reconfirma a autenticidade através da mesma sessão original — ver
`AuthorityRegistry.bootstrap_root_authority()`.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from core.evidence.registry import VerificationStatus


@dataclass
class OwnerPrincipal:
    principal_id: str
    owner_id: str
    authenticated: bool = False
    authentication_source: str | None = None
    session_id: str | None = None
    issued_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    scopes: list = field(default_factory=list)
    integrity_reference: str = field(default_factory=lambda: f"principal_sig_{uuid.uuid4().hex[:16]}")
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED

    @staticmethod
    def new_id() -> str:
        return f"principal_{uuid.uuid4().hex[:12]}"

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    def is_genuinely_authenticated(self) -> bool:
        """CORREÇÃO (item 10, Teste fake OwnerPrincipal): um objeto
        montado manualmente com `authenticated=True`/`verification_status
        =VERIFIED` NÃO passa aqui sozinho — quem decide isso de verdade
        é `AuthorityRegistry.bootstrap_root_authority()`, que sempre
        RE-VALIDA a sessão original (`session_id`) contra o
        `OwnerAuthService` fornecido, nunca confia apenas nestes campos."""
        return self.authenticated and self.verification_status == VerificationStatus.VERIFIED and not self.is_expired


def issue_owner_principal(*, auth_service, session_token: str) -> OwnerPrincipal:
    """CORREÇÃO (item 2): ÚNICO caminho legítimo para obter um
    `OwnerPrincipal` genuinamente autenticado — valida um
    `session_token` REAL (assinado HMAC, emitido por
    `OwnerAuthService.login()` após verificar e-mail+senha via
    PBKDF2) contra o `auth_service` fornecido. Token inválido/
    expirado/revogado, ou sessão que não é do papel OWNER
    -> `authenticated=False`, `verification_status=INVALID` — nunca
    finge sucesso."""
    from core.security.auth import AuthError
    from core.security.rbac import Role

    try:
        session = auth_service.current_session(session_token)
    except AuthError:
        return OwnerPrincipal(
            principal_id=OwnerPrincipal.new_id(), owner_id="UNKNOWN", authenticated=False,
            verification_status=VerificationStatus.INVALID,
        )

    if session.get("role") != Role.OWNER.value:
        return OwnerPrincipal(
            principal_id=OwnerPrincipal.new_id(), owner_id=session.get("sub", "UNKNOWN"), authenticated=False,
            verification_status=VerificationStatus.INVALID,
        )

    return OwnerPrincipal(
        principal_id=OwnerPrincipal.new_id(), owner_id=session["sub"], authenticated=True,
        authentication_source="OwnerAuthService", session_id=session_token,
        expires_at=session.get("exp"), scopes=list(session.get("scopes", [])),
        verification_status=VerificationStatus.VERIFIED,
    )
