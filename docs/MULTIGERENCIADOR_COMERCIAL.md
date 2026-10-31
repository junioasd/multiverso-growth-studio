# MultiGerenciador Comercial

`commercial/multigerenciador.py` — Seção 29.

Fluxo: MERCADO → AUDIÊNCIA → OPORTUNIDADE → PRODUTO → OFERTA → CANAL → FUNIL → CONTEÚDO → DISTRIBUIÇÃO → LEAD → CONVERSÃO → RECEITA → MARGEM → APRENDIZADO → OTIMIZAÇÃO.

`MultiGerenciadorComercial` mantém instâncias coesas de:
- `AffiliateMarketplaceIntelligence` (descoberta/ranking de produtos)
- `FunnelEngine` (recomendação de funil)
- `CRM`, `SalesTracker`, `FinanceLedger`
- `LearningLoop`
- `CommercialQualityGate` (readiness para escalar)

`assess_opportunity()` executa mercado→produto→funil numa chamada. `evaluate_scaling_readiness()` combina vendas + finanças + quality gate antes de qualquer decisão de escala — nunca aprova com dado insuficiente.
