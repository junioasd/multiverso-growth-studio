# Platform Adapters

`core/adapters/platforms/adapters.py` — Seção 46.

YouTubeAdapter, TikTokAdapter, MetaAdapter, WebsiteAdapter, EmailAdapter, PaymentAdapter, AnalyticsAdapter — todos herdam de `PlatformAdapter`.

- `is_configured()` verifica a env key correspondente (ex: `YOUTUBE_API_KEY`).
- `publish()` sem configuração real → `AdapterResult(mode=MOCK, success=True, ...)` com um `content_id` claramente prefixado `mock://`.
- `publish()` com "configuração" mas sem fluxo OAuth do Owner implementado → `AdapterResult(mode=REAL, success=False, error=...)`. **Nunca** retorna `success=True` sob `mode=REAL` sem uma integração real de fato implementada — isso é proposital nesta fase de fundação (Seção 27/62).
