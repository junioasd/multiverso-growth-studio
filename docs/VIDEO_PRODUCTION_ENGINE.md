# Video Production Engine

Implementa o pipeline da Seção 8 do Comando Mestre em `core/production/pipeline.py`:

IDEIA → PESQUISA → FACT CHECK → PLANEJAMENTO → ROTEIRO → STORYBOARD →
NARRAÇÃO → CENAS → VISUAIS → 2D/3D/VÍDEO IA → MÚSICA → EFEITOS →
EDIÇÃO → LEGENDAS → THUMBNAIL → TÍTULO → DESCRIÇÃO → SEO → CTA →
QUALITY GATE → PREVIEW → (aguarda autorização do Owner).

## Módulos

- `script_engine.py` — avalia originalidade, filler genérico, CTA, hook; fact-check com status `OK|UNKNOWN|REVIEW_REQUIRED|FAILED`.
- `narration.py` — separa `NARRATION_TEXT` de `SCENE_INSTRUCTIONS`/`VISUAL_PROMPT`/`EDITING_INSTRUCTIONS`/`MUSIC_INSTRUCTIONS`; `assert_clean_for_voice()` bloqueia qualquer marcação técnica residual antes de ir para a Voice Engine.
- `scene_engine.py` — Dynamic Scene Engine: intervalo entre cenas varia por nicho/formato/retenção, nunca um número fixo universal.
- `format_engine.py` — 16:9 (YouTube longo) vs 9:16 (Shorts/TikTok/Reels); corte cego é bloqueado por padrão (`BlindCropRejected`), exige recomposição visual.
- `sync_validator.py` — detecta narração cortada, vídeo terminando cedo, dessincronia, silêncio final excessivo, cenas ausentes.
- `copyright_gate.py` — `UNKNOWN`/`AMBIGUOUS`/`INCOMPATIBLE` sempre bloqueiam.
- `discovery_engines.py` — Title/Thumbnail/SEO Engines, com detecção de padrões de clickbait enganoso.

## Regra de publicação

`ProductionPipeline.finalize_preview()` nunca retorna um estágio de publicação real — o último estágio possível é `AWAITING_OWNER_AUTHORIZATION`. Publicar de fato é responsabilidade de um adapter de plataforma (`core/adapters/platforms`) chamado explicitamente após autorização do Owner.
