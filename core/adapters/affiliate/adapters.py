"""
core.adapters.affiliate.adapters
===================================
Adapters de plataformas de afiliados (Seção 31). Nomes como Hotmart,
Monetizze, Eduzz, Kiwify, Braip são exemplos de adapters — sem
dependência rígida. Cada plataforma pode ser adicionada, desativada ou
substituída sem reconstruir o sistema.

Seção 34: sem scraping proibido. Usa apenas API/catálogo/feed/export
oficial quando disponível; senão, dados fornecidos legitimamente pelo
Owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.adapters.base import AdapterMode, AdapterResult, BaseAdapter
from core.config import SETTINGS


@dataclass
class CatalogProduct:
    external_id: str
    name: str
    niche: str
    price: float
    commission_pct: float
    reputation_score: float | None = None  # None = UNKNOWN
    refund_rate: float | None = None        # None = UNKNOWN
    metadata: dict = field(default_factory=dict)


class AffiliateMarketplaceAdapter(BaseAdapter):
    """Base para adapters de marketplace de afiliados."""

    env_key: str = ""

    def is_configured(self) -> bool:
        return SETTINGS.provider_key(self.env_key) is not None if self.env_key else False

    def fetch_catalog(self, niche: str | None = None) -> AdapterResult:
        if not self.is_configured():
            mock_catalog = self._mock_catalog(niche)
            return AdapterResult(
                mode=AdapterMode.MOCK,
                success=True,
                data={
                    "provider": self.provider_name,
                    "products": [p.__dict__ for p in mock_catalog],
                    "note": "Catálogo simulado - nenhuma chamada real de API foi feita.",
                },
            )
        return AdapterResult(
            mode=AdapterMode.REAL,
            success=False,
            error=f"integração real com {self.provider_name} requer credencial oficial configurada",
        )

    def _mock_catalog(self, niche: str | None) -> list[CatalogProduct]:
        base_niche = niche or "geral"
        return [
            CatalogProduct(
                external_id=f"{self.provider_name}_mock_1",
                name=f"Produto exemplo A ({base_niche})",
                niche=base_niche,
                price=197.0,
                commission_pct=0.5,
                reputation_score=0.7,
                refund_rate=0.08,
            ),
            CatalogProduct(
                external_id=f"{self.provider_name}_mock_2",
                name=f"Produto exemplo B ({base_niche})",
                niche=base_niche,
                price=97.0,
                commission_pct=0.7,
                reputation_score=0.4,
                refund_rate=0.22,
            ),
        ]


class HotmartAdapter(AffiliateMarketplaceAdapter):
    provider_name = "hotmart"
    env_key = "HOTMART_CLIENT_ID"


class MonetizzeAdapter(AffiliateMarketplaceAdapter):
    provider_name = "monetizze"
    env_key = "MONETIZZE_API_KEY"


class EduzzAdapter(AffiliateMarketplaceAdapter):
    provider_name = "eduzz"
    env_key = "EDUZZ_API_KEY"


class KiwifyAdapter(AffiliateMarketplaceAdapter):
    provider_name = "kiwify"
    env_key = "KIWIFY_API_KEY"


class BraipAdapter(AffiliateMarketplaceAdapter):
    provider_name = "braip"
    env_key = "BRAIP_API_KEY"


REGISTRY: dict[str, type[AffiliateMarketplaceAdapter]] = {
    "hotmart": HotmartAdapter,
    "monetizze": MonetizzeAdapter,
    "eduzz": EduzzAdapter,
    "kiwify": KiwifyAdapter,
    "braip": BraipAdapter,
}


def get_adapter(platform: str) -> AffiliateMarketplaceAdapter:
    cls = REGISTRY.get(platform.lower())
    if not cls:
        raise ValueError(f"plataforma de afiliado não registrada: {platform}")
    return cls()
