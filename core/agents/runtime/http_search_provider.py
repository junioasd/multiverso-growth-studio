"""
core.agents.runtime.http_search_provider
=============================================
MICROCORREÇÃO — item 2 (WEB/MERCADO: remover NullProvider como
implementação de produção). `HttpSearchProvider` é um adapter REAL —
faz uma requisição HTTP genuína (via `urllib.request`, stdlib, sem
dependência paga) contra um endpoint de busca configurado por
variável de ambiente. Nunca uma implementação permanentemente
"sempre indisponível": `is_configured()` reflete a presença REAL de
credencial (`MGS_SEARCH_API_KEY`) + endpoint
(`MGS_SEARCH_API_URL`) no ambiente — quando ausentes, reporta isso
explicitamente; quando presentes, faz a chamada de rede de verdade.

Neste ambiente/instalação específica, sem credencial configurada
(nenhuma variável de ambiente definida) e sem acesso à rede
disponível para o processo, toda chamada real resulta em
`available=False` com o motivo genuíno (`NO_CREDENTIAL_CONFIGURED` ou
o erro de rede real) — nunca um resultado fabricado. Em uma
instalação com credencial real e rede disponível, este MESMO código
faz a chamada HTTP de verdade e devolve resultado real, com fonte,
timestamp e evidência.
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

_ENV_API_KEY = "MGS_SEARCH_API_KEY"
_ENV_API_URL = "MGS_SEARCH_API_URL"
# MICROCORREÇÃO (suite-order stability): reduzido de 10.0s para 3.0s —
# nunca aumentar timeout para mascarar problema; aqui é o oposto:
# reduzir o pior caso de qualquer chamada de rede real (configurada
# ou não) para nunca comprometer a duração da suíte de testes nem o
# tempo de resposta em produção sob rede instável.
_DEFAULT_TIMEOUT_SECONDS = 3.0


class HttpSearchProvider:
    """Provider REAL de pesquisa/inteligência web via HTTP. Lê
    credencial/endpoint de variáveis de ambiente por padrão (nunca
    hardcoded) — pode receber `api_key`/`api_url` explicitamente para
    testes/integrações específicas, mas o comportamento de produção
    padrão é ler do ambiente."""

    name = "http_search_provider"

    def __init__(self, *, api_key: str | None = None, api_url: str | None = None, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
                 paid: bool | None = None) -> None:
        # v1.0.4 (bloqueador 4): classificação de custo EXPLÍCITA — parâmetro
        # `paid` ou MGS_SEARCH_PROVIDER_PAID=true/false; sem ela, None =>
        # PAYMENT_CLASSIFICATION_REQUIRED em runtime operacional.
        if paid is None:
            env = (os.environ.get("MGS_SEARCH_PROVIDER_PAID") or "").strip().lower()
            paid = True if env == "true" else (False if env == "false" else None)
        self.cost_classification = None if paid is None else ("PAID" if paid else "FREE")
        self.api_key = api_key if api_key is not None else os.environ.get(_ENV_API_KEY)
        self.api_url = api_url if api_url is not None else os.environ.get(_ENV_API_URL)
        self.timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        """CORREÇÃO (item 2): reflete a presença REAL de credencial E
        endpoint — nunca `True` por padrão, nunca `False` permanente
        (assim que uma credencial real for configurada no ambiente,
        passa a `True` automaticamente, sem alterar código)."""
        return bool(self.api_key) and bool(self.api_url)

    def call(self, *, operation: str, params: dict) -> ProviderResponse:
        from core.operations.egress import egress_blocked   # v1.0.3: guarda central de saída

        _blocked = egress_blocked(self.api_url)
        if _blocked:
            return ProviderResponse(available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN, reason=_blocked)
        if not self.is_configured():
            return ProviderResponse(
                available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN,
                reason=(
                    f"NO_CREDENTIAL_CONFIGURED: variáveis de ambiente '{_ENV_API_KEY}'/'{_ENV_API_URL}' não "
                    "definidas nesta instalação — adapter real pronto, aguardando credencial; nunca simula resultado"
                ),
            )

        from core.operations import cost_guard as _cg

        return _cg.run_metered_call(
            provider=self.name, classification=self.cost_classification, operation=operation, params=params,
            perform=lambda: self._perform(operation, params),
            blocked=lambda reason: ProviderResponse(available=False, provider_name=self.name,
                                                    claim_type=ClaimType.UNKNOWN, reason=reason))

    def _perform(self, operation: str, params: dict) -> tuple:
        """I/O real. Devolve (resposta, desfecho EXECUTED|NOT_EXECUTED|UNKNOWN, custo, moeda)."""
        from bridge.execution.platform_adapters import _classify_http_error
        from core.operations.egress import egress_blocked

        _pre = egress_blocked(self.api_url)          # pré-checagem; a ÚLTIMA fica no transporte central
        if _pre:
            return (ProviderResponse(available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN,
                                     reason=_pre), "NOT_EXECUTED", None, None)

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
            return (ProviderResponse(available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN,
                                     reason=_eb.reason), "NOT_EXECUTED", None, None)
        except urllib.error.HTTPError as exc:
            kind = _classify_http_error(exc.code)

            # Preserve o diagnostico REAL devolvido pelo provider/adaptador,
            # mas nunca exp?e segredos em logs/resultado.
            try:
                from core.security.sanitize import sanitize_text

                raw_error = exc.read().decode(
                    "utf-8",
                    errors="replace",
                )

                safe_error = sanitize_text(raw_error)

                # Limite defensivo para n?o carregar corpo arbitrariamente grande.
                safe_error = safe_error[:2000]

            except Exception as detail_exc:
                safe_error = (
                    "<error body unavailable: "
                    f"{type(detail_exc).__name__}>"
                )

            return (
                ProviderResponse(
                    available=False,
                    provider_name=self.name,
                    claim_type=ClaimType.UNKNOWN,
                    fetched_at=fetched_at,
                    reason=(
                        "falha HTTP real ao chamar provider de busca: "
                        f"HTTP {exc.code} ({kind}); "
                        f"detail={safe_error}"
                    ),
                ),
                "UNKNOWN" if kind == "UNKNOWN" else "NOT_EXECUTED",
                None,
                None,
            )
        except urllib.error.URLError as exc:
            # CORREÇÃO (item 2): falha REAL de rede/credencial —
            # motivo genuíno reportado, nunca um resultado fabricado.
            return (ProviderResponse(
                available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN, fetched_at=fetched_at,
                reason=f"falha real de rede ao chamar provider de busca: {getattr(exc, 'reason', exc)}",
            ), "NOT_EXECUTED", None, None)
        except Exception as exc:
            return (ProviderResponse(
                available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN, fetched_at=fetched_at,
                reason=f"falha ao processar resposta real do provider de busca: {exc}",
            ), "UNKNOWN", None, None)

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return (ProviderResponse(
                available=False, provider_name=self.name, claim_type=ClaimType.UNKNOWN, fetched_at=fetched_at,
                reason=f"resposta HTTP {status_code} recebida mas não é JSON válido: {exc}",
            ), "EXECUTED", None, None)

        # CORREÇÃO (item 2 — registrar fonte, timestamp e evidência):
        # a classificação do dado (FACT/ESTIMATE/INFERENCE) vem do
        # PRÓPRIO provider externo, quando ele a declara — nunca
        # assumida como FACT por padrão; sem declaração explícita,
        # fica UNKNOWN (nunca promovida silenciosamente).
        declared_claim_type = parsed.get("claim_type", "UNKNOWN")
        try:
            claim_type = ClaimType(declared_claim_type)
        except ValueError:
            claim_type = ClaimType.UNKNOWN

        _cost = parsed.get("cost")
        _cost = float(_cost) if isinstance(_cost, (int, float)) and not isinstance(_cost, bool) else None
        return (ProviderResponse(
            available=True, provider_name=self.name,
            data={
                "result": parsed.get("result"), "source_url": parsed.get("source_url"), "http_status": status_code,
            },
            confidence=float(parsed.get("confidence", 0.0)), claim_type=claim_type, fetched_at=fetched_at,
            reason="",
        ), "EXECUTED", _cost, (parsed.get("currency") if _cost is not None else None))
