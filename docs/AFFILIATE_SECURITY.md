# Affiliate Security

Regras aplicadas (Seções 32-39):

- **Senha nunca chega ao sistema**: fluxo é sempre OAuth/API oficial/token de escopo mínimo (Seção 32/33). Nenhum campo de senha de plataforma de afiliado existe no domínio.
- **Sem scraping proibido** (Seção 34): `AffiliateMarketplaceAdapter.fetch_catalog()` só usa API/feed oficial (`is_configured()`) ou cai em MOCK — nunca simula acesso não autorizado.
- **Link management** (Seção 39): (a implementar na fase de piloto) associação `platform + product_id + offer_id + campaign + channel + content + funnel` sem expor credenciais; nunca usado para spam ou cloaking.
- **Compliance Gate** (Seção 38): ver `docs/QUALITY_CONTROL.md`.
