"""
core.production.scene_engine
==============================
Dynamic Scene Engine (Seção 13). Não aceita vídeos praticamente
estáticos. Frequência visual depende de roteiro/ritmo/nicho/audiência
/formato/retenção — nunca um número fixo universal de segundos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Niche(str, Enum):
    DOCUMENTARY = "documentary"
    NEWS = "news"
    EDUCATIONAL = "educational"
    ENTERTAINMENT = "entertainment"
    STORYTELLING = "storytelling"
    COMMERCIAL = "commercial"
    GENERIC = "generic"


# Faixas de referência (segundos entre trocas de cena), não fixas —
# usadas como ponto de partida e ajustadas por ritmo/retenção (Seção 52).
_NICHE_SCENE_INTERVAL_RANGE_SECONDS: dict[Niche, tuple[float, float]] = {
    Niche.DOCUMENTARY: (6.0, 14.0),
    Niche.NEWS: (4.0, 9.0),
    Niche.EDUCATIONAL: (5.0, 12.0),
    Niche.ENTERTAINMENT: (2.0, 6.0),
    Niche.STORYTELLING: (4.0, 10.0),
    Niche.COMMERCIAL: (2.0, 5.0),
    Niche.GENERIC: (4.0, 10.0),
}


@dataclass
class ScenePlanRequest:
    total_duration_seconds: float
    niche: Niche = Niche.GENERIC
    format_short_form: bool = False  # Shorts/TikTok/Reels tendem a ritmo mais rápido
    retention_curve_hint: str | None = None  # "drop_at_10s" etc — de aprendizado real


@dataclass
class ScenePlan:
    scene_count: int
    avg_interval_seconds: float
    reasoning: list[str] = field(default_factory=list)


class StaticVideoRejected(Exception):
    pass


def plan_scenes(request: ScenePlanRequest) -> ScenePlan:
    low, high = _NICHE_SCENE_INTERVAL_RANGE_SECONDS[request.niche]
    reasoning = [f"faixa base para nicho '{request.niche.value}': {low}-{high}s por cena"]

    if request.format_short_form:
        low, high = low * 0.5, high * 0.6
        reasoning.append("formato curto (Shorts/TikTok/Reels): ritmo acelerado")

    if request.retention_curve_hint:
        # Aprendizado real (Seção 52) pode reduzir o intervalo se houver
        # sinal de queda de retenção específica.
        low, high = low * 0.85, high * 0.85
        reasoning.append(f"ajuste por sinal de retenção: {request.retention_curve_hint}")

    target_interval = (low + high) / 2
    scene_count = max(1, round(request.total_duration_seconds / target_interval))

    plan = ScenePlan(scene_count=scene_count, avg_interval_seconds=target_interval, reasoning=reasoning)

    static_ratio = 1 - min(1.0, (scene_count * 2.0) / max(request.total_duration_seconds, 1))
    if scene_count <= 1 and request.total_duration_seconds > 20:
        raise StaticVideoRejected(
            "plano resultaria em vídeo praticamente estático "
            f"({scene_count} cena para {request.total_duration_seconds}s)"
        )

    return plan
