"""
core.adapters.voice.engine
============================
Voice Engine (Seções 17-18). Vozes IA humanizadas com perfis
configuráveis. Vozes de pessoas famosas SOMENTE com autorização
explícita ou licença comercial válida — nunca por padrão.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.adapters.base import AdapterMode, AdapterResult, BaseAdapter
from core.config import SETTINGS


class VoiceProfile(str, Enum):
    DOCUMENTAL = "DOCUMENTAL"
    PREMIUM = "PREMIUM"
    ENERGETIC = "ENERGETIC"
    CALM = "CALM"
    EDUCATIONAL = "EDUCATIONAL"
    DRAMATIC = "DRAMATIC"
    MYSTERY = "MYSTERY"
    AUTHORITATIVE = "AUTHORITATIVE"
    FRIENDLY = "FRIENDLY"
    STORYTELLER = "STORYTELLER"
    COMMERCIAL = "COMMERCIAL"
    NEWS_STYLE = "NEWS_STYLE"
    YOUNG = "YOUNG"
    MATURE = "MATURE"
    MALE = "MALE"
    FEMALE = "FEMALE"
    NEUTRAL = "NEUTRAL"


class CelebrityVoiceDenied(Exception):
    pass


@dataclass
class VoiceRequest:
    text: str
    profile: VoiceProfile = VoiceProfile.DOCUMENTAL
    language: str = "pt-BR"
    is_celebrity_voice: bool = False
    celebrity_authorization_ref: str | None = None  # referência a licença/consentimento


@dataclass
class VoiceQualityAssessment:
    prosody_score: float
    emotion_score: float
    diction_score: float
    naturalness_score: float
    consistency_score: float

    def overall(self) -> float:
        return (
            self.prosody_score
            + self.emotion_score
            + self.diction_score
            + self.naturalness_score
            + self.consistency_score
        ) / 5


class VoiceEngine(BaseAdapter):
    provider_name = "voice_engine"

    def is_configured(self) -> bool:
        return SETTINGS.provider_key("VOICE_PROVIDER_KEY") is not None

    def synthesize(self, request: VoiceRequest) -> AdapterResult:
        # Guarda de segurança: voz de pessoa famosa exige autorização/licença
        # explícita. Sem isso, a requisição é recusada — nunca clonagem por
        # padrão (Seção 18).
        if request.is_celebrity_voice and not request.celebrity_authorization_ref:
            raise CelebrityVoiceDenied(
                "síntese de voz de pessoa famosa requer autorização explícita "
                "ou licença comercial válida (celebrity_authorization_ref ausente)"
            )

        if not self.is_configured():
            return AdapterResult(
                mode=AdapterMode.MOCK,
                success=True,
                data={
                    "provider": "mock_voice",
                    "profile": request.profile.value,
                    "language": request.language,
                    "audio_ref": f"mock://voice/{request.profile.value}",
                    "assessment": VoiceQualityAssessment(0.8, 0.75, 0.8, 0.78, 0.82).__dict__,
                    "note": "Áudio simulado - nenhuma chamada real de API foi feita.",
                },
            )

        return AdapterResult(
            mode=AdapterMode.REAL,
            success=False,
            error="integração real de voz ainda não implementada — aguardando fase de piloto pago",
        )
