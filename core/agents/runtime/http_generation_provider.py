"""
core.agents.runtime.http_generation_provider
=================================================
MICROCORREÇÃO — item 1/2 (agentes criativos: remover NullProvider
como implementação de produção). `HttpGenerationProvider` é o
equivalente de `HttpSearchProvider` para capacidades GENERATIVAS
(imagem/vídeo/áudio) — mesma disciplina: adapter HTTP real via
`urllib.request` (stdlib), credencial lida de variável de ambiente
(`MGS_GENERATION_API_KEY`/`MGS_GENERATION_API_URL`), nunca hardcoded,
nunca uma implementação permanentemente indisponível — quando uma
credencial real for configurada, `is_configured()` reflete isso
automaticamente e a chamada de rede é genuína.

Neste ambiente, sem credencial configurada e sem rede disponível,
toda chamada honestamente reporta indisponibilidade com o motivo
real — nunca uma imagem/áudio/vídeo "gerado" fabricado.
"""

from __future__ import annotations

from core.operations import egress as _egress_transport

import json
import os
import time
import urllib.error
import urllib.request

from core.agents.runtime.providers import ProviderResponse
from core.evidence.claims import ClaimType

_ENV_API_KEY = "MGS_GENERATION_API_KEY"
_ENV_API_URL = "MGS_GENERATION_API_URL"
# MICROCORREÇÃO (suite-order stability): reduzido de 30.0s para 5.0s
# (geração pode legitimamente levar mais que busca, mas 30s era um
# risco real de acumular tempo em qualquer cenário de rede lenta —
# nunca mascarando problema, apenas limitando o pior caso).
_DEFAULT_TIMEOUT_SECONDS = 5.0


class HttpGenerationProvider:
    """Provider REAL de geração de conteúdo (imagem/vídeo/áudio) via
    HTTP. Mesma disciplina de `HttpSearchProvider` — nunca finge estar
    configurado, nunca fabrica um resultado de geração."""

    name = "http_generation_provider"
    cost_classification = "PAID"      # v1.0.4: classificação de custo explícita

    def __init__(self, *, api_key: str | None = None, api_url: str | None = None, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get(_ENV_API_KEY)
        self.api_url = api_url if api_url is not None else os.environ.get(_ENV_API_URL)
        self.timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self.api_key) and bool(self.api_url)

    def call(self, *, operation: str, params: dict) -> ProviderResponse:
        from core.operations import cost_guard as _cg   # v1.0.1: custo e kill switch antes de chamada paga
        from core.operations import kill_switch as _ks

        _blocked = _ks.block_reason()
        if _blocked:
            return ProviderResponse(available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN, reason=_blocked)
        if not self.is_configured():
            return ProviderResponse(
                available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN,
                reason=(
                    f"NO_CREDENTIAL_CONFIGURED: variáveis de ambiente '{_ENV_API_KEY}'/'{_ENV_API_URL}' não "
                    "definidas nesta instalação — adapter real pronto, aguardando credencial de geração; "
                    "nunca simula um resultado gerado"
                ),
            )

        # v1.0.4: caminho único de provedor cobrável (classificação PAID).
        return _cg.run_metered_call(
            provider=self.name, classification=self.cost_classification, operation=operation, params=params,
            perform=lambda: self._perform(operation, params),
            blocked=lambda reason: ProviderResponse(available=False, provider_name=self.name,
                                                    claim_type=ClaimType.UNKNOWN, reason=reason))

    def _perform(self, operation: str, params: dict) -> tuple:
        """Chamada HTTP real. Devolve (resposta, desfecho, custo_real, moeda):
        desfecho EXECUTED | NOT_EXECUTED (certeza de que nada foi processado) |
        UNKNOWN (a requisição saiu e o resultado é incerto)."""
        from bridge.execution.platform_adapters import _classify_http_error

        from core.operations.egress import egress_blocked   # v1.0.3: guarda central imediatamente antes do I/O

        _blocked = egress_blocked(self.api_url)
        if _blocked:
            return (ProviderResponse(available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN,
                                     reason=_blocked), "NOT_EXECUTED", None, None)
        request_body = json.dumps({"operation": operation, "params": params}).encode("utf-8")
        request = urllib.request.Request(
            self.api_url, data=request_body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "MultiversoGrowthStudio/1.0",
            },
        )
        fetched_at = time.time()
        try:
            with _egress_transport.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                status_code = response.status
        except _egress_transport.EgressBlocked as _eb:   # v1.0.4: bloqueio NO ponto de envio
            return (ProviderResponse(available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN, reason=_eb.reason), "NOT_EXECUTED", None, None)
        except urllib.error.HTTPError as exc:
            kind = _classify_http_error(exc.code)
            return (ProviderResponse(
                available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN, fetched_at=fetched_at,
                reason=f"falha real de rede ao chamar provider de geração: HTTP {exc.code} ({kind})",
            ), "UNKNOWN" if kind == "UNKNOWN" else "NOT_EXECUTED", None, None)
        except urllib.error.URLError as exc:
            return (ProviderResponse(
                available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN, fetched_at=fetched_at,
                reason=f"falha real de rede ao chamar provider de geração: {getattr(exc, 'reason', exc)}",
            ), "NOT_EXECUTED", None, None)
        except Exception as exc:
            return (ProviderResponse(
                available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN, fetched_at=fetched_at,
                reason=f"falha ao processar resposta real do provider de geração: {exc}",
            ), "UNKNOWN", None, None)

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return (ProviderResponse(
                available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN, fetched_at=fetched_at,
                reason=f"resposta HTTP {status_code} recebida mas não é JSON válido: {exc}",
            ), "EXECUTED", None, None)   # o serviço respondeu 2xx: executou; custo desconhecido

        cost = parsed.get("cost")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        return (ProviderResponse(
            available=True, provider_name=self.name,
            data={
                "asset_url": parsed.get("asset_url"), "asset_id": parsed.get("asset_id"), "http_status": status_code,
            },
            confidence=float(parsed.get("confidence", 0.0)), claim_type=ClaimType.FACT, fetched_at=fetched_at,
            reason="",
        ), "EXECUTED", cost, parsed.get("currency") if cost is not None else None)
