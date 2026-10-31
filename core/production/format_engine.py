"""
core.production.format_engine
================================
Formatos de vídeo por plataforma (Seção 11). YouTube long-form 16:9,
Shorts/TikTok/Reels 9:16. Nunca transformar 16:9 em 9:16 por corte cego
— recompor visualmente quando necessário.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PlatformFormat(str, Enum):
    YOUTUBE_LONG = "youtube_long"
    YOUTUBE_SHORTS = "youtube_shorts"
    TIKTOK = "tiktok"
    INSTAGRAM_REELS = "instagram_reels"


@dataclass(frozen=True)
class AspectSpec:
    aspect_ratio: str
    width: int
    height: int


FORMAT_SPECS: dict[PlatformFormat, AspectSpec] = {
    PlatformFormat.YOUTUBE_LONG: AspectSpec("16:9", 1920, 1080),
    PlatformFormat.YOUTUBE_SHORTS: AspectSpec("9:16", 1080, 1920),
    PlatformFormat.TIKTOK: AspectSpec("9:16", 1080, 1920),
    PlatformFormat.INSTAGRAM_REELS: AspectSpec("9:16", 1080, 1920),
}


class BlindCropRejected(Exception):
    pass


@dataclass
class ReframePlan:
    source_format: PlatformFormat
    target_format: PlatformFormat
    strategy: str  # "native" | "recompose" | "blind_crop"
    notes: str = ""


def plan_reframe(source_format: PlatformFormat, target_format: PlatformFormat, *, allow_blind_crop: bool = False) -> ReframePlan:
    source_spec = FORMAT_SPECS[source_format]
    target_spec = FORMAT_SPECS[target_format]

    if source_spec.aspect_ratio == target_spec.aspect_ratio:
        return ReframePlan(source_format, target_format, strategy="native")

    if allow_blind_crop:
        # Explicitamente desencorajado pela Seção 11; só permitido se o
        # chamador forçar (nunca é o caminho padrão).
        raise BlindCropRejected(
            "corte cego 16:9 -> 9:16 (ou vice-versa) não é permitido por padrão; "
            "use recomposição visual (recompose)"
        )

    return ReframePlan(
        source_format,
        target_format,
        strategy="recompose",
        notes="recomposição visual necessária: reenquadrar elementos-chave, "
        "não apenas cortar bordas",
    )
