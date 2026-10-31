"""
core.production.sync_validator
================================
Validação de duração e sincronização (Seção 12). Bloqueia narração
cortada, vídeo terminando cedo, áudio fora de sincronia, silêncio
final excessivo, cenas ausentes, duração incoerente.
"""

from __future__ import annotations

from dataclasses import dataclass, field

WORDS_PER_MINUTE_DEFAULT = 150  # velocidade média de narração humanizada pt-BR
MAX_TRAILING_SILENCE_SECONDS = 3.0
MAX_DURATION_DELTA_RATIO = 0.15


@dataclass
class SyncCheckInput:
    word_count: int
    words_per_minute: float
    planned_scene_count: int
    actual_scene_count: int
    video_duration_seconds: float
    audio_duration_seconds: float
    trailing_silence_seconds: float


@dataclass
class SyncCheckResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    estimated_narration_seconds: float = 0.0


def estimate_narration_seconds(word_count: int, words_per_minute: float = WORDS_PER_MINUTE_DEFAULT) -> float:
    if words_per_minute <= 0:
        words_per_minute = WORDS_PER_MINUTE_DEFAULT
    return (word_count / words_per_minute) * 60.0


def validate_sync(check: SyncCheckInput) -> SyncCheckResult:
    issues: list[str] = []

    estimated_seconds = estimate_narration_seconds(check.word_count, check.words_per_minute)

    if check.audio_duration_seconds < estimated_seconds * (1 - MAX_DURATION_DELTA_RATIO):
        issues.append(
            f"narração possivelmente cortada: esperado ~{estimated_seconds:.1f}s, "
            f"áudio tem {check.audio_duration_seconds:.1f}s"
        )

    if check.video_duration_seconds < check.audio_duration_seconds - 0.5:
        issues.append("vídeo termina antes do áudio (vídeo terminando cedo)")

    offset = abs(check.video_duration_seconds - check.audio_duration_seconds)
    if offset > 1.0:
        issues.append(f"áudio fora de sincronia com o vídeo: offset={offset:.2f}s")

    if check.trailing_silence_seconds > MAX_TRAILING_SILENCE_SECONDS:
        issues.append(
            f"silêncio final excessivo: {check.trailing_silence_seconds:.1f}s "
            f"(máximo {MAX_TRAILING_SILENCE_SECONDS}s)"
        )

    if check.actual_scene_count < check.planned_scene_count:
        missing = check.planned_scene_count - check.actual_scene_count
        issues.append(f"cenas ausentes: faltam {missing} de {check.planned_scene_count} planejadas")

    return SyncCheckResult(ok=len(issues) == 0, issues=issues, estimated_narration_seconds=estimated_seconds)
