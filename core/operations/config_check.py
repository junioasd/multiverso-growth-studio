"""
core.operations.config_check — validação da configuração de PRODUÇÃO.
Nunca gera nem exibe segredo: só verifica presença, placeholder e tamanho.
"""
from __future__ import annotations

import os
import tempfile

_PLACEHOLDERS = ("CHANGE_ME", "CHANGEME", "PLACEHOLDER", "YOUR_", "EXAMPLE", "SECRET_HERE", "TODO", "XXX")
_EPHEMERAL_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/shm/", "/private/tmp/", "/run/user/")


def is_production(env=None) -> bool:
    env = env or os.environ
    return (env.get("APP_ENV") or "").lower() in ("production", "prod")


def _placeholder(v: str) -> bool:
    return any(p in v.upper() for p in _PLACEHOLDERS)


def check_db_path(env=None) -> dict:
    env = env or os.environ
    p = env.get("DB_PATH")
    if not p:
        return {"status": "REQUIRES_OWNER_CONFIGURATION", "detail": "DB_PATH não definido explicitamente"}
    ap = os.path.abspath(p)
    tmp = os.path.abspath(tempfile.gettempdir()) + os.sep
    if ap.startswith(_EPHEMERAL_PREFIXES) or ap.startswith(tmp):
        return {"status": "REQUIRES_OWNER_CONFIGURATION", "detail": f"DB_PATH em local temporário/efêmero: {ap}"}
    code_dir = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))) + os.sep
    if ap.startswith(code_dir):
        return {"status": "REQUIRES_OWNER_CONFIGURATION", "detail": "DB_PATH dentro do diretório do código — use volume persistente dedicado"}
    return {"status": "READY", "detail": "DB_PATH explícito fora de local temporário e fora do código (volume não auditável daqui)"}


def check_secret(name: str, env=None, min_len: int = 32) -> dict:
    env = env or os.environ
    v = env.get(name)
    if not v:
        return {"status": "REQUIRES_OWNER_CONFIGURATION", "detail": f"{name} ausente"}
    if _placeholder(v):
        return {"status": "REJECTED_PLACEHOLDER", "detail": f"{name} contém placeholder"}
    if len(v) < min_len:
        return {"status": "REJECTED_WEAK", "detail": f"{name} curto demais (< {min_len})"}
    return {"status": "READY", "detail": f"{name} presente (valor não exibido)"}


def check_owner(env=None) -> dict:
    env = env or os.environ
    if not env.get("OWNER_EMAIL") or not env.get("OWNER_PASSWORD_HASH"):
        return {"status": "REQUIRES_OWNER_CONFIGURATION", "detail": "OWNER_EMAIL/OWNER_PASSWORD_HASH ausentes — usar `main.py set-owner-password`"}
    if _placeholder(env["OWNER_PASSWORD_HASH"]):
        return {"status": "REJECTED_PLACEHOLDER", "detail": "OWNER_PASSWORD_HASH é placeholder"}
    return {"status": "READY", "detail": "credencial do Owner presente (valor não exibido)"}


def check_tls(env=None) -> dict:
    env = env or os.environ
    if (env.get("BEHIND_TLS_PROXY") or "").lower() in ("1", "true", "yes"):
        return {"status": "DECLARED_NOT_VALIDATED", "detail": "Owner declarou proxy TLS — certificado real não verificável daqui"}
    return {"status": "PUBLIC_EXPOSURE_NOT_READY", "detail": "sem proxy TLS declarado — não expor publicamente"}


def cookie_secure(env=None) -> bool:
    env = env or os.environ
    return is_production(env) or (env.get("COOKIE_SECURE") or "").lower() in ("1", "true", "yes")


def production_config_report(env=None) -> dict:
    env = env or os.environ
    return {"db_path": check_db_path(env), "vault_master_key": check_secret("MGS_VAULT_MASTER_KEY", env),
            "session_secret": check_secret("SESSION_SECRET", env), "owner_credential": check_owner(env),
            "tls": check_tls(env), "cookie_secure": cookie_secure(env), "production_mode": is_production(env)}
