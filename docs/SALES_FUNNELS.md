# Sales / Funnels

## Funnel Engine (`commercial/funnel_engine.py`, Seções 42-44)
Tipos suportados: direct-to-offer, lead-magnet, email-funnel, content-funnel, VSL, webinar, quiz, tripwire, upsell, cross-sell, community, DM-assisted, organic-remarketing.

Recomendação heurística inicial (confiança sempre `LOW` ou `MEDIUM`, nunca `CERTAIN`):
- audiência fria/baixa confiança → lead-magnet
- ticket ≥ R$500 → webinar
- audiência quente e confiante → direct-to-offer
- caso contrário → content-funnel conservador

Templates por plataforma (`PLATFORM_FUNNEL_TEMPLATES`) são ponto de partida, não regra fixa — Seção 44 exige testar e aprender (`ExperimentEngine`, `LearningLoop`).

## Sales Tracking (`commercial/sales_tracking.py`, Seção 48)
Todo evento com `amount` **exige** `value_type` (`REAL|ESTIMATED|FORECAST`) — tentar registrar valor sem tipo declarado levanta `UnverifiedAmountRejected`. Isso impede, por construção, que qualquer chamador "invente" receita.
