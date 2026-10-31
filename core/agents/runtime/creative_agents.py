"""
core.agents.runtime.creative_agents
========================================
BLOCO FINAL 1 (item 2 — INTELIGÊNCIA MULTIMODAL: imagens/vídeo/áudio)
— AURORA, PIXEL, FRAME, VOX, SONORA: os 5 agentes cuja função oficial
depende de geração/edição real de conteúdo visual/sonoro.

Neste ambiente, sem modelo generativo real conectado, nenhum provider
está configurado — cada agente SEMPRE reporta `UNAVAILABLE` de forma
explícita, nunca fabrica uma "imagem gerada" ou "narração produzida"
que não existe. A arquitetura está pronta para receber um provider
real (ex: um serviço de geração de imagem/áudio/vídeo) — ver
`core.agents.runtime.providers.ExternalProvider`.
"""

from __future__ import annotations

from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.agents.runtime.http_generation_provider import HttpGenerationProvider
from core.capabilities.registry import Capability
from core.agents.runtime.web_research_agents import ProviderDependentAgent


class CreativeProviderAgent(ProviderDependentAgent):
    """CORREÇÃO (item 2 — remover NullProvider como implementação de
    produção): base comum para os 5 agentes criativos — provider
    PADRÃO é `HttpGenerationProvider()`, um adapter HTTP REAL para
    geração de conteúdo (nunca mais `NullProvider`/stub permanente)."""

    _default_provider_factory = HttpGenerationProvider


class AuroraAgent(CreativeProviderAgent):
    agent_id = "AURORA"
    primary_task_types = frozenset({"DEFINE_CREATIVE_DIRECTION", "REVIEW_VISUAL_CONCEPT"})
    all_rounder_task_types = frozenset({"REVIEW_VISUAL_CONCEPT"})
    provider_operation = "CREATIVE_DIRECTION"
    max_input_items = 50

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if not self.can_handle(task_type=task_type):
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")
        return self._call_provider(task_type=task_type, params=payload)


class PixelAgent(CreativeProviderAgent):
    agent_id = "PIXEL"
    primary_task_types = frozenset({"GENERATE_THUMBNAIL", "GENERATE_IMAGE"})
    all_rounder_task_types = frozenset({"GENERATE_IMAGE"})
    provider_operation = "IMAGE_GENERATION"
    max_input_items = 50

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if not self.can_handle(task_type=task_type):
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")
        return self._call_provider(task_type=task_type, params=payload)


class FrameAgent(CreativeProviderAgent):
    agent_id = "FRAME"
    primary_task_types = frozenset({"PRODUCE_VIDEO", "EDIT_VIDEO"})
    all_rounder_task_types = frozenset({"EDIT_VIDEO"})
    provider_operation = "VIDEO_PRODUCTION"
    max_input_items = 20

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if not self.can_handle(task_type=task_type):
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")
        return self._call_provider(task_type=task_type, params=payload)


class VoxAgent(CreativeProviderAgent):
    agent_id = "VOX"
    primary_task_types = frozenset({"GENERATE_NARRATION"})
    all_rounder_task_types = frozenset()
    provider_operation = "VOICE_NARRATION"
    max_input_items = 20

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if not self.can_handle(task_type=task_type):
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")
        return self._call_provider(task_type=task_type, params=payload)


class SonoraAgent(CreativeProviderAgent):
    agent_id = "SONORA"
    primary_task_types = frozenset({"GENERATE_MUSIC", "MIX_AUDIO"})
    all_rounder_task_types = frozenset({"MIX_AUDIO"})
    provider_operation = "AUDIO_PRODUCTION"
    max_input_items = 20

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if not self.can_handle(task_type=task_type):
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")
        return self._call_provider(task_type=task_type, params=payload)


def register_creative_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    mapping = (
        ("AURORA", Capability.MULTIMODAL_IMAGE, CompetencyLevel.MASTER),
        ("PIXEL", Capability.MULTIMODAL_IMAGE, CompetencyLevel.ESPECIALISTA),
        ("FRAME", Capability.MULTIMODAL_VIDEO, CompetencyLevel.ESPECIALISTA),
        ("VOX", Capability.MULTIMODAL_AUDIO, CompetencyLevel.ESPECIALISTA),
        ("SONORA", Capability.MULTIMODAL_AUDIO, CompetencyLevel.ESPECIALISTA),
    )
    for agent_id, capability, level in mapping:
        capability_registry.register(
            agent=agent_id, capability=capability, level=level, granted_by=granted_by,
            provider="NONE", available=False, authorized=True, health=CapabilityHealth.UNKNOWN,
        )
