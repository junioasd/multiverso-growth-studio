"""
atlas.discovery.sources
===========================
ATLAS PIPELINE 1 — fontes de descoberta de conteúdo.

MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real encontrada por
auditoria independente, 4ª rodada): `discover()` não recebe mais um
`receipt_id` livre para o adapter anexar aos seus sinais — recebe um
`atlas.discovery.provenance.SourceExecutionContext`, que só permite
SOLICITAR uma aquisição verificável através de `context.acquire()`.
Um adapter que nunca chama `context.acquire()` nunca obtém nada
genuíno para referenciar em seus sinais — qualquer `acquisition_id`
que ele "invente" é sanitizado para `UNKNOWN` pelo motor antes de
persistir. Ver `atlas.discovery.provenance` para o mecanismo
completo.

Nenhuma fonte desta entrega tem uma integração de API REALMENTE
implementada — o transporte PADRÃO (`NotImplementedTransport`,
registrado automaticamente pelo `ContentDiscoveryEngine` para toda
fonte sem um transporte explícito) SEMPRE levanta, honestamente.
`GenericMockDiscoverySource` nunca sequer tenta chamar
`context.acquire()` — permanece estruturalmente incapaz de produzir
evidência REAL/FACT, em NENHUMA configuração.

Cada fonte pode devolver indisponibilidade (`SourceUnavailable`) sem
derrubar o scan inteiro — tratado pelo motor
(`atlas.discovery.engine`), nunca aqui.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from atlas.discovery.models import ContentOpportunity, Platform
from atlas.discovery.provenance import SourceExecutionContext
from core.config import SETTINGS

__all__ = ["DiscoverySource", "GenericMockDiscoverySource", "SourceUnavailable", "build_default_sources"]


class SourceUnavailable(Exception):
    """Levantada por `DiscoverySource.discover()` quando a fonte
    genuinamente não pôde responder (indisponibilidade, rate limit,
    erro de rede, OU nenhuma integração real implementada/nenhum
    transporte registrado) — NUNCA para "não ter dado" (isso é uma
    lista vazia, não uma exceção). O motor trata isso registrando a
    fonte como indisponível, sem derrubar as demais."""


class DiscoverySource(ABC):
    """Interface de uma fonte de descoberta de conteúdo. Uma
    integração real (fora desta entrega) implementa `discover()`
    chamando `context.acquire()` — a ÚNICA via de obter um resultado
    verificável — e só então constrói sinais REAL/ESTIMATED/INFERRED
    a partir do resultado genuíno recebido. A mera presença desta
    classe na lista `sources=[...]` do engine NUNCA, por si só,
    concede confiança operacional — isso vem exclusivamente de um
    `AcquisitionTransport` genuinamente registrado pela infraestrutura
    para este `source_name` (ver `atlas.discovery.provenance`/
    `atlas.discovery.engine`)."""

    platform: Platform
    source_name: str

    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    def discover(
        self, *, niche: str | None = None, context: SourceExecutionContext | None = None,
    ) -> list[ContentOpportunity]:
        ...


@dataclass
class GenericMockDiscoverySource(DiscoverySource):
    """Fonte SEM nenhuma integração de API real implementada nesta
    entrega — para QUALQUER plataforma. `is_configured()` só informa
    se uma variável de credencial está presente no ambiente (útil
    para diagnóstico/observabilidade), mas isso NUNCA muda o
    comportamento de `discover()`: esta classe nunca chama
    `context.acquire()` — é estruturalmente incapaz de produzir
    evidência REAL/FACT, em NENHUMA configuração."""

    platform: Platform
    source_name: str
    env_key: str = ""

    def is_configured(self) -> bool:
        return bool(self.env_key) and SETTINGS.provider_key(self.env_key) is not None

    def discover(
        self, *, niche: str | None = None, context: SourceExecutionContext | None = None,
    ) -> list[ContentOpportunity]:
        if self.is_configured():
            # MICROCORREÇÃO CRÍTICA: uma credencial está presente, mas
            # esta classe NUNCA implementou uma chamada de API real —
            # reporta honestamente como indisponível/não implementado,
            # NUNCA fabrica uma oportunidade. A presença da credencial
            # é irrelevante aqui: o que importaria é uma aquisição
            # real ter acontecido através do broker, o que esta classe
            # genérica nunca solicita.
            raise SourceUnavailable(
                f"{self.source_name}: credencial presente, mas nenhuma integração real de API foi "
                f"implementada nesta entrega para esta fonte (NOT_IMPLEMENTED) — presença de credencial "
                f"nunca substitui uma aquisição real verificada; nenhuma oportunidade foi fabricada"
            )
        # Sem credencial — fonte genuinamente MOCK, sem nenhuma
        # oportunidade real a reportar. NUNCA inventa dado.
        return []


def build_default_sources() -> list[DiscoverySource]:
    """Registro padrão de fontes — uma por plataforma explicitamente
    pedida. Adicionar uma plataforma nova no futuro é só ACRESCENTAR
    um item aqui (ou registrar uma `DiscoverySource` DEDICADA, com
    chamada de API real genuína através do broker, quando uma
    integração oficial existir); `atlas.discovery.engine` nunca
    precisa mudar."""
    return [
        GenericMockDiscoverySource(platform=Platform.YOUTUBE, source_name="youtube", env_key="YOUTUBE_API_KEY"),
        GenericMockDiscoverySource(
            platform=Platform.YOUTUBE_SHORTS, source_name="youtube_shorts", env_key="YOUTUBE_API_KEY",
        ),
        GenericMockDiscoverySource(platform=Platform.TIKTOK, source_name="tiktok", env_key="TIKTOK_API_KEY"),
        GenericMockDiscoverySource(platform=Platform.INSTAGRAM, source_name="instagram", env_key="META_API_KEY"),
        GenericMockDiscoverySource(
            platform=Platform.INSTAGRAM_REELS, source_name="instagram_reels", env_key="META_API_KEY",
        ),
    ]
