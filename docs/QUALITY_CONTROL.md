# Quality Control

Framework genérico em `core/quality/gates.py` (`Verdict`: APPROVED, REJECTED, REWORK_REQUIRED, REVIEW_REQUIRED, BLOCKED, UNKNOWN).

## Video Quality Gate (`core/quality/video_quality.py`, Seção 21)
Bloqueia: áudio cortado, duração incoerente (>15% de desvio), vídeo estático (>35%), imagem única repetida, cenas irrelevantes, deformação, dessincronia (>250ms), legendas com erro (>5%), pronúncia fraca (<0.75), thumbnail fraca (<0.5), CTA fraco (<0.5), música inadequada (<0.4), erro factual, copyright incerto, violação de política.

## Commercial Quality Gate (`core/quality/commercial_quality.py`, Seção 50)
Antes de escalar produto/oferta: exige amostra mínima (30 cliques / 10 leads) — uma venda isolada nunca é suficiente. Bloqueia refund >15%, margem ≤0, compliance em violação.

## Affiliate Compliance Gate (`commercial/affiliate_compliance.py`, Seção 38)
10 critérios de compliance de afiliados. Campos críticos (regras da plataforma, termos do produtor, claims, disclosure, requisitos legais) não verificados (`None`) já bloqueiam (`BLOCKED`); campos não críticos não verificados geram `REVIEW_REQUIRED`.

Em todos os gates: dado ausente nunca vira aprovação por omissão.
