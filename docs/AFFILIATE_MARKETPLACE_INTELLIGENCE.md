# Affiliate Marketplace Intelligence

`commercial/affiliate_marketplace_intelligence.py` — Seção 30.

Fluxo: PLATAFORMA AUTORIZADA → CATÁLOGO → PRODUTOS → ANÁLISE → RANKING → COMPLIANCE → FIT COM AUDIÊNCIA → SELEÇÃO/RECOMENDAÇÃO.

- `discover_catalog(platform, niche)` usa o adapter registrado (`core/adapters/affiliate/adapters.py`) — sem credencial real, retorna catálogo MOCK claramente identificado.
- `analyze_and_rank()` combina `ProductScoringEngine` + `AffiliateComplianceGate`; um produto só é `recommended=True` se **não** rejeitado pelo score, **aprovado** no compliance, e score ≥ 0.55.
- Produtos sem dado suficiente vão para o fim do ranking (`UNKNOWN` nunca compete com scores reais).
