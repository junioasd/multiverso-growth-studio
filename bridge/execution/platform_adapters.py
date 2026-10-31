"""
bridge.execution.platform_adapters
=======================================
BLOCO FINAL 2 (item 2/4 — NAVIGATOR REAL / integrações comerciais).
Base comum para adapters REAIS de plataforma — cada subclasse aponta
para a estrutura REAL e documentada publicamente da API oficial de
cada plataforma (YouTube Data API v3, Meta Graph API, TikTok API for
Developers) — nunca uma estrutura de endpoint inventada. Toda chamada
passa por: verificação de credencial real no `TokenVault`,
rate limiting real (`RateLimiter`), chamada HTTP genuína
(`urllib.request`), e registro na trilha de auditoria.

Nunca simula sucesso: sem credencial configurada no cofre, ou com
falha de rede real, retorna `PlatformActionResult(success=False)`
com o motivo genuíno.
"""

from __future__ import annotations

from core.operations import egress as _egress_transport

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from bridge.execution.rate_limiter import RateLimiter
from bridge.execution.vault import SecretNotFound, TokenVault


@dataclass
class PlatformActionResult:
    success: bool
    platform: str = ""
    action: str = ""
    status_code: int | None = None
    data: dict = field(default_factory=dict)
    reason: str = ""
    executed_at: float = field(default_factory=time.time)
    duration_ms: float | None = None
    # COMANDO 3/6 — estado de ENTREGA (o que se sabe sobre o efeito
    # externo), distinto de `success`:
    #   NOT_SENT   — a requisição não saiu (sem credencial, rate limit
    #                local, falha ao conectar/enviar): nada executado.
    #   REJECTED   — o serviço recusou de forma definitiva (4xx): nada executado.
    #   TRANSIENT  — o serviço recusou por ora (408/429/503): nada executado.
    #   UNKNOWN    — a requisição saiu e o resultado é DESCONHECIDO (timeout
    #                aguardando resposta, conexão caiu, 500/502/504, 2xx
    #                ilegível): o efeito PODE ter ocorrido.
    #   ACCEPTED   — aceito para processamento (202): ainda não executado.
    #   EXECUTED   — o serviço confirmou a execução (2xx com corpo válido).
    delivery: str | None = None

    def __post_init__(self) -> None:
        if self.delivery is None:
            self.delivery = "EXECUTED" if self.success else "NOT_SENT"


class PlatformApiAdapter:
    """Base REAL — subclasses definem `base_url` e os métodos
    específicos de cada ação suportada, cada um montando a URL/corpo
    REAL da API oficial documentada e chamando `self._request()`."""

    platform: str = "UNSET"
    base_url: str = "UNSET"
    default_rate_limit_capacity: float = 10.0
    default_rate_limit_refill_per_second: float = 1.0

    def __init__(
        self, *, vault: TokenVault | None = None, rate_limiter: RateLimiter | None = None,
        account_reference_id: str | None = None, timeout_seconds: float = 10.0,
    ) -> None:
        self.vault = vault
        self.rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter()
        self.account_reference_id = account_reference_id
        self.timeout_seconds = timeout_seconds

    def _rate_limit_key(self) -> str:
        return f"{self.platform}:{self.account_reference_id or 'default'}"

    def _get_access_token(self) -> str | None:
        if self.vault is None or self.account_reference_id is None:
            return None
        try:
            return self.vault.retrieve_token(self.account_reference_id)
        except SecretNotFound:
            return None

    def _request(
        self, *, action: str, method: str, path: str, params: dict | None = None, body: dict | None = None,
    ) -> PlatformActionResult:
        # v1.0.1: KILL SWITCH — chamada direta ao adapter também é bloqueada.
        from core.operations.egress import egress_blocked   # v1.0.3: guarda central

        _blocked = egress_blocked(self.base_url)
        if _blocked:
            return PlatformActionResult(success=False, platform=self.platform, action=action, delivery="NOT_SENT",
                                        reason=_blocked)
        access_token = self._get_access_token()
        if not access_token:
            return PlatformActionResult(
                success=False, platform=self.platform, action=action, delivery="NOT_SENT",
                reason=(
                    f"nenhuma credencial real configurada no cofre para '{self.platform}' "
                    f"(account_reference_id={self.account_reference_id!r}) — adapter real pronto, aguardando OAuth2"
                ),
            )

        decision = self.rate_limiter.try_acquire(key=self._rate_limit_key())
        if not decision.allowed:
            return PlatformActionResult(
                success=False, platform=self.platform, action=action, delivery="NOT_SENT",
                reason=f"rate limit local excedido para '{self._rate_limit_key()}' — tente novamente em {decision.retry_after_seconds:.1f}s",
            )

        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Authorization": f"Bearer {access_token}", "Content-Type": "application/json",
                "User-Agent": "MultiversoGrowthStudio-Navigator/1.0",
            },
        )
        start = time.time()
        try:
            with _egress_transport.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                status_code = response.status
        except _egress_transport.EgressBlocked as _eb:   # v1.0.4: bloqueio NO ponto de envio
            return PlatformActionResult(success=False, platform=self.platform, action=action, delivery="NOT_SENT", reason=_eb.reason)
        except urllib.error.HTTPError as exc:
            delivery = _classify_http_error(exc.code)
            return PlatformActionResult(
                success=False, platform=self.platform, action=action, status_code=exc.code, delivery=delivery,
                reason=f"API de {self.platform} recusou (HTTP {exc.code}): {exc.reason}"
                + (" — resultado externo DESCONHECIDO (o serviço pode ter executado)" if delivery == "UNKNOWN" else ""),
                duration_ms=(time.time() - start) * 1000,
            )
        except urllib.error.URLError as exc:
            # `urllib` só embrulha em URLError as falhas de CONEXÃO/ENVIO
            # (`h.request`); a espera pela resposta fica fora desse try.
            return PlatformActionResult(
                success=False, platform=self.platform, action=action, delivery="NOT_SENT",
                reason=f"falha real de rede ao chamar {self.platform}: {getattr(exc, 'reason', exc)}",
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as exc:
            # Timeout aguardando a resposta, conexão derrubada, resposta
            # truncada: a requisição JÁ SAIU — o efeito pode ter ocorrido.
            return PlatformActionResult(
                success=False, platform=self.platform, action=action, delivery="UNKNOWN",
                reason=f"resultado externo DESCONHECIDO após o envio ({type(exc).__name__}: {exc}) — "
                       f"não reenviar sem reconciliar",
                duration_ms=(time.time() - start) * 1000,
            )

        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            return PlatformActionResult(
                success=False, platform=self.platform, action=action, status_code=status_code, delivery="UNKNOWN",
                reason=f"resposta HTTP {status_code} de {self.platform} não é JSON válido: {exc}",
                duration_ms=(time.time() - start) * 1000,
            )
        if status_code == 202:
            return PlatformActionResult(
                success=False, platform=self.platform, action=action, status_code=status_code, data=parsed,
                delivery="ACCEPTED", reason=f"{self.platform} ACEITOU para processamento (HTTP 202) — "
                                            f"execução ainda não confirmada",
                duration_ms=(time.time() - start) * 1000,
            )
        return PlatformActionResult(
            success=True, platform=self.platform, action=action, status_code=status_code, data=parsed,
            delivery="EXECUTED", duration_ms=(time.time() - start) * 1000,
        )


def _classify_http_error(code: int) -> str:
    """HTTP de erro -> estado de entrega. 408/429/503: o serviço declara
    que NÃO processou (retry seguro). 500/502/504: o pedido pode ter sido
    processado antes da falha (ou encaminhado pelo gateway) — DESCONHECIDO.
    Demais 4xx: recusa definitiva."""
    if code in (408, 429, 503):
        return "TRANSIENT"
    if code in (500, 502, 504):
        return "UNKNOWN"
    return "REJECTED"
