"""
youtube_strategy.change_radar
================================
YouTube Change Radar (Seção 25). Interface para acompanhar mudanças
futuras em recomendação, monetização, conteúdo gerado por IA, reused
content, copyright, disclosures, Shorts, Shopping, SEO, discovery,
advertising, políticas.

NÃO implementa scraping proibido — apenas registra entradas fornecidas
por fontes autorizadas (ex: anúncios oficiais, RSS oficial, boletins).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class PolicyArea(str, Enum):
    RECOMMENDATION = "recommendation"
    MONETIZATION = "monetization"
    AI_GENERATED_CONTENT = "ai_generated_content"
    REUSED_CONTENT = "reused_content"
    COPYRIGHT = "copyright"
    DISCLOSURES = "disclosures"
    SHORTS = "shorts"
    SHOPPING = "shopping"
    SEO = "seo"
    DISCOVERY = "discovery"
    ADVERTISING = "advertising"
    OTHER_POLICY = "other_policy"


@dataclass
class PolicyChangeEntry:
    area: PolicyArea
    summary: str
    source_url: str | None
    recorded_at: float = field(default_factory=time.time)


class YouTubeChangeRadar:
    """Registro local de mudanças de política reportadas manualmente
    pelo Owner ou por uma fonte oficial autorizada. Não faz scraping."""

    def __init__(self) -> None:
        self._entries: list[PolicyChangeEntry] = []

    def record_change(self, *, area: PolicyArea, summary: str, source_url: str | None = None) -> PolicyChangeEntry:
        entry = PolicyChangeEntry(area=area, summary=summary, source_url=source_url)
        self._entries.append(entry)
        return entry

    def by_area(self, area: PolicyArea) -> list[PolicyChangeEntry]:
        return [e for e in self._entries if e.area == area]

    def recent(self, limit: int = 10) -> list[PolicyChangeEntry]:
        return sorted(self._entries, key=lambda e: e.recorded_at, reverse=True)[:limit]
