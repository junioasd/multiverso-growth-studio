"""
core.config
============
Carregamento de configuração central do MULTIVERSO GROWTH STUDIO.

REGRAS (Seção 4 / 32 / 58 / 59 do Comando Mestre):
- Nenhum segredo real pode estar hardcoded neste ou em nenhum outro arquivo.
- Todos os valores sensíveis vêm de variáveis de ambiente (.env real, nunca
  commitado). O .env.example contém apenas placeholders.
- Se uma variável obrigatória não existir, o sistema deve operar em modo
  MOCK/degradado e nunca inventar um valor real.

MICROCORREÇÃO 3 (auditoria — item 5, CARREGAMENTO DA CONFIGURAÇÃO):
O guia sempre orientou preencher um `.env`, mas nada no código o
carregava de fato — apenas `os.environ` era consultado diretamente.
`_load_dotenv()` roda ANTES de `SETTINGS = Settings()` ser construído,
populando `os.environ` a partir de um arquivo `.env` (se existir),
SEM jamais sobrescrever uma variável que o ambiente real já definiu
(precedência do ambiente sobre o arquivo). O parser é deliberadamente
"burro": trata cada linha como dado literal — nunca executa comandos,
nunca expande `$VAR` (crítico: hashes de senha PBKDF2 contêm `$`
literal, ex. `pbkdf2_sha256$260000$...`), e nunca loga o conteúdo.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _dotenv_candidates() -> list[Path]:
    """Locais verificados, em ordem: diretório de trabalho atual primeiro
    (fluxo documentado: `cd multiverso_growth_studio && cp .env.example .env`),
    depois a raiz do pacote (fallback para quando o processo é iniciado de
    outro cwd, ex. em testes)."""
    candidates = [Path.cwd() / ".env"]
    try:
        project_root = Path(__file__).resolve().parent.parent
        candidates.append(project_root / ".env")
    except OSError:
        pass
    return candidates


def _parse_dotenv_line(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    # Remove UMA camada de aspas envolventes, se presentes — isso não é
    # expansão de shell, é apenas para permitir `KEY="valor com espaços"`.
    # Qualquer `$` dentro do valor (com ou sem aspas) é preservado
    # literalmente — nunca interpretado como referência a outra variável.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def _load_dotenv_file(path: Path) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                parsed = _parse_dotenv_line(raw_line)
                if parsed is None:
                    continue
                key, value = parsed
                # Precedência: uma variável já definida no ambiente real
                # NUNCA é sobrescrita pelo arquivo .env.
                if key not in os.environ:
                    os.environ[key] = value
    except OSError:
        # Falha ao ler o .env não deve derrubar a aplicação — ela
        # simplesmente opera com o que já está no ambiente (ou vazio).
        return


def _load_dotenv() -> None:
    for candidate in _dotenv_candidates():
        if candidate.is_file():
            _load_dotenv_file(candidate)
            return  # carrega apenas o primeiro .env encontrado


_load_dotenv()  # DEVE rodar antes de qualquer leitura de env abaixo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "development"))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "./data/mgs.db"))
    session_ttl_seconds: int = field(
        default_factory=lambda: int(_env("SESSION_TTL_SECONDS", "3600") or 3600)
    )

    # SESSION_SECRET nunca deve ser um valor fixo de fábrica em produção.
    # Se não configurado, geramos um segredo efêmero por processo (somente
    # dev/testes) e sinalizamos isso explicitamente.
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET", ""))
    session_secret_is_ephemeral: bool = False

    # MICROCORREÇÃO 2 (ACESSO OWNER): a inicialização padrão (`main.py`,
    # `api/bootstrap.py`) deve carregar as credenciais do Owner a partir
    # desta configuração documentada (.env), em vez de sempre cair num
    # e-mail de exemplo sem senha configurada (o que tornava login
    # impossível por padrão, mesmo com um `.env` real preenchido).
    owner_email: str = field(default_factory=lambda: _env("OWNER_EMAIL", "owner@example.com"))
    owner_password_hash: str = field(default_factory=lambda: _env("OWNER_PASSWORD_HASH", ""))

    # CORREÇÃO (item 1 — RADAR WEB COMERCIAL: autostart real no
    # runtime). Configuração persistida via ambiente — `main.py
    # cmd_serve` lê isto para decidir se inicia o worker E se habilita
    # o agendamento automaticamente. `RADAR_WEB_ENABLED=false`
    # (padrão) significa: o worker/infraestrutura pode subir junto com
    # a aplicação (quando o runtime decidir), mas o agendamento fica
    # desabilitado — nenhum scan real acontece sem configuração
    # explícita.
    radar_web_enabled: bool = field(
        default_factory=lambda: _env("RADAR_WEB_ENABLED", "false").strip().lower() in ("1", "true", "yes")
    )
    radar_web_interval_seconds: float = field(
        default_factory=lambda: float(_env("RADAR_WEB_INTERVAL_SECONDS", "3600") or 3600)
    )

    def resolved_session_secret(self) -> str:
        if self.session_secret and self.session_secret != "CHANGE_ME_GENERATE_A_RANDOM_SECRET":
            return self.session_secret
        # modo dev/teste: gera segredo efêmero em memória (não persiste)
        return _EPHEMERAL_SECRET

    def provider_key(self, name: str) -> str | None:
        """Retorna a chave de um provider externo, ou None se não configurada.
        None sempre significa: use adapter em modo MOCK."""
        value = _env(name, "")
        return value or None


_EPHEMERAL_SECRET = secrets.token_hex(32)

SETTINGS = Settings()
