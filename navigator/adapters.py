"""
navigator.adapters
=====================
NAVIGATOR — adaptadores por plataforma.

Reaproveita `core.adapters.base.BaseAdapter` (já existente) — mesmo
contrato usado pelos adaptadores de afiliados e do Radar: sem
credencial configurada, `mode()` é sempre MOCK, e nenhuma chamada
externa real é feita neste lote. A ORDEM de mecanismo de execução
preferida agora é decidida pelo BRIDGE (`bridge.hub.
EXECUTION_MECHANISM_PRIORITY`/`best_available_execution_path`) — este
módulo só executa (em MOCK) depois que o BRIDGE já confirmou um
caminho viável (ver `navigator/executor.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.adapters.base import AdapterMode, AdapterResult, BaseAdapter
from navigator.models import NavigatorAction


@dataclass
class PlatformExecutionAdapter(BaseAdapter):
    provider_name: str = "generic"

    def is_configured(self) -> bool:
        return False  # nenhuma integração externa real neste lote

    def execute(self, *, action: NavigatorAction, payload: dict) -> AdapterResult:
        mode = self.mode()
        return AdapterResult(
            mode=mode, success=True,
            data={
                "platform": self.provider_name, "action": action.value,
                "note": "MOCK — nenhuma chamada real de API/navegador/RPA foi feita",
            },
        )


# Plataformas cobertas nesta entrega (Seção 1: redes sociais, páginas,
# grupos, perfis, sites, blogs, CMS, marketplaces, afiliados, lojas,
# CRMs, ferramentas comerciais, páginas de venda, landing pages,
# painéis administrativos, plataformas de anúncios, comunidades,
# fóruns, diretórios). Lista extensível sem alterar `NavigatorExecutor`.
DEFAULT_PLATFORMS = (
    "youtube", "instagram", "facebook", "tiktok", "site", "blog", "cms",
    "marketplace", "affiliate_platform", "own_store", "crm", "sales_page",
    "landing_page", "admin_panel", "ads_platform", "community", "forum", "directory",
)


def build_default_platform_adapters() -> dict:
    return {platform: PlatformExecutionAdapter(provider_name=platform) for platform in DEFAULT_PLATFORMS}
