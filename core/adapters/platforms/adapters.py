"""
core.adapters.platforms.adapters
==================================
Platform Adapters (Seção 46). Quando não conectado, usa mock. Nunca
afirma publicação real se apenas mock foi executado (verificar sempre
`AdapterResult.mode`).

Seção 27: nunca contornar OAuth, consentimento, 2FA, CAPTCHA,
verification, eligibility, API limits, políticas.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.adapters.base import AdapterMode, AdapterResult, BaseAdapter
from core.config import SETTINGS


@dataclass
class PublicationRequest:
    channel_id: str
    title: str
    description: str
    video_ref: str
    thumbnail_ref: str | None = None
    tags: list[str] = field(default_factory=list)
    visibility: str = "PRIVATE"  # nunca default público
    schedule_at: float | None = None
    category: str | None = None


class PlatformAdapter(BaseAdapter):
    """Base para adapters de plataforma de publicação/distribuição."""

    env_key: str = ""

    def is_configured(self) -> bool:
        return SETTINGS.provider_key(self.env_key) is not None if self.env_key else False

    def publish(self, request: PublicationRequest) -> AdapterResult:
        if not self.is_configured():
            return AdapterResult(
                mode=AdapterMode.MOCK,
                success=True,
                data={
                    "provider": self.provider_name,
                    "channel_id": request.channel_id,
                    "title": request.title,
                    "visibility": request.visibility,
                    "content_id": f"mock://{self.provider_name}/{request.channel_id}/{abs(hash(request.title))}",
                    "note": "Publicação simulada - nenhuma chamada real de API/OAuth foi feita.",
                },
            )
        # Integração real requer OAuth explícito do Owner (Seção 27/32) —
        # não implementada nesta fundação por design.
        return AdapterResult(
            mode=AdapterMode.REAL,
            success=False,
            error=f"publicação real via {self.provider_name} requer fluxo OAuth do Owner concluído",
        )


class YouTubeAdapter(PlatformAdapter):
    provider_name = "youtube"
    env_key = "YOUTUBE_API_KEY"


class TikTokAdapter(PlatformAdapter):
    provider_name = "tiktok"
    env_key = "TIKTOK_API_KEY"


class MetaAdapter(PlatformAdapter):
    """Instagram / Facebook."""
    provider_name = "meta"
    env_key = "META_API_KEY"


class WebsiteAdapter(PlatformAdapter):
    provider_name = "website"
    env_key = "WEBSITE_API_KEY"


class EmailAdapter(PlatformAdapter):
    provider_name = "email"
    env_key = "EMAIL_PROVIDER_KEY"

    def send(self, to: str, subject: str, body: str) -> AdapterResult:
        if not self.is_configured():
            return AdapterResult(
                mode=AdapterMode.MOCK,
                success=True,
                data={"provider": "email", "to": to, "subject": subject, "note": "Envio simulado."},
            )
        return AdapterResult(mode=AdapterMode.REAL, success=False, error="provider de email não implementado")


class PaymentAdapter(PlatformAdapter):
    provider_name = "payment"
    env_key = "PAYMENT_PROVIDER_KEY"


class AnalyticsAdapter(PlatformAdapter):
    provider_name = "analytics"
    env_key = "ANALYTICS_PROVIDER_KEY"

    def fetch_metrics(self, channel_id: str) -> AdapterResult:
        if not self.is_configured():
            return AdapterResult(
                mode=AdapterMode.MOCK,
                success=True,
                data={"provider": "analytics", "channel_id": channel_id, "metrics": "UNKNOWN (mock)"},
            )
        return AdapterResult(mode=AdapterMode.REAL, success=False, error="provider de analytics não implementado")
