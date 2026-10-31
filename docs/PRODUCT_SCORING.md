# Product Scoring Engine

`commercial/product_scoring.py` — Seções 35-37.

Pesos declarados e auditáveis (`WEIGHTS`): audience_fit, product_quality, offer_quality, market_demand, conversion_potential, commission_quality, refund_risk (invertido), reputation.

## Regra de rejeição direta (Seção 37)
Comissão ≥60% **não** salva um produto com:
- qualidade < 0.4, OU
- refund > 20%, OU
- reputação < 0.4, OU
- fit com audiência < 0.3

Nesses casos, `ProductScoreResult.rejected = True` independentemente do score numérico.

## UNKNOWN nunca é inventado
Campos sem dado ficam como `"UNKNOWN"` no componente e são excluídos do cálculo ponderado (a normalização usa apenas o peso dos campos presentes).
