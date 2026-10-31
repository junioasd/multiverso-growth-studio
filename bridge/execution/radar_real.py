"""
bridge.execution.radar_real
================================
BLOCO FINAL 2 (item 3 — RADAR REAL). Descoberta contínua de
oportunidades REAIS — cada oportunidade registrada precisa ter
passado por uma busca/navegação REAL (via `BrowserExecutor` ou um
provider HTTP real já configurado) e ser validada quanto a: fonte
(domínio real, nunca inventado), URL (formato válido), data (marca
temporal real do momento da descoberta), disponibilidade (a página
respondeu de verdade) e evidência (hash/conteúdo real capturado).

Nunca inventa uma oportunidade: toda tentativa de descoberta que não
conseguir buscar dado real (sem rede, URL inválida, página
indisponível) é registrada como `UNAVAILABLE`, nunca populada com uma
oportunidade fabricada só para "preencher" o radar.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from bridge.execution.browser_executor import BrowserExecutor


class OpportunityValidationStatus(str, Enum):
    VALIDATED = "VALIDATED"          # fonte real, URL válida, página respondeu
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"  # tentativa real, mas a fonte não respondeu
    INVALID_URL = "INVALID_URL"      # nunca chegou a tentar -- formato inválido


@dataclass
class DiscoveredOpportunity:
    opportunity_id: str
    source_url: str
    platform_or_domain: str
    title: str
    discovered_at: float
    validation_status: OpportunityValidationStatus
    evidence_hash: str | None = None
    niche: str | None = None
    notes: str = ""


_URL_PATTERN = re.compile(r"^https?://[^\s]+\.[^\s]+")


class RadarReal:
    """Radar REAL de oportunidades. Cada chamada a `discover()` faz
    uma tentativa GENUÍNA de acesso à fonte (via `BrowserExecutor`) —
    nunca gera uma oportunidade sem essa tentativa real ter ocorrido."""

    def __init__(self, *, browser_executor: BrowserExecutor | None = None) -> None:
        self.browser_executor = browser_executor if browser_executor is not None else BrowserExecutor()
        self._discovered: dict = {}

    def discover(self, *, candidate_url: str, niche: str | None = None) -> DiscoveredOpportunity:
        """Tenta descobrir uma oportunidade REAL a partir de uma URL
        candidata (fornecida pelo chamador — RadarReal nunca inventa
        de onde vem a URL; isso é responsabilidade de quem alimenta o
        radar com fontes legítimas conhecidas: marketplaces, blogs,
        comunidades). Sempre faz uma tentativa REAL de acesso antes de
        validar."""
        opportunity_id = f"opp_{uuid.uuid4().hex[:12]}"
        if not _URL_PATTERN.match(candidate_url or ""):
            return DiscoveredOpportunity(
                opportunity_id=opportunity_id, source_url=candidate_url or "", platform_or_domain="",
                title="", discovered_at=time.time(), validation_status=OpportunityValidationStatus.INVALID_URL,
                niche=niche, notes="URL candidata não tem formato válido — nunca tentada",
            )

        result = self.browser_executor.navigate_and_extract(url=candidate_url)
        domain = self._extract_domain(candidate_url)
        if not result.success:
            opportunity = DiscoveredOpportunity(
                opportunity_id=opportunity_id, source_url=candidate_url, platform_or_domain=domain, title="",
                discovered_at=time.time(), validation_status=OpportunityValidationStatus.SOURCE_UNAVAILABLE,
                niche=niche, notes=f"tentativa real de acesso falhou: {result.reason}",
            )
        else:
            import hashlib

            evidence_hash = hashlib.sha256((result.final_url or candidate_url).encode() + (result.page_title or "").encode()).hexdigest()
            opportunity = DiscoveredOpportunity(
                opportunity_id=opportunity_id, source_url=result.final_url or candidate_url,
                platform_or_domain=domain, title=result.page_title or "", discovered_at=time.time(),
                validation_status=OpportunityValidationStatus.VALIDATED, evidence_hash=evidence_hash, niche=niche,
            )
        self._discovered[opportunity_id] = opportunity
        return opportunity

    def discover_many(self, *, candidate_urls: list, niche: str | None = None) -> list:
        return [self.discover(candidate_url=url, niche=niche) for url in candidate_urls]

    @staticmethod
    def _extract_domain(url: str) -> str:
        match = re.match(r"^https?://([^/]+)", url)
        return match.group(1) if match else ""

    def validated_opportunities(self, *, niche: str | None = None) -> list:
        results = [o for o in self._discovered.values() if o.validation_status == OpportunityValidationStatus.VALIDATED]
        if niche is not None:
            results = [o for o in results if o.niche == niche]
        return results
