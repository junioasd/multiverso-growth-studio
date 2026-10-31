"""
bridge.execution.oauth2
============================
BLOCO FINAL 2 (item 1 — OAuth2/scopes, renovação/revogação). Fluxo
REAL de autorização OAuth2 (authorization code + refresh) usando
`oauthlib` (já instalado — biblioteca de referência para OAuth2, não
uma reimplementação caseira) para construção de URL/parsing, e
`urllib.request` (stdlib) para a troca de código por token e
renovação. Nunca simula um token — sem credencial configurada ou sem
rede disponível, a troca de token falha explicitamente com o motivo
real.
"""

from __future__ import annotations

from core.operations import egress as _egress_transport

import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

from oauthlib.oauth2 import WebApplicationClient


@dataclass
class OAuth2ProviderConfig:
    """Configuração REAL de um provedor OAuth2 — endpoints e
    client_id/secret vêm de fora (nunca hardcoded para uma plataforma
    específica dentro do código)."""

    platform: str
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list = field(default_factory=list)


@dataclass
class TokenExchangeResult:
    success: bool
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float | None = None
    reason: str = ""


class OAuth2Flow:
    """Fluxo REAL de autorização — construção de URL via `oauthlib`
    (nunca uma concatenação de string caseira e frágil), troca de
    código/refresh via HTTP real (`urllib.request`). Cada etapa que
    depende de rede reporta falha explícita e real quando não pode
    completar — nunca fabrica um token."""

    def __init__(self, config: OAuth2ProviderConfig, *, timeout_seconds: float = 5.0) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds
        self._client = WebApplicationClient(config.client_id)

    def build_authorization_url(self, *, state: str | None = None) -> tuple:
        """Constrói a URL de autorização REAL via `oauthlib` — o
        usuário precisa visitar essa URL de verdade e conceder acesso;
        nada aqui é simulado. Devolve (url, state) — o `state` deve
        ser guardado pelo chamador e validado no callback (proteção
        real contra CSRF)."""
        state = state or uuid.uuid4().hex
        url = self._client.prepare_request_uri(
            self.config.authorization_endpoint, redirect_uri=self.config.redirect_uri,
            scope=self.config.scopes, state=state,
        )
        return url, state

    def exchange_code_for_token(self, *, authorization_code: str) -> TokenExchangeResult:
        """Troca REAL do código de autorização por um token — faz uma
        requisição HTTP genuína ao `token_endpoint`. Sem rede/endpoint
        alcançável, retorna falha explícita com o motivo real, nunca
        um token fabricado."""
        body = self._client.prepare_request_body(
            code=authorization_code, redirect_uri=self.config.redirect_uri,
            include_client_id=True, client_secret=self.config.client_secret,
        )
        return self._post_token_request(body)

    def refresh_access_token(self, *, refresh_token: str) -> TokenExchangeResult:
        """Renovação REAL via `refresh_token` — mesma disciplina:
        chamada HTTP genuína, nunca fabrica sucesso."""
        body = self._client.prepare_refresh_body(
            refresh_token=refresh_token, client_secret=self.config.client_secret,
        )
        return self._post_token_request(body)

    def _post_token_request(self, body: str) -> TokenExchangeResult:
        from core.operations.egress import egress_blocked   # v1.0.3: troca E renovação de token passam aqui

        _blocked = egress_blocked(self.config.token_endpoint)
        if _blocked:
            return TokenExchangeResult(success=False, reason=_blocked)
        request = urllib.request.Request(
            self.config.token_endpoint, data=body.encode("utf-8"), method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        try:
            with _egress_transport.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except _egress_transport.EgressBlocked as _eb:   # v1.0.4: bloqueio NO ponto de envio
            return TokenExchangeResult(success=False, reason=_eb.reason)
        except urllib.error.URLError as exc:
            return TokenExchangeResult(
                success=False, reason=f"falha real de rede ao trocar token OAuth2: {getattr(exc, 'reason', exc)}",
            )
        except Exception as exc:
            return TokenExchangeResult(success=False, reason=f"falha ao processar resposta OAuth2 real: {exc}")

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return TokenExchangeResult(success=False, reason=f"resposta OAuth2 não é JSON válido: {exc}")

        if "error" in parsed:
            return TokenExchangeResult(success=False, reason=f"provedor OAuth2 recusou: {parsed.get('error_description', parsed['error'])}")

        access_token = parsed.get("access_token")
        if not access_token:
            return TokenExchangeResult(success=False, reason="resposta OAuth2 sem access_token — nunca aceito como sucesso parcial")

        expires_in = parsed.get("expires_in")
        expires_at = (time.time() + float(expires_in)) if expires_in is not None else None
        return TokenExchangeResult(
            success=True, access_token=access_token, refresh_token=parsed.get("refresh_token"), expires_at=expires_at,
        )
