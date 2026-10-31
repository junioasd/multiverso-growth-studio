# Voice Engine

`core/adapters/voice/engine.py` — Seções 17-18.

- 17 perfis configuráveis (`VoiceProfile`): DOCUMENTAL, PREMIUM, ENERGETIC, CALM, EDUCATIONAL, DRAMATIC, MYSTERY, AUTHORITATIVE, FRIENDLY, STORYTELLER, COMMERCIAL, NEWS_STYLE, YOUNG, MATURE, MALE, FEMALE, NEUTRAL.
- Avaliação de qualidade declarada em `VoiceQualityAssessment` (prosódia, emoção, dicção, naturalidade, consistência).
- **Guarda de segurança obrigatória**: `synthesize()` levanta `CelebrityVoiceDenied` sempre que `is_celebrity_voice=True` sem `celebrity_authorization_ref` preenchido. Não há bypass.
- Sem `VOICE_PROVIDER_KEY` configurada → modo MOCK.
