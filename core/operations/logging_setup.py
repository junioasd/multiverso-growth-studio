"""
core.operations.logging_setup — logging operacional persistente com REDAÇÃO.
Arquivo rotativo (limite de tamanho + retenção por quantidade), formato com
timestamp, nível, componente e correlation_id. Um filtro remove, ANTES da
escrita, os valores de segredos conhecidos do ambiente e padrões de credencial
(Bearer, token=, password=, api_key=, secret=…). Nunca registra segredo completo.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re

_SECRET_ENV_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "HASH")
_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/=]{6,})"),
    re.compile(r"(?i)((?:access_token|refresh_token|token|api[_-]?key|apikey|secret|password|senha|client_secret)"
               r"\s*[=:]\s*[\"']?)([^\s\"'&,;]{4,})"),
]
REDACTED = "[REDACTED]"
_HANDLER_TAG = "_mgs_operational_handler"


def _secret_values() -> list:
    vals = []
    for k, v in os.environ.items():
        if v and len(v) >= 6 and any(h in k.upper() for h in _SECRET_ENV_HINTS):
            vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


def redact(text: str) -> str:
    for v in _secret_values():
        text = text.replace(v, REDACTED)
    for p in _PATTERNS:
        text = p.sub(lambda m: m.group(1) + REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg, record.args = redact(msg), ()
        if not hasattr(record, "correlation_id"):
            record.correlation_id = "-"
        return True


class _RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))   # cobre também tracebacks de exceção


def configure_logging(*, log_dir: str, level: str = "INFO", max_bytes: int = 5 * 1024 * 1024,
                      backup_count: int = 10, component: str = "growth_studio") -> str:
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{component}.log")
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_TAG, False):
            root.removeHandler(h)
            h.close()
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    setattr(handler, _HANDLER_TAG, True)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(_RedactingFormatter(
        "%(asctime)s %(levelname)s [%(name)s] [cid=%(correlation_id)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    return path


def is_configured() -> bool:
    return any(getattr(h, _HANDLER_TAG, False) for h in logging.getLogger().handlers)
