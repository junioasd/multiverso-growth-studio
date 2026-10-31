"""
core.agents.runtime.providers
==================================
BLOCO FINAL 1 (itens 2/3/4/5) — abstração REAL de provider externo,
usada por todo agente cuja capacidade depende de rede/API paga/modelo
generativo (pesquisa web, geração de imagem/áudio/vídeo). Neste
ambiente/instalação, NENHUM provider real está configurado (sem
acesso à rede) — `NullProvider` é usado por padrão e SEMPRE reporta
`is_configured()=False`, nunca finge sucesso.

Uma organização que integrar este sistema a um provider real (API de
busca, serviço de geração de imagem, etc.) implementa
`ExternalProvider` e injeta a instância no agente — a arquitetura já
está pronta para isso; o que falta é a integração de rede em si, que
está fora do alcance deste ambiente.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from core.evidence.claims import ClaimType


class ExternalProvider(Protocol):
    def is_configured(self) -> bool: ...
    def call(self, *, operation: str, params: dict) -> "ProviderResponse": ...


@dataclass
class ProviderResponse:
    available: bool
    provider_name: str
    data: dict = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""
    fetched_at: float = field(default_factory=time.time)
    # BLOCO FINAL 1 (item 3 — separar dado real, inferência e
    # indisponibilidade): todo provider real precisa classificar
    # explicitamente o que está devolvendo — `FACT` (observação direta,
    # ex: contagem real de resultados de busca), `ESTIMATE` (extrapolação
    # numérica a partir de dado parcial), `INFERENCE` (conclusão
    # qualitativa derivada, nunca uma medição direta), ou `UNKNOWN`
    # quando o próprio provider não sabe classificar. Nunca inventa
    # `FACT` por padrão — omissão vira `UNKNOWN`, nunca `FACT`.
    claim_type: ClaimType = ClaimType.UNKNOWN


class NullProvider:
    """Provider padrão — SEMPRE indisponível. Nunca fabrica um
    resultado; usado sempre que nenhum provider real foi configurado
    (o caso padrão neste ambiente, sem acesso à rede)."""

    name = "NONE"

    def is_configured(self) -> bool:
        return False

    def call(self, *, operation: str, params: dict) -> ProviderResponse:
        return ProviderResponse(
            available=False, provider_name=self.name, confidence=0.0,
            reason=f"nenhum provider real configurado para '{operation}' — instalação sem acesso a rede/API externa",
        )
