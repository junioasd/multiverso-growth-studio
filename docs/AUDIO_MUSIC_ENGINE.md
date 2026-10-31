# Audio / Music Engine

`core/adapters/music/engine.py` — Seção 19.

- `MusicRequest` exige `mood` e `scene_context` — música nunca é tratada como "preenchimento de silêncio": `fill_silence_only=True` levanta `MusicPurposeRejected`.
- Sem `MUSIC_PROVIDER_KEY` configurada → modo MOCK, com `license="MOCK_INTERNAL_ONLY"` explícito.
