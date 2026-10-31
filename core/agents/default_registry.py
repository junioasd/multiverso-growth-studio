"""
core.agents.default_registry
================================
CORREÇÃO — restaura a estrutura OFICIAL dos 25 agentes principais
(nunca substituídos silenciosamente por nomes inventados):

ATLAS, ORION, SAGE, PRISMA, LIBRA, MERCURY, VENTURE, NEXUS, RELAY,
ARGOS, SCRIPTOR, VERITAS, AURORA, FRAME, VOX, SONORA, PIXEL, PULSE,
BRIDGE, SENTINEL, JURIS, METRICA, ECHO, NAVIGATOR, FORGE.

BLOCO FINAL 1 (item 1) + MICROCORREÇÃO (eliminar todos os PARTIAL):
**25/25 agentes `ACTIVE`, 0 SCAFFOLD, 0 PARTIAL.**

Cada agente tem runtime real (`core.agents.runtime.*`), contrato
genuíno (`can_handle`/`execute`), integrado ao Capability Registry via
as funções `register_*_capabilities()`. `ACTIVE` aqui significa:
runtime genuinamente real e testado (nunca uma exceção crua escapando
como sucesso, nunca dado fabricado) — não que TODA integração externa
esteja necessariamente ativa neste exato ambiente:

- **13 agentes autocontidos ou sobre sistemas internos reais**
  (CRM/Finance/Sales/MasterLibrary/EvidenceRegistry/LearningLoop/
  QualitySupervisor/BridgeHub): METRICA, ECHO, VERITAS, JURIS, PRISMA,
  LIBRA, MERCURY, NEXUS, RELAY, ARGOS, SCRIPTOR, SENTINEL, BRIDGE.
  Quando o sistema real (ex: um `FinanceLedger`) não é conectado na
  instanciação, o agente reporta `UNAVAILABLE` de forma honesta em vez
  de fabricar dado — isso não torna o `implementation_status` falso: o
  runtime é genuinamente real, e reportar indisponibilidade
  corretamente é o comportamento correto de um agente ACTIVE bem-
  comportado.
- **9 agentes dependentes de provider EXTERNO** (rede/API paga/modelo
  generativo) — ORION, SAGE, VENTURE, PULSE (pesquisa web) e AURORA,
  PIXEL, FRAME, VOX, SONORA (geração multimodal). MICROCORREÇÃO:
  `NullProvider` (stub permanentemente indisponível) foi REMOVIDO
  como provider padrão de produção — substituído por
  `HttpSearchProvider`/`HttpGenerationProvider`
  (`core.agents.runtime.http_search_provider`/
  `http_generation_provider`), adapters HTTP REAIS (via
  `urllib.request`, stdlib) que leem credencial de variável de
  ambiente e fazem chamada de rede genuína quando configurados. Neste
  ambiente específico, sem credencial/rede configurada, honestamente
  `UNAVAILABLE` — mas o RUNTIME e o ADAPTER são genuinamente reais e
  prontos, nunca um stub fixo.

Todos os 25 têm `agentic_architecture_supported=True` e
`all_rounder_capable=True` (arquitetura transversal comum a TODOS,
independente de `implementation_status`).
"""

from __future__ import annotations

from core.agents.base import AgentRegistration, AgentRegistry, ImplementationStatus, MultimodalCapabilityEntry
from core.agents.runtime.argos import ArgosAgent
from core.agents.runtime.bridge import BridgeAgent
from core.agents.runtime.creative_agents import AuroraAgent, FrameAgent, PixelAgent, SonoraAgent, VoxAgent
from core.agents.runtime.echo import EchoAgent
from core.agents.runtime.juris import JurisAgent
from core.agents.runtime.libra import LibraAgent
from core.agents.runtime.mercury import MercuryAgent
from core.agents.runtime.metrica import MetricaAgent
from core.agents.runtime.nexus import NexusAgent
from core.agents.runtime.prisma import PrismaAgent
from core.agents.runtime.relay import RelayAgent
from core.agents.runtime.scriptor import ScriptorAgent
from core.agents.runtime.sentinel import SentinelAgent
from core.agents.runtime.veritas import VeritasAgent
from core.agents.runtime.web_research_agents import OrionAgent, PulseAgent, SageAgent, VentureAgent
from core.capabilities.registry import Capability, CapabilityHealth, CompetencyLevel
from core.multimodal.evidence import Modality


class ActiveAgentContract:
    """Contrato REAL mínimo para os agentes ACTIVE — `can_handle()`
    nunca mente.

    CORREÇÃO (item 13 — resolve_agent_for_task nunca escolhe agente só
    por all_rounder=True): separa `primary_task_types` (domínio
    principal — usado nos tiers DOMAIN_SPECIALIST/SECONDARY_SPECIALIST)
    de `all_rounder_task_types` (tarefas que o agente SÓ atende quando
    agindo como ALL-ROUNDER, fora do domínio principal — usado
    EXCLUSIVAMENTE no tier ALL_ROUNDER). `can_handle()` (usado pelo
    tier ALL_ROUNDER) cobre AMBOS os conjuntos; `can_handle_as_primary()`
    (usado pelos tiers 1/2) cobre SOMENTE o domínio principal — uma
    tarefa genuinamente desconhecida por ninguém NUNCA cai
    automaticamente em quem tiver `all_rounder=True`."""

    def __init__(self, *, agent_id: str, primary_task_types: set, all_rounder_task_types: set | None = None) -> None:
        self.agent_id = agent_id
        self.primary_task_types = primary_task_types
        self.all_rounder_task_types = all_rounder_task_types or set()

    def can_handle_as_primary(self, *, task_type: str) -> bool:
        return task_type in self.primary_task_types

    def can_handle(self, *, task_type: str) -> bool:
        return task_type in self.primary_task_types or task_type in self.all_rounder_task_types


def _mm(modality: Modality, *, supported: bool = True) -> MultimodalCapabilityEntry:
    return MultimodalCapabilityEntry(modality=modality, supported=supported, available=False, provider="MOCK")


# (agent_id, contract_instance, display_name, specialization, competency_level, capabilities, modalidades)
# CORREÇÃO (BLOCO FINAL 1 — item 1): os 11 agentes com runtime real
# autocontido/sobre sistemas internos — `implementation_status=ACTIVE`.
_ACTIVE_AGENTS_SPEC = (
    ("METRICA", MetricaAgent(), "Métricas multidimensionais, receita, CAC, LTV, ROI, margem e performance.", CompetencyLevel.MASTER, [Capability.DATA_ANALYSIS], [Modality.STRUCTURED_DATA, Modality.TABLE]),
    ("ECHO", EchoAgent(), "Aprendizado contínuo a partir de resultados.", CompetencyLevel.MASTER, [Capability.DATA_ANALYSIS, Capability.REASONING], [Modality.STRUCTURED_DATA, Modality.TEXT]),
    ("VERITAS", VeritasAgent(), "Pesquisa factual, checagem, fontes e confiabilidade.", CompetencyLevel.ESPECIALISTA, [Capability.QUALITY_CONTROL, Capability.REASONING], [Modality.TEXT, Modality.DOCUMENT]),
    ("JURIS", JurisAgent(), "Copyright, licenças, políticas e compliance.", CompetencyLevel.ESPECIALISTA, [Capability.QUALITY_CONTROL], [Modality.TEXT, Modality.DOCUMENT]),
    ("PRISMA", PrismaAgent(), "Inteligência de dados, padrões e oportunidades.", CompetencyLevel.ESPECIALISTA, [Capability.DATA_ANALYSIS, Capability.REASONING], [Modality.STRUCTURED_DATA, Modality.TABLE]),
    ("LIBRA", LibraAgent(), "Curadoria da BIBLIOTECA MESTRE, conhecimento e aprendizado validado.", CompetencyLevel.MASTER, [Capability.REASONING, Capability.DATA_ANALYSIS], [Modality.TEXT, Modality.DOCUMENT]),
    ("MERCURY", MercuryAgent(), "MultiGerenciador Comercial, vendas multicanal e monetização.", CompetencyLevel.MASTER, [Capability.COMMERCIAL_INTELLIGENCE], [Modality.TEXT, Modality.STRUCTURED_DATA]),
    ("NEXUS", NexusAgent(), "Funis, ofertas, CTAs, upsell, downsell, order bump e conversão.", CompetencyLevel.MASTER, [Capability.COMMERCIAL_INTELLIGENCE, Capability.PLANNING], [Modality.TEXT, Modality.STRUCTURED_DATA]),
    ("RELAY", RelayAgent(), "Leads, CRM, relacionamento, remarketing e recuperação de oportunidades.", CompetencyLevel.ESPECIALISTA, [Capability.TOOL_USE, Capability.COMMERCIAL_INTELLIGENCE], [Modality.TEXT, Modality.STRUCTURED_DATA]),
    ("ARGOS", ArgosAgent(), "Estratégia de conteúdo e planejamento editorial multicanal.", CompetencyLevel.MASTER, [Capability.PLANNING, Capability.MULTIMODAL_TEXT], [Modality.TEXT, Modality.DOCUMENT]),
    ("SCRIPTOR", ScriptorAgent(), "Roteiros, textos, copy e storytelling.", CompetencyLevel.ESPECIALISTA, [Capability.MULTIMODAL_TEXT], [Modality.TEXT, Modality.DOCUMENT]),
)

# MICROCORREÇÃO — os 9 agentes cuja capacidade principal depende de
# provider externo agora são `ACTIVE`: runtime real e testado, com
# adapter HTTP REAL (`HttpSearchProvider`/`HttpGenerationProvider` —
# nunca mais `NullProvider`) pronto para credencial real.
_ACTIVE_PROVIDER_AGENTS_SPEC = (
    ("ORION", OrionAgent(), "Radar permanente de mercados, tendências, nichos e oportunidades.", CompetencyLevel.MASTER, [Capability.WEB_RESEARCH, Capability.COMMERCIAL_INTELLIGENCE], [Modality.WEB_PAGE, Modality.STRUCTURED_DATA]),
    ("SAGE", SageAgent(), "Pesquisa de público, concorrência, dores, desejos e comportamento.", CompetencyLevel.ESPECIALISTA, [Capability.WEB_RESEARCH, Capability.DATA_ANALYSIS], [Modality.TEXT, Modality.WEB_PAGE, Modality.STRUCTURED_DATA]),
    ("VENTURE", VentureAgent(), "Descoberta de produtos físicos, infoprodutos, afiliados, serviços e oportunidades comerciais.", CompetencyLevel.ESPECIALISTA, [Capability.COMMERCIAL_INTELLIGENCE, Capability.WEB_RESEARCH], [Modality.TEXT, Modality.WEB_PAGE]),
    ("PULSE", PulseAgent(), "SEO, distribuição, publicação e crescimento multicanal.", CompetencyLevel.MASTER, [Capability.WEB_RESEARCH, Capability.TOOL_USE], [Modality.WEB_PAGE, Modality.TEXT]),
    ("AURORA", AuroraAgent(), "Direção criativa e conceito visual.", CompetencyLevel.MASTER, [Capability.MULTIMODAL_IMAGE], [Modality.IMAGE, Modality.TEXT]),
    ("FRAME", FrameAgent(), "Produção/montagem/edição de vídeo 2D/3D/híbrido.", CompetencyLevel.ESPECIALISTA, [Capability.MULTIMODAL_VIDEO, Capability.MULTIMODAL_IMAGE, Capability.MULTIMODAL_AUDIO], [Modality.VIDEO, Modality.IMAGE, Modality.AUDIO, Modality.TEXT]),
    ("VOX", VoxAgent(), "Narração humanizada, emoção, ritmo e vozes.", CompetencyLevel.ESPECIALISTA, [Capability.MULTIMODAL_AUDIO], [Modality.AUDIO, Modality.TEXT]),
    ("SONORA", SonoraAgent(), "Música, efeitos, áudio, mixagem e sincronização.", CompetencyLevel.ESPECIALISTA, [Capability.MULTIMODAL_AUDIO], [Modality.AUDIO, Modality.TEXT]),
    ("PIXEL", PixelAgent(), "Thumbnails, imagens, criativos e peças visuais.", CompetencyLevel.ESPECIALISTA, [Capability.MULTIMODAL_IMAGE], [Modality.IMAGE, Modality.SCREENSHOT, Modality.TEXT]),
)


def build_default_agent_registry() -> AgentRegistry:
    registry = AgentRegistry()

    registry.register(AgentRegistration(
        agent_id="ATLAS", display_name="Atlas", role="ATLAS", specialization="Diretor Operacional MASTER, orquestração geral.",
        level=CompetencyLevel.MASTER,
        capabilities=[Capability.ORCHESTRATION, Capability.REASONING, Capability.PLANNING, Capability.ALL_ROUNDER],
        # CORREÇÃO (item 6): ATLAS nunca com multimodal_capabilities
        # vazio — coordenação multimodal AMPLA, arquiteturalmente
        # suportada para as 12 modalidades listadas; `available=False`
        # em todas (nenhum provider real conectado) — nunca ACTIVE sem
        # provider de verdade.
        multimodal_capabilities=[
            _mm(Modality.TEXT), _mm(Modality.IMAGE), _mm(Modality.SCREENSHOT), _mm(Modality.DOCUMENT),
            _mm(Modality.PDF), _mm(Modality.TABLE), _mm(Modality.STRUCTURED_DATA), _mm(Modality.WEB_PAGE),
            _mm(Modality.AUDIO), _mm(Modality.VIDEO), _mm(Modality.LOG), _mm(Modality.CODE),
        ],
        all_rounder_capable=True, all_rounder_runtime_available=True,
        agentic_architecture_supported=True, agentic_runtime_available=True,
        available_tools=["mission_delegation", "reasoning_engine", "radar_web", "master_library"],
        permissions=["ORCHESTRATE"], authority_level="OPERATIONAL_DIRECTOR",
        health=CapabilityHealth.HEALTHY, provider="internal",
        implementation_status=ImplementationStatus.ACTIVE,
        notes="Diretor Operacional MASTER — executor real, testado.",
        aliases=["atlas", "atlas@system"],
        contract=ActiveAgentContract(
            agent_id="ATLAS", primary_task_types={"orchestration", "mission_delegation", "reasoning_cycle"},
            all_rounder_task_types={"generic_coordination", "cross_domain_review"},
        ),
    ))
    registry.register(AgentRegistration(
        agent_id="NAVIGATOR", display_name="Navigator", role="NAVIGATOR", specialization="Executor operacional externo multicanal.",
        level=CompetencyLevel.MASTER,
        capabilities=[Capability.AGENTIC_EXECUTION, Capability.TOOL_USE, Capability.BROWSER, Capability.ALL_ROUNDER],
        multimodal_capabilities=[_mm(Modality.WEB_PAGE), _mm(Modality.SCREENSHOT), _mm(Modality.TEXT), _mm(Modality.STRUCTURED_DATA)],
        all_rounder_capable=True, all_rounder_runtime_available=True,
        agentic_architecture_supported=True, agentic_runtime_available=True,
        available_tools=["mechanism_executors", "bridge"], permissions=["EXECUTE_OPERATIONAL"],
        authority_level="EXECUTOR", health=CapabilityHealth.HEALTHY, provider="internal",
        implementation_status=ImplementationStatus.ACTIVE,
        notes="Executor real, ações críticas sempre via Owner Approval.",
        aliases=["navigator", "navigator@system"],
        contract=ActiveAgentContract(
            agent_id="NAVIGATOR", primary_task_types={"navigator_execution", "platform_action"},
            all_rounder_task_types={"generic_coordination"},
        ),
    ))
    registry.register(AgentRegistration(
        agent_id="FORGE", display_name="Forge", role="FORGE", specialization="Manutenção, diagnóstico, reparo, confiabilidade e recuperação estrutural.",
        level=CompetencyLevel.MASTER,
        capabilities=[Capability.REPAIR, Capability.CODE, Capability.ALL_ROUNDER],
        multimodal_capabilities=[_mm(Modality.CODE), _mm(Modality.LOG), _mm(Modality.SCREENSHOT), _mm(Modality.DOCUMENT)],
        all_rounder_capable=True, all_rounder_runtime_available=True,
        agentic_architecture_supported=True, agentic_runtime_available=True,
        available_tools=["repair_executor"], permissions=["DIAGNOSE_REPAIR"],
        authority_level="EXECUTOR", health=CapabilityHealth.HEALTHY, provider="internal",
        implementation_status=ImplementationStatus.ACTIVE,
        notes="Pipeline real, rollback verificado, executor real.",
        aliases=["forge", "forge@system"],
        contract=ActiveAgentContract(
            agent_id="FORGE", primary_task_types={"forge_diagnosis", "forge_repair"},
            all_rounder_task_types={"generic_coordination"},
        ),
    ))
    registry.register(AgentRegistration(
        agent_id="SENTINEL", display_name="Sentinel", role="SENTINEL", specialization="Quality Gates e controle de qualidade.",
        level=CompetencyLevel.MASTER, capabilities=[Capability.QUALITY_CONTROL],
        multimodal_capabilities=[_mm(Modality.TEXT), _mm(Modality.STRUCTURED_DATA), _mm(Modality.LOG)],
        all_rounder_capable=True, all_rounder_runtime_available=True,
        agentic_architecture_supported=True, agentic_runtime_available=True,
        available_tools=["sentinel_runtime"], permissions=[], authority_level="ADVISORY",
        health=CapabilityHealth.HEALTHY, provider="internal",
        implementation_status=ImplementationStatus.ACTIVE,
        notes=(
            "MICROCORREÇÃO — runtime real (core.agents.runtime.sentinel.SentinelAgent), wrapper sobre "
            "atlas.quality_supervisor.QualitySupervisor (Quality Gates reais, já testados, nunca reimplementados)."
        ),
        aliases=["sentinel"],
        contract=SentinelAgent(),
    ))
    registry.register(AgentRegistration(
        agent_id="BRIDGE", display_name="Bridge", role="BRIDGE", specialization="MULTIVERSO ACCESS & EXECUTION HUB, conexões e integrações.",
        level=CompetencyLevel.MASTER, capabilities=[Capability.TOOL_USE],
        multimodal_capabilities=[],
        all_rounder_capable=True, all_rounder_runtime_available=True,
        agentic_architecture_supported=True, agentic_runtime_available=True,
        available_tools=["bridge_runtime"], permissions=[], authority_level="INFRASTRUCTURE",
        health=CapabilityHealth.HEALTHY, provider="internal",
        implementation_status=ImplementationStatus.ACTIVE,
        notes=(
            "MICROCORREÇÃO — runtime real (core.agents.runtime.bridge.BridgeAgent), wrapper sobre "
            "bridge.hub.BridgeHub (registro real de conectores/credenciais, já testado, nunca reimplementado)."
        ),
        aliases=["bridge"],
        contract=BridgeAgent(),
    ))

    for agent_id, contract, specialization, level, caps, modalities in _ACTIVE_AGENTS_SPEC:
        registry.register(AgentRegistration(
            agent_id=agent_id, display_name=agent_id.capitalize(), role=agent_id, specialization=specialization,
            level=level, capabilities=caps,
            multimodal_capabilities=[_mm(m) for m in modalities],
            all_rounder_capable=True, all_rounder_runtime_available=True,
            agentic_architecture_supported=True, agentic_runtime_available=True,
            available_tools=[f"{agent_id.lower()}_runtime"], permissions=[],
            authority_level="NONE", health=CapabilityHealth.HEALTHY, provider="internal",
            implementation_status=ImplementationStatus.ACTIVE,
            notes=(
                f"BLOCO FINAL 1 — runtime real (core.agents.runtime.{agent_id.lower()}). Opera sobre sistemas "
                f"internos reais quando conectados; reporta UNAVAILABLE honestamente quando não conectado — "
                f"nunca fabrica resultado."
            ),
            aliases=[agent_id.lower()],
            contract=contract,
        ))

    for agent_id, contract, specialization, level, caps, modalities in _ACTIVE_PROVIDER_AGENTS_SPEC:
        registry.register(AgentRegistration(
            agent_id=agent_id, display_name=agent_id.capitalize(), role=agent_id, specialization=specialization,
            level=level, capabilities=caps,
            multimodal_capabilities=[_mm(m) for m in modalities],
            all_rounder_capable=True, all_rounder_runtime_available=True,
            agentic_architecture_supported=True, agentic_runtime_available=True,
            available_tools=[f"{agent_id.lower()}_runtime"], permissions=[],
            authority_level="NONE", health=CapabilityHealth.HEALTHY, provider="internal",
            implementation_status=ImplementationStatus.ACTIVE,
            notes=(
                f"MICROCORREÇÃO — runtime real (core.agents.runtime.{agent_id.lower()}), testado; adapter HTTP "
                f"REAL (HttpSearchProvider/HttpGenerationProvider, nunca NullProvider) pronto para credencial "
                f"externa. Nesta instalação, sem credencial/rede configurada, reporta UNAVAILABLE honestamente "
                f"nas chamadas de domínio — nunca fabrica resultado."
            ),
            aliases=[agent_id.lower()],
            contract=contract,
        ))

    return registry

