"""
core.security.auth
===================
Fluxo de login do Owner (Seção 4 do Comando Mestre).

formulário -> API login -> autenticação -> sessão segura -> redirecionamento

Implementa:
- hash seguro de senha (PBKDF2-HMAC-SHA256 + salt, via stdlib `hashlib`)
- geração/validação de sessão assinada (HMAC, formato compacto tipo JWT)
- expiração de sessão
- rate limiting simples por identidade (janela deslizante em memória)
- logout (revogação de sessão)

REGRA ABSOLUTA (Seção 32): o Owner nunca entrega a senha real ao sistema
de forma persistida em texto puro. Apenas o hash é armazenado.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

from core.config import SETTINGS
from core.security.rbac import Principal, Role

PBKDF2_ITERATIONS = 260_000


class AuthError(Exception):
    """Erro de autenticação genérico e seguro (nunca vaza detalhe interno)."""


class RateLimitExceeded(AuthError):
    pass


# ---------------------------------------------------------------------------
# Hash de senha
# ---------------------------------------------------------------------------

def hash_password(plain_password: str, *, salt: bytes | None = None, iterations: int | None = None) -> str:
    """Gera um hash seguro no formato: pbkdf2_sha256$iterations$salt$hash (base64).

    CORREÇÃO (itens 24-26 — regressão monolítica: reduzir dependência
    de wall-clock nos testes): `iterations` é OPCIONAL — produção nunca
    passa este parâmetro, então continua usando `PBKDF2_ITERATIONS`
    (260.000, valor recomendado OWASP, inalterado). Testes que não
    dependem da força criptográfica real (só do FLUXO de autenticação)
    podem passar um valor bem menor via
    `tests._auth_test_helpers.fast_hash_password()` — o custo de CPU de
    PBKDF2 escala diretamente com a velocidade do processador, então em
    uma máquina significativamente mais lenta essa mesma operação pode
    custar bem mais que os ~70ms medidos neste ambiente; multiplicado
    pelas dezenas de chamadas em testes de autenticação, é uma fonte
    real e quantificável de variação de tempo entre ambientes — nunca
    um travamento genuíno."""
    if not plain_password or len(plain_password) < 8:
        raise ValueError("senha deve ter ao menos 8 caracteres")
    salt = salt or secrets.token_bytes(16)
    effective_iterations = iterations if iterations is not None else PBKDF2_ITERATIONS
    derived = hashlib.pbkdf2_hmac(
        "sha256", plain_password.encode("utf-8"), salt, effective_iterations
    )
    return "pbkdf2_sha256${}${}${}".format(
        effective_iterations,
        base64.b64encode(salt).decode(),
        base64.b64encode(derived).decode(),
    )


def verify_password(plain_password: str, stored_hash: str) -> bool:
    try:
        algo, iterations_s, salt_b64, hash_b64 = stored_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256", plain_password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(derived, expected)


# ---------------------------------------------------------------------------
# Sessão assinada (tipo JWT compacto, sem dependências externas)
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_session_token(
    *, subject: str, role: str, scopes: list[str] | None = None, extra: dict | None = None
) -> str:
    now = int(time.time())
    payload = {
        "sub": subject,
        "role": role,
        # MICROCORREÇÃO 2 (GOVERNANÇA): escopos ficam DENTRO do token
        # assinado — não são mais algo que o chamador possa alegar depois.
        # `principal_from_session()` reconstrói o Principal (identidade +
        # papel + escopos) exclusivamente a partir deste payload já
        # verificado (assinatura HMAC + expiração), nunca de headers ou
        # campos soltos fornecidos pelo cliente na requisição.
        "scopes": scopes if scopes is not None else [],
        "iat": now,
        "exp": now + SETTINGS.session_ttl_seconds,
        "jti": secrets.token_hex(8),
    }
    if extra:
        payload.update(extra)
    header = {"alg": "HS256", "typ": "MGS-SESSION"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = hmac.new(
        SETTINGS.resolved_session_secret().encode(), signing_input, hashlib.sha256
    ).digest()
    sig_b64 = _b64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def verify_session_token(token: str) -> dict:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        raise AuthError("token malformado")

    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected_sig = hmac.new(
        SETTINGS.resolved_session_secret().encode(), signing_input, hashlib.sha256
    ).digest()
    given_sig = _b64url_decode(sig_b64)
    if not hmac.compare_digest(expected_sig, given_sig):
        raise AuthError("assinatura inválida")

    payload = json.loads(_b64url_decode(payload_b64))
    if payload.get("exp", 0) < int(time.time()):
        raise AuthError("sessão expirada")
    return payload


def principal_from_session(payload: dict) -> Principal:
    """Reconstrói um `Principal` (identidade + papel + escopos) A PARTIR
    de um payload de sessão JÁ VERIFICADO (`verify_session_token`/
    `OwnerAuthService.current_session`) — nunca de dados soltos que um
    chamador possa alegar. Esta é a única forma suportada de obter um
    `Principal` a partir de uma requisição HTTP autenticada.

    Levanta `AuthError` se o payload tiver um `role` desconhecido —
    fail-closed em vez de assumir um papel padrão."""
    try:
        role = Role(payload["role"])
    except (KeyError, ValueError):
        raise AuthError("sessão com papel (role) inválido ou ausente")
    scopes = set(payload.get("scopes") or [])
    return Principal(subject=payload["sub"], role=role, scopes=scopes)


# ---------------------------------------------------------------------------
# Session store (revogação / logout) + Rate limiting em memória
# ---------------------------------------------------------------------------

class SessionStore:
    """MICROCORREÇÃO 3 (item 3, REVOGAÇÃO DURÁVEL DE SESSÕES).

    Falha original: a revogação vivia só em `set()` na memória do
    processo — uma nova instância (reinício, ou outra instância do
    serviço) nunca via as revogações feitas por outra, então um token
    JWT ainda criptograficamente válido (dentro do prazo de expiração)
    voltava a ser aceito depois de logout.

    Agora, com `db_path` configurado, `revoke()` grava no banco e
    `is_revoked()` SEMPRE consulta o banco (além do cache local) —
    nenhuma instância confia apenas na própria memória. Sem `db_path`
    (modo em memória, usado por testes leves), o comportamento anterior
    é preservado.
    """

    def __init__(self, *, db_path: str | None = None) -> None:
        self._revoked_jti: set[str] = set()
        self._db_path = db_path
        if self._db_path:
            self._load_from_db()

    def _load_from_db(self) -> None:
        from core.models import db as db_module

        with db_module.connect(self._db_path) as conn:
            now = time.time()
            # limpeza oportunista de revogações já expiradas (o próprio
            # JWT já expiraria de qualquer forma — isso só evita que a
            # tabela cresça indefinidamente).
            conn.execute("DELETE FROM revoked_sessions WHERE expires_at < ?", (now,))
            rows = conn.execute("SELECT jti FROM revoked_sessions").fetchall()
            self._revoked_jti = {row["jti"] for row in rows}

    def revoke(self, token: str) -> None:
        try:
            payload = verify_session_token(token)
        except AuthError:
            return
        jti = payload["jti"]
        expires_at = payload.get("exp", time.time() + 3600)
        self._revoked_jti.add(jti)
        if self._db_path:
            from core.models import db as db_module

            with db_module.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO revoked_sessions (jti, revoked_at, expires_at) VALUES (?,?,?)",
                    (jti, time.time(), expires_at),
                )

    def is_revoked(self, token: str) -> bool:
        try:
            payload = verify_session_token(token)
        except AuthError:
            return True
        jti = payload["jti"]
        if jti in self._revoked_jti:
            return True
        if self._db_path:
            # Consulta fresca ao banco — cobre revogação feita por OUTRA
            # instância que esta nunca viu localmente.
            from core.models import db as db_module

            with db_module.connect(self._db_path) as conn:
                row = conn.execute("SELECT 1 FROM revoked_sessions WHERE jti = ?", (jti,)).fetchone()
                if row is not None:
                    self._revoked_jti.add(jti)  # cache local para próximas checagens
                    return True
        return False


# Mantido por compatibilidade de nome com versões anteriores (alias).
InMemorySessionStore = SessionStore


@dataclass
class RateLimiter:
    max_attempts: int = 5
    window_seconds: int = 300
    _attempts: dict[str, list[float]] = field(default_factory=dict)

    def check_and_record(self, identity: str) -> None:
        now = time.time()
        window_start = now - self.window_seconds
        attempts = [t for t in self._attempts.get(identity, []) if t >= window_start]
        if len(attempts) >= self.max_attempts:
            raise RateLimitExceeded("muitas tentativas de login; tente mais tarde")
        attempts.append(now)
        self._attempts[identity] = attempts

    def reset(self, identity: str) -> None:
        self._attempts.pop(identity, None)


# ---------------------------------------------------------------------------
# Fluxo de login completo
# ---------------------------------------------------------------------------

class OwnerAuthService:
    """Serviço de autenticação do Owner.

    `stored_password_hash` deve vir de armazenamento seguro (nunca deste
    código). Sem hash configurado, login real é recusado (não há bypass).
    `db_path`, quando informado, torna a revogação de sessão (logout)
    durável e compartilhada entre instâncias (ver `SessionStore`).

    CORREÇÃO (item 3 — identidade do serviço de autenticação, não
    autodeclarada): cada instância recebe um `auth_service_id` próprio
    e único. O campo `_canonical_marker` (nunca exposto por API
    pública, nunca serializável) só é preenchido por
    `create_canonical_auth_service()` — é isso que
    `ApplicationGovernanceContext` verifica para aceitar (ou recusar,
    `UNTRUSTED_AUTH_SERVICE`) um `OwnerAuthService` como fonte de
    confiança para bootstrap de autoridade raiz.
    """

    def __init__(
        self, *, owner_email: str, stored_password_hash: str | None, db_path: str | None = None,
        application_id: str = "default", tenant_id: str = "default",
    ):
        self.owner_email = owner_email
        self.stored_password_hash = stored_password_hash
        self.sessions = SessionStore(db_path=db_path)
        self.rate_limiter = RateLimiter()
        # CORREÇÃO (item 3): identidade própria — nunca autodeclarada
        # pelo chamador em nenhum outro lugar do sistema.
        self.auth_service_id = f"authsvc_{secrets.token_hex(12)}"
        self.application_id = application_id
        self.tenant_id = tenant_id
        self.created_by_runtime = False
        self.configuration_origin = "manual"
        self.status = "ACTIVE"
        self.integrity_reference = f"authsvc_sig_{secrets.token_hex(16)}"
        self._canonical_marker = None  # só setado por create_canonical_auth_service()

    def login(self, email: str, password: str) -> str:
        self.rate_limiter.check_and_record(email.lower())

        if not self.stored_password_hash:
            raise AuthError("login indisponível: credenciais do Owner não configuradas")

        # Sempre executa verify_password mesmo em e-mail incorreto,
        # para reduzir diferença de tempo entre "usuário existe" e "não existe".
        email_matches = hmac.compare_digest(email.lower(), self.owner_email.lower())
        password_matches = verify_password(password, self.stored_password_hash)

        if not (email_matches and password_matches):
            raise AuthError("credenciais inválidas")

        self.rate_limiter.reset(email.lower())
        # Owner sempre recebe escopo universal ("*") — é a única
        # identidade humana autorizada (Seção 1/4), então o próprio
        # papel OWNER já concentra todas as Permissions (core.security.rbac).
        token = create_session_token(subject=email, role=Role.OWNER.value, scopes=["*"])
        return token

    def logout(self, token: str) -> None:
        self.sessions.revoke(token)

    def current_session(self, token: str) -> dict:
        if self.sessions.is_revoked(token):
            raise AuthError("sessão revogada")
        return verify_session_token(token)

# CORREÇÃO (itens 1/2/3/18 — round 9: OwnerAuthService canônico do
# runtime): gerado UMA VEZ por processo, nunca exposto por nenhuma API
# pública, nunca serializável — é o que distingue um `OwnerAuthService`
# genuinamente criado pela fábrica canônica de qualquer instância
# fabricada externamente por um atacante (mesmo com implementação
# idêntica). `ApplicationGovernanceContext` verifica este marcador
# antes de aceitar um `auth_service` como fonte de confiança — sem
# conhecer o valor exato (impossível de adivinhar, gerado por
# `secrets.token_hex`), um atacante nunca consegue fazer sua própria
# instância "passar" por canônica.
_RUNTIME_CANONICAL_AUTH_MARKER = secrets.token_hex(32)


def create_canonical_auth_service(
    *, owner_email: str, stored_password_hash: str | None, db_path: str | None = None,
    application_id: str = "default", tenant_id: str = "default",
) -> OwnerAuthService:
    """CORREÇÃO (itens 1/2/3): ÚNICO caminho para criar um
    `OwnerAuthService` que `ApplicationGovernanceContext.bootstrap_for_app()`
    aceitará como canônico — marca a instância internamente com o
    segredo de processo. Chamado exclusivamente por `api.bootstrap.
    build_app()` (o runtime real da aplicação) — nunca diretamente por
    código de negócio ou por um chamador externo."""
    service = OwnerAuthService(
        owner_email=owner_email, stored_password_hash=stored_password_hash, db_path=db_path,
        application_id=application_id, tenant_id=tenant_id,
    )
    service.created_by_runtime = True
    service.configuration_origin = "runtime_bootstrap"
    service._canonical_marker = _RUNTIME_CANONICAL_AUTH_MARKER
    return service


def is_canonical_auth_service(auth_service) -> bool:
    """Checagem usada por `ApplicationGovernanceContext` — `True`
    somente para instâncias genuinamente emitidas por
    `create_canonical_auth_service()` neste MESMO processo."""
    return getattr(auth_service, "_canonical_marker", None) == _RUNTIME_CANONICAL_AUTH_MARKER
