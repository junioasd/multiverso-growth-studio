"""
core.adapters.video.router
============================
Video Model Router (Seção 14). Independente de fornecedor. Escolhe o
modelo POR CENA considerando qualidade, realismo, movimento,
consistência, personagem, física, estilo, 3D, duração, resolução,
áudio, custo, latência, disponibilidade, licença, restrições, tipo de
cena.

Se provider real não estiver configurado, usa mock. Nunca inventa
execução real.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.adapters.base import AdapterMode, AdapterResult, BaseAdapter
from core.config import SETTINGS


@dataclass
class SceneRequirements:
    scene_type: str               # e.g. "cinematic", "infographic", "character_dialogue"
    needs_3d: bool = False
    needs_character_consistency: bool = False
    max_cost_usd: float | None = None
    max_latency_seconds: float | None = None
    target_resolution: str = "1080p"
    duration_seconds: float = 5.0


@dataclass
class VideoProviderProfile:
    name: str
    env_key: str
    supports_3d: bool
    supports_character_consistency: bool
    quality_score: float     # 0-1, heurística declarada, não medida em produção real
    relative_cost: float     # custo relativo (1.0 = baseline)
    typical_latency_seconds: float


# Perfis declarativos dos providers conhecidos (Seção 14: Seedance, Kling,
# Veo, Sora e outros). Valores são estimativas de referência, não reais.
KNOWN_PROVIDERS: list[VideoProviderProfile] = [
    VideoProviderProfile("seedance", "VIDEO_PROVIDER_SEEDANCE_KEY", True, True, 0.80, 1.0, 60),
    VideoProviderProfile("kling", "VIDEO_PROVIDER_KLING_KEY", True, True, 0.85, 1.3, 90),
    VideoProviderProfile("veo", "VIDEO_PROVIDER_VEO_KEY", True, False, 0.88, 1.6, 120),
    VideoProviderProfile("sora", "VIDEO_PROVIDER_SORA_KEY", True, True, 0.90, 2.0, 150),
]


class MockVideoProvider(BaseAdapter):
    provider_name = "mock_video_provider"

    def is_configured(self) -> bool:
        return False  # mock nunca é "configurado" como real

    def generate_scene(self, requirements: SceneRequirements) -> AdapterResult:
        return AdapterResult(
            mode=AdapterMode.MOCK,
            success=True,
            data={
                "provider": "mock",
                "scene_type": requirements.scene_type,
                "duration_seconds": requirements.duration_seconds,
                "asset_ref": f"mock://video/{requirements.scene_type}-{id(requirements)}",
                "note": "Ativo simulado - nenhuma chamada real de API foi feita.",
            },
        )


class RealVideoProviderStub(BaseAdapter):
    """Stub para um provider real. Só executa se a env key estiver presente.
    Sem integração real implementada aqui de propósito (Seção 62: Cost
    Control — evitar chamadas pagas desnecessárias durante desenvolvimento).
    """

    def __init__(self, profile: VideoProviderProfile):
        self.profile = profile
        self.provider_name = profile.name

    def is_configured(self) -> bool:
        return SETTINGS.provider_key(self.profile.env_key) is not None

    def generate_scene(self, requirements: SceneRequirements) -> AdapterResult:
        if not self.is_configured():
            return AdapterResult(
                mode=AdapterMode.MOCK,
                success=False,
                error=f"provider '{self.profile.name}' não configurado (sem API key)",
            )
        # Integração real intencionalmente não implementada nesta etapa
        # (Seção 62: sem credenciais/API paga nesta fase de fundação).
        return AdapterResult(
            mode=AdapterMode.REAL,
            success=False,
            error="integração real ainda não implementada — aguardando fase de piloto pago",
        )


@dataclass
class RoutingDecision:
    chosen_provider: str
    mode: AdapterMode
    reasons: list[str] = field(default_factory=list)


class VideoModelRouter:
    def __init__(self):
        self._providers: dict[str, RealVideoProviderStub] = {
            p.name: RealVideoProviderStub(p) for p in KNOWN_PROVIDERS
        }
        self._profiles: dict[str, VideoProviderProfile] = {p.name: p for p in KNOWN_PROVIDERS}
        self._mock = MockVideoProvider()

    def choose_provider(self, requirements: SceneRequirements) -> RoutingDecision:
        reasons: list[str] = []
        candidates = []
        for name, profile in self._profiles.items():
            if requirements.needs_3d and not profile.supports_3d:
                continue
            if requirements.needs_character_consistency and not profile.supports_character_consistency:
                continue
            if requirements.max_cost_usd is not None and profile.relative_cost > requirements.max_cost_usd:
                continue
            if (
                requirements.max_latency_seconds is not None
                and profile.typical_latency_seconds > requirements.max_latency_seconds
            ):
                continue
            candidates.append(profile)

        if not candidates:
            reasons.append("nenhum provider real atende aos requisitos; usando mock")
            return RoutingDecision(chosen_provider="mock", mode=AdapterMode.MOCK, reasons=reasons)

        # Escolhe o de maior qualidade dentro do custo aceitável
        candidates.sort(key=lambda p: (-p.quality_score, p.relative_cost))
        best = candidates[0]

        adapter = self._providers[best.name]
        if adapter.is_configured():
            reasons.append(f"provider real '{best.name}' selecionado por melhor qualidade/custo")
            return RoutingDecision(chosen_provider=best.name, mode=AdapterMode.REAL, reasons=reasons)

        reasons.append(f"provider ideal '{best.name}' não configurado; caindo para mock")
        return RoutingDecision(chosen_provider="mock", mode=AdapterMode.MOCK, reasons=reasons)

    def generate_scene(self, requirements: SceneRequirements) -> AdapterResult:
        decision = self.choose_provider(requirements)
        if decision.mode == AdapterMode.MOCK:
            return self._mock.generate_scene(requirements)
        return self._providers[decision.chosen_provider].generate_scene(requirements)
