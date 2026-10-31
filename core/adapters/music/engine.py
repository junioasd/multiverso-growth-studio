"""
core.adapters.music.engine
============================
Audio / Music Engine (Seção 19). Música, trilha, efeitos, ambience,
ducking, mixagem, normalização, transições, sincronização. Música deve
combinar com emoção/ritmo/contexto/identidade do canal — nunca usada
apenas para preencher silêncio.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.adapters.base import AdapterMode, AdapterResult, BaseAdapter
from core.config import SETTINGS


@dataclass
class MusicRequest:
    mood: str
    scene_context: str
    channel_identity: str
    duration_seconds: float
    fill_silence_only: bool = False  # se True, é sinal de anti-padrão


class MusicPurposeRejected(Exception):
    pass


class MusicEngine(BaseAdapter):
    provider_name = "music_engine"

    def is_configured(self) -> bool:
        return SETTINGS.provider_key("MUSIC_PROVIDER_KEY") is not None

    def compose_or_select(self, request: MusicRequest) -> AdapterResult:
        if request.fill_silence_only:
            raise MusicPurposeRejected(
                "música não deve ser usada apenas para preencher silêncio "
                "(Seção 19) — informe mood/contexto reais"
            )

        if not self.is_configured():
            return AdapterResult(
                mode=AdapterMode.MOCK,
                success=True,
                data={
                    "provider": "mock_music",
                    "mood": request.mood,
                    "scene_context": request.scene_context,
                    "duration_seconds": request.duration_seconds,
                    "track_ref": f"mock://music/{request.mood}",
                    "license": "MOCK_INTERNAL_ONLY",
                    "note": "Faixa simulada - nenhuma chamada real de API foi feita.",
                },
            )

        return AdapterResult(
            mode=AdapterMode.REAL,
            success=False,
            error="integração real de música ainda não implementada — aguardando fase de piloto pago",
        )
