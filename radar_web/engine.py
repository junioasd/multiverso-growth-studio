"""
radar_web.engine
====================
RADAR WEB COMERCIAL — motor de descoberta (itens 7, 8, 9).

Mesma arquitetura já hardened do `atlas.radar.engine.
CommercialOpportunityRadar`: fontes plugáveis (`StrategySource`),
nenhuma integração externa real neste lote — MOCK explícito, nunca
disfarçado. Item 8 (fontes permitidas): a arquitetura só contempla
fontes PÚBLICAS por design — nenhum mecanismo de bypass de paywall,
autenticação ou scraping proibido existe neste código.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.evidence.claims import ClaimType
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy, compute_strategy_fingerprint


class StrategySource(ABC):
    """Fonte de descoberta — item 8: só fontes PÚBLICAS e permitidas
    (páginas públicas, blogs, documentação, bibliotecas públicas de
    anúncios, relatórios, benchmarks, APIs, marketplaces públicos).
    Uma integração real substitui `discover()`; a interface nunca
    permite acesso a área privada, paywall ou scraping proibido —
    esses mecanismos simplesmente não têm lugar nesta classe."""

    source_name: str = "generic"
    mechanism: FunnelMechanism = FunnelMechanism.OTHER

    @abstractmethod
    def discover(self, *, niche: str | None = None) -> list:
        raise NotImplementedError


@dataclass
class MockStrategySource(StrategySource):
    mechanism: FunnelMechanism
    source_name: str

    def discover(self, *, niche: str | None = None) -> list:
        """MOCK explícito — nenhuma integração externa real neste lote.
        Evidência SEMPRE `UNKNOWN`/`confidence=0.0` — nunca inventa
        nicho, oferta, canal ou sinais de escala.

        CORREÇÃO (item 4 — identidade estável): `strategy_id` é um
        fingerprint ESTRUTURAL (mecanismo+nicho+canal), NUNCA um UUID
        aleatório — a MESMA combinação, descoberta em anos diferentes,
        sempre produz o MESMO `strategy_id`, permitindo versionamento
        longitudinal real."""
        channel = "UNKNOWN"
        strategy_id = compute_strategy_fingerprint(
            mechanism=self.mechanism, niche=niche or "UNKNOWN", channel=channel,
        )
        strategy = FunnelStrategy(
            strategy_id=strategy_id, name=f"{self.mechanism.value} ({self.source_name})",
            niche=niche or "UNKNOWN", offer="UNKNOWN", channel=channel, market="UNKNOWN",
            funnel_stage="UNKNOWN", mechanism=self.mechanism, maturity=FunnelMaturity.UNKNOWN,
            claim_type=ClaimType.UNKNOWN, confidence=0.0, source=self.source_name, observed_at=time.time(),
        )
        return [strategy]


def build_default_strategy_sources() -> list:
    return [
        MockStrategySource(mechanism=mechanism, source_name=f"mock_{mechanism.value.lower()}")
        for mechanism in FunnelMechanism if mechanism != FunnelMechanism.OTHER
    ]


class RadarWebComercial:
    def __init__(self, *, sources: list | None = None) -> None:
        self.sources = sources if sources is not None else build_default_strategy_sources()
        self._tracked: list = []

    def discover(self, *, niche: str | None = None, mechanisms: set | None = None) -> list:
        results = []
        for source in self.sources:
            if mechanisms is not None and source.mechanism not in mechanisms:
                continue
            results.extend(source.discover(niche=niche))
        self._tracked.extend(results)
        return results

    def tracked_strategies(self) -> list:
        return list(self._tracked)

    @staticmethod
    def classify_maturity(strategy: FunnelStrategy) -> FunnelStrategy:
        """Item 9: nunca classifica HIGH_SCALE/MATURE só por opinião —
        `FunnelStrategy.with_maturity_enforced()` já garante o
        rebaixamento estrutural para UNKNOWN sem evidência FACT e
        confiança suficiente."""
        return strategy.with_maturity_enforced()
