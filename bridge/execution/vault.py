"""
bridge.execution.vault
==========================
BLOCO FINAL 2 (item 1 — ACCESS & EXECUTION HUB: cofre seguro de
tokens/segredos). `TokenVault` — armazenamento REAL de credenciais,
criptografado em repouso via `cryptography.Fernet` (já instalado,
AES-128 em modo CBC + HMAC, autenticado). Nunca um dicionário em texto
puro disfarçado de "cofre".

Chave mestra: lida de `MGS_VAULT_MASTER_KEY` (variável de ambiente,
nunca hardcoded, nunca incluída no ZIP) — mesma disciplina de
`HttpSearchProvider`/`HttpGenerationProvider`. Sem essa variável
definida, o cofre opera em modo EFÊMERO (gera uma chave nova a cada
processo, avisado explicitamente) — útil para desenvolvimento/teste,
mas os segredos armazenados NÃO sobrevivem a um restart nesse modo
(o próprio design de criptografia impede decifrar com uma chave
diferente da que cifrou — nunca finge persistência que não existe).
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from cryptography.fernet import Fernet, InvalidToken

_ENV_MASTER_KEY = "MGS_VAULT_MASTER_KEY"


class TokenType(str, Enum):
    OAUTH2_ACCESS_TOKEN = "OAUTH2_ACCESS_TOKEN"
    OAUTH2_REFRESH_TOKEN = "OAUTH2_REFRESH_TOKEN"
    API_KEY = "API_KEY"
    WEBHOOK_SECRET = "WEBHOOK_SECRET"


class VaultKeyMode(str, Enum):
    PERSISTENT = "PERSISTENT"  # MGS_VAULT_MASTER_KEY definida -- sobrevive a restart
    EPHEMERAL = "EPHEMERAL"    # chave gerada em memória -- nunca sobrevive a restart


class SecretNotFound(KeyError):
    pass


class SecretDecryptionFailed(Exception):
    """Levantado quando a chave mestra atual não consegue decifrar um
    segredo persistido — nunca retorna um valor parcial/adivinhado."""


@dataclass
class StoredTokenMetadata:
    reference_id: str
    platform: str
    token_type: TokenType
    scopes: list = field(default_factory=list)
    expires_at: float | None = None
    created_at: float = field(default_factory=time.time)
    revoked: bool = False

    def is_expired(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at


class TokenVault:
    def __init__(self, *, db_path: str | None = None, master_key: str | None = None) -> None:
        self.db_path = db_path
        env_key = master_key if master_key is not None else os.environ.get(_ENV_MASTER_KEY)
        if env_key:
            # aceita tanto uma chave Fernet valida (44 chars base64) quanto
            # qualquer string -- deriva uma chave Fernet valida via hash real
            self._fernet = Fernet(self._derive_fernet_key(env_key))
            self.key_mode = VaultKeyMode.PERSISTENT
        else:
            from core.operations.config_check import is_production

            if is_production():
                raise RuntimeError("MGS_VAULT_MASTER_KEY ausente em PRODUÇÃO — chave efêmera não é usada como "
                                   "substituto silencioso (tokens seriam perdidos no restart)")
            self._fernet = Fernet(Fernet.generate_key())
            self.key_mode = VaultKeyMode.EPHEMERAL
        self._metadata: dict = {}
        self._encrypted_values: dict = {}
        if db_path:
            self._load_from_db()

    @staticmethod
    def _derive_fernet_key(raw: str) -> bytes:
        import hashlib

        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    def store_token(
        self, *, platform: str, token_type: TokenType, value: str, scopes: list | None = None,
        expires_at: float | None = None, reference_id: str | None = None,
    ) -> str:
        reference_id = reference_id or f"tok_{uuid.uuid4().hex[:12]}"
        encrypted = self._fernet.encrypt(value.encode("utf-8"))
        metadata = StoredTokenMetadata(
            reference_id=reference_id, platform=platform, token_type=token_type,
            scopes=list(scopes or []), expires_at=expires_at,
        )
        self._metadata[reference_id] = metadata
        self._encrypted_values[reference_id] = encrypted
        self._persist(reference_id, metadata, encrypted)
        return reference_id

    def retrieve_token(self, reference_id: str) -> str:
        if reference_id not in self._metadata:
            raise SecretNotFound(f"reference_id '{reference_id}' não encontrado no cofre")
        metadata = self._metadata[reference_id]
        if metadata.revoked:
            raise SecretNotFound(f"reference_id '{reference_id}' foi revogado")
        try:
            decrypted = self._fernet.decrypt(self._encrypted_values[reference_id])
        except InvalidToken as exc:
            raise SecretDecryptionFailed(
                f"falha ao decifrar '{reference_id}' — chave mestra atual não corresponde à usada para cifrar "
                f"(modo da chave: {self.key_mode.value})"
            ) from exc
        return decrypted.decode("utf-8")

    def metadata_for(self, reference_id: str) -> StoredTokenMetadata:
        if reference_id not in self._metadata:
            raise SecretNotFound(f"reference_id '{reference_id}' não encontrado no cofre")
        return self._metadata[reference_id]

    def revoke(self, reference_id: str) -> None:
        """Revogação REAL — marca permanentemente revogado e remove o
        valor cifrado do armazenamento; nunca reversível."""
        if reference_id not in self._metadata:
            raise SecretNotFound(f"reference_id '{reference_id}' não encontrado no cofre")
        self._metadata[reference_id].revoked = True
        self._encrypted_values.pop(reference_id, None)
        self._persist(reference_id, self._metadata[reference_id], b"")

    def needs_renewal(self, reference_id: str, *, safety_margin_seconds: float = 300.0) -> bool:
        """Verifica se o token expira dentro da margem de segurança
        (padrão 5 minutos) — usado pela lógica de renovação automática
        de OAuth2."""
        metadata = self.metadata_for(reference_id)
        if metadata.expires_at is None:
            return False
        return time.time() >= (metadata.expires_at - safety_margin_seconds)

    def list_by_platform(self, platform: str) -> list:
        return [m for m in self._metadata.values() if m.platform == platform and not m.revoked]

    # ------------------------------------------------------------------
    # Persistência
    # ------------------------------------------------------------------

    def _persist(self, reference_id: str, metadata: StoredTokenMetadata, encrypted: bytes) -> None:
        if not self.db_path:
            return
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vault_tokens "
                "(reference_id, platform, token_type, scopes, expires_at, created_at, revoked, "
                "encrypted_value, key_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reference_id, metadata.platform, metadata.token_type.value,
                    ",".join(metadata.scopes), metadata.expires_at, metadata.created_at,
                    int(metadata.revoked), encrypted, self.key_mode.value,
                ),
            )

    def _load_from_db(self) -> None:
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            for row in conn.execute("SELECT * FROM vault_tokens").fetchall():
                metadata = StoredTokenMetadata(
                    reference_id=row["reference_id"], platform=row["platform"],
                    token_type=TokenType(row["token_type"]),
                    scopes=row["scopes"].split(",") if row["scopes"] else [],
                    expires_at=row["expires_at"], created_at=row["created_at"], revoked=bool(row["revoked"]),
                )
                self._metadata[row["reference_id"]] = metadata
                if not metadata.revoked:
                    self._encrypted_values[row["reference_id"]] = row["encrypted_value"]
