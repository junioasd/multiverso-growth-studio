"""
atlas.director
================
ATLAS — Diretor Operacional do MULTIVERSO GROWTH STUDIO.

Hierarquia obrigatória:

    OWNER (Júnior)
      -> SUPERVISÃO / GOVERNANÇA  (core.governance.facade.GovernedFacade
                                    + core.security.rbac)
        -> ATLAS — DIRETOR OPERACIONAL  (este módulo)
          -> demais IAs, motores e agentes do Growth Studio
             (commercial/*, core/production/*, core/jobs/*, ...)

Princípios estruturais desta implementação:

1. ATLAS NÃO É OWNER. `AtlasDirector` só pode ser construído com um
   `Principal` de `role=Role.ATLAS` (`__post_init__` recusa qualquer
   outra role) — e as Permissions da role ATLAS são fixas em
   `core.security.rbac` (sem MANAGE_CREDENTIALS, sem
   MANAGE_GLOBAL_OPERATIONS, sem VIEW_GLOBAL_DASHBOARD, sem
   APPROVE_PUBLICATION, sem PUBLISH_CONTENT). ATLAS não tem nenhum meio
   de conceder permissões a si mesmo — RBAC é código estático, não
   dado que o ATLAS possa escrever.

2. TODA mutação de negócio (CRM/Sales/Finance/Jobs) que o ATLAS
   solicita passa pela MESMA `GovernedFacade` usada por qualquer outro
   principal — sujeita às MESMAS checagens de identidade, binding,
   RBAC, isolamento e replay já corrigidas nas rodadas de hardening.
   Nenhum atalho, nenhuma via alternativa.

3. Ações que exigiriam autoridade que o ATLAS estruturalmente não tem
   (publicar, gastar, contratar serviço, alterar credenciais, conectar
   conta externa) NUNCA são executadas por `AtlasDirector` — são SEMPRE
   escalonadas ao Owner via `atlas.escalation.EscalationDesk`
   (`request_critical_action`), mesmo que uma checagem de permissão
   isolada por acaso permitisse. Essa é uma barreira estrutural
   adicional, não apenas a ausência de uma Permission.

4. ATLAS nunca inventa métrica, evidência ou resultado — toda análise
   de desempenho (`atlas.performance.PerformanceAnalyst`) vem
   exclusivamente da `GovernedFacade` (dados reais persistidos), e todo
   veredito de qualidade (`atlas.quality_supervisor.QualitySupervisor`)
   vem exclusivamente dos Quality Gates já existentes.

5. Nada neste pacote toca `core/models/db.py`, `commercial/crm.py`,
   `commercial/sales_tracking.py`, `commercial/finance.py`,
   `core/jobs/queue.py` ou `core/governance/facade.py` — todo o
   hardening aprovado até a V6 (atomicidade, idempotência entre
   domínios, concorrência, migração/recuperação) permanece intocado.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atlas.advanced_planning import AdvancedPlan, AdvancedPlanner
from atlas.escalation import EscalationDesk, EscalationItem
from atlas.digital_twin import (
    DecisionLedger, DecisionStatus, DigitalTwin, EvidenceGate, EvidenceRequirement, ReadingStatus,
)
from atlas.learning import AdaptiveStrategyCycle, AtlasLearningCoordinator, CausalStatus, StrategyStatus
from atlas.opportunity import OpportunityAssessment, OpportunityEvaluator
from atlas.performance import (
    DimensionalPerformanceReport,
    MultiDimensionalPerformanceAnalyst,
    PerformanceReport,
    PerformanceAnalyst,
)
from atlas.planning import ObjectivePlanner, OperationalPlan
from atlas.quality_supervisor import QualitySupervisor, SupervisionOutcome
from atlas.radar.engine import CommercialOpportunityRadar
from atlas.radar.models import ChannelType, CommercialOpportunity, RankedOpportunity
from atlas.radar.scheduler import RadarScheduler
from atlas.radar.worker import RadarWorker
from atlas.sentinel import Sentinel
from bridge.hub import BridgeHub
from commercial.funnel_engine import FunnelSelectionInput
from core.agents.base import AgentRegistry, ImplementationStatus, UnknownAgentAlias
from core.agents.default_registry import build_default_agent_registry
from core.agents.runtime.argos import ArgosAgent, register_argos_capabilities
from core.agents.runtime.bridge import BridgeAgent, register_bridge_capabilities
from core.agents.runtime.creative_agents import (
    AuroraAgent, FrameAgent, PixelAgent, SonoraAgent, VoxAgent, register_creative_capabilities,
)
from core.agents.runtime.echo import EchoAgent, register_echo_capabilities
from core.agents.runtime.juris import JurisAgent, register_juris_capabilities
from core.agents.runtime.libra import LibraAgent, register_libra_capabilities
from core.agents.runtime.mercury import MercuryAgent, register_mercury_capabilities
from core.agents.runtime.metrica import MetricaAgent, register_metrica_capabilities
from core.agents.runtime.nexus import NexusAgent, register_nexus_capabilities
from core.agents.runtime.prisma import PrismaAgent, register_prisma_capabilities
from core.agents.runtime.relay import RelayAgent, register_relay_capabilities
from core.agents.runtime.scriptor import ScriptorAgent, register_scriptor_capabilities
from core.agents.runtime.sentinel import SentinelAgent, register_sentinel_capabilities
from atlas.orchestration.graph_executor import GraphExecutor
from atlas.discovery.pipeline import DiscoveryPipeline
from bridge.execution.vault import TokenVault
from bridge.execution.rate_limiter import RateLimiter
from bridge.execution.browser_executor import BrowserExecutor
from bridge.execution.navigator_real import NavigatorReal
from bridge.execution.platforms import YouTubeAdapter, MetaGraphAdapter, TikTokAdapter
from bridge.execution.commercial_platforms import HotmartAdapter, MonetizzeAdapter, GoogleAnalyticsAdapter, GoogleAdsAdapter
from bridge.execution.radar_real import RadarReal
from bridge.execution.webhooks import WebhookDispatcher
from bridge.execution.spend_guard import SpendGuard
from bridge.execution.observability import ExecutionObservability
from core.agents.runtime.veritas import VeritasAgent, register_veritas_capabilities
from core.agents.runtime.web_research_agents import (
    OrionAgent, PulseAgent, SageAgent, VentureAgent, register_web_research_capabilities,
)
from core.learning.loop import LearningLoop as QuantitativeLearningLoop
from core.capabilities.registry import Capability, CapabilityHealth, CapabilityRegistry, CompetencyLevel
from core.coordination.delegation import DelegationRegistry
from core.coordination.mission import MissionAssignment, MissionStatus, MissionTracker
from core.governance.facade import GovernedFacade
from core.library.master_library import MasterLibrary
from core.memory.working_memory import WorkingMemory
from core.multimodal.models import MultimodalReasoner
from core.multimodal.real_analyzers import install_real_analyzers
from core.reasoning.cycle import ReasoningStage, ReasoningTrace
from core.security.rbac import Principal, Role, require_owner
from core.security.sanitize import sanitize_dict
from forge.engineer import ForgeEngineer
from navigator.adapters import DEFAULT_PLATFORMS as NAVIGATOR_DEFAULT_PLATFORMS
from navigator.executor import NavigatorExecutor
from radar_web.adoption import AdoptionPipeline
from radar_web.engine import RadarWebComercial
from radar_web.evolution import AnnualReviewEngine, StrategyHistoryStore
from radar_web.scheduler import RadarWebScheduler
from radar_web.worker import RadarWebWorker

# Ações que o ATLAS estruturalmente NUNCA executa — sempre escalonadas.
# EXPANSÃO (item 12 — autonomia controlada): lista ampliada com as
# categorias adicionais pedidas — nunca REMOVE nenhuma das 5 originais
# (ATLAS V1), só adiciona.
CRITICAL_ACTION_TYPES = frozenset({
    "publish_content",
    "spend_money",
    "contract_service",
    "modify_credentials",
    "connect_external_account",
    "transfer_funds",
    "destructive_deletion",
    "modify_security",
    "modify_ownership",
    "critical_structural_change",
    "sensitive_publication",
    "irreversible_action",
})


class AtlasAuthorityViolation(Exception):
    """Levantado quando algo tenta fazer o ATLAS agir com autoridade
    que ele estruturalmente não possui. Nunca contornada — sempre
    recusada e auditada."""


class AtlasHierarchyViolation(Exception):
    """Levantado quando `AtlasDirector` é construído ou operado fora da
    hierarquia obrigatória (ex: com um Principal que não é ATLAS)."""


@dataclass
class AtlasDirector:
    facade: GovernedFacade
    principal: Principal
    # ATLAS+RADAR (item 3 — persistência de aprovações do Owner): quando
    # informado, `EscalationDesk` persiste solicitações críticas em
    # SQLite — sobrevivem a reinício/restart/nova sessão. `None` (padrão)
    # preserva o comportamento em memória do ATLAS V1 para quem não
    # passar `db_path` explicitamente.
    db_path: str | None = None
    # EXPANSÃO — identidades subordinadas do NAVIGATOR/FORGE. ATLAS
    # nunca opera "como" NAVIGATOR/FORGE — sempre delega a um
    # `Principal` PRÓPRIO dessas roles (RBAC de menor privilégio
    # dedicado, ver `core/security/rbac.py`), construído aqui só se o
    # chamador não fornecer um.
    navigator_principal: Principal = None  # type: ignore[assignment]
    forge_principal: Principal = None  # type: ignore[assignment]
    planner: ObjectivePlanner = field(default_factory=ObjectivePlanner)
    advanced_planner: AdvancedPlanner = field(default_factory=AdvancedPlanner)
    quality_supervisor: QualitySupervisor = field(default_factory=QualitySupervisor)
    sentinel: Sentinel = field(init=False)
    bridge: BridgeHub = field(default_factory=BridgeHub)
    memory: WorkingMemory = field(default_factory=WorkingMemory)
    escalation_desk: EscalationDesk = field(init=False)
    opportunity_evaluator: OpportunityEvaluator = field(init=False)
    performance_analyst: PerformanceAnalyst = field(init=False)
    multidimensional_analyst: MultiDimensionalPerformanceAnalyst = field(init=False)
    radar: CommercialOpportunityRadar = field(init=False)
    radar_scheduler: RadarScheduler = field(init=False)
    radar_worker: RadarWorker = field(init=False)
    learning: AtlasLearningCoordinator = field(init=False)
    missions: MissionTracker = field(init=False)
    library: MasterLibrary = field(init=False)
    navigator: NavigatorExecutor = field(init=False)
    forge: ForgeEngineer = field(init=False)
    # EXPANSÃO (agentic-first/multimodal/all-rounder/RADAR WEB COMERCIAL)
    capability_registry: CapabilityRegistry = field(init=False)
    agent_registry: AgentRegistry = field(init=False)
    multimodal_reasoner: MultimodalReasoner = field(init=False)
    radar_web: RadarWebComercial = field(init=False)
    strategy_history: StrategyHistoryStore = field(init=False)
    annual_review: AnnualReviewEngine = field(init=False)
    adoption_pipeline: AdoptionPipeline = field(init=False)
    delegation_registry: DelegationRegistry = field(init=False)
    radar_web_scheduler: RadarWebScheduler = field(init=False)
    radar_web_worker: RadarWebWorker = field(init=False)

    def __post_init__(self) -> None:
        if self.principal.role != Role.ATLAS:
            raise AtlasHierarchyViolation(
                f"AtlasDirector exige um Principal de role ATLAS — recebeu '{self.principal.role.value}'. "
                "ATLAS não é Owner e não pode operar com nenhuma outra autoridade."
            )
        self.escalation_desk = EscalationDesk(self.facade.audit, db_path=self.db_path)
        self.opportunity_evaluator = OpportunityEvaluator(self.facade.mgc)
        self.performance_analyst = PerformanceAnalyst(self.facade)
        self.multidimensional_analyst = MultiDimensionalPerformanceAnalyst(self.facade)
        self.sentinel = Sentinel(self.quality_supervisor)
        self.missions = MissionTracker(db_path=self.db_path)
        # ATLAS EXPANSION 1 (orquestração executável): motor real de
        # execução de grafo/loop/paralelismo — construído SOBRE
        # `self.missions`/`self.facade.job_queue`/`self.escalation_desk`
        # já existentes, nunca duplicando nenhum deles.
        self.graph_executor = GraphExecutor(
            mission_tracker=self.missions, job_queue=self.facade.job_queue, escalation_desk=self.escalation_desk,
            db_path=self.db_path,
        )
        # ATLAS PIPELINE 1 (inteligência e descoberta): orquestra a
        # descoberta de oportunidades de CONTEÚDO (distinto de
        # `self.radar`, que é comercial/oferta) SOBRE o MESMO
        # `graph_executor`/`missions`/`job_queue`/`escalation_desk` —
        # nunca um motor de execução paralelo.
        self.discovery_pipeline = DiscoveryPipeline(
            mission_tracker=self.missions, job_queue=self.facade.job_queue, escalation_desk=self.escalation_desk,
            db_path=self.db_path, audit=self.facade.audit,
        )
        self.library = MasterLibrary(db_path=self.db_path)
        self.radar = CommercialOpportunityRadar()
        self.radar_scheduler = RadarScheduler(
            job_queue=self.facade.job_queue, radar=self.radar, db_path=self.db_path,
        )
        # ATLAS+RADAR — microcorreção final: worker de fundo construído
        # aqui, mas NUNCA iniciado automaticamente na construção do
        # AtlasDirector (evita threads surpresa em toda instanciação de
        # testes/objetos). Quem decide QUANDO o worker realmente roda é
        # o runtime que efetivamente serve a aplicação —
        # `main.py cmd_serve` (via `build_app(start_radar_worker=True)`)
        # ou qualquer chamador explícito de `self.radar_worker.start()`.
        self.radar_worker = RadarWorker(scheduler=self.radar_scheduler)
        self.learning = AtlasLearningCoordinator(self.facade.mgc.learning_loop, self.facade.audit)

        # EXPANSÃO — NAVIGATOR e FORGE ganham identidades PRÓPRIAS
        # (nunca operam "como" ATLAS). Escopo coringa "*" por padrão —
        # como o próprio ATLAS — ajustável passando um Principal
        # explícito ao construir o AtlasDirector.
        if self.navigator_principal is None:
            self.navigator_principal = Principal(subject="navigator@system", role=Role.NAVIGATOR, scopes={"*"})
        if self.forge_principal is None:
            self.forge_principal = Principal(subject="forge@system", role=Role.FORGE, scopes={"*"})
        # CORREÇÃO (item 5 — BRIDGE + NAVIGATOR): um ÚNICO BridgeHub
        # compartilhado entre ATLAS e NAVIGATOR (contrato claro
        # ATLAS -> NAVIGATOR -> BRIDGE -> EXECUTOR/ADAPTER). Fallback
        # MOCK explícito pré-registrado para as plataformas conhecidas
        # — nenhum mecanismo REAL é inventado; uma integração real
        # precisaria ser registrada explicitamente via
        # `atlas.bridge.register_connector(..., is_configured=True, ...)`.
        for platform in NAVIGATOR_DEFAULT_PLATFORMS:
            if not self.bridge.available_connectors(platform=platform):
                self.bridge.register_mock_fallback(platform=platform)
        self.navigator = NavigatorExecutor(
            principal=self.navigator_principal, escalation_desk=self.escalation_desk,
            audit=self.facade.audit, db_path=self.db_path, bridge=self.bridge,
        )
        self.forge = ForgeEngineer(principal=self.forge_principal, audit=self.facade.audit, db_path=self.db_path)

        # EXPANSÃO — item 25/26: Capability Registry com uma linha de
        # base CONCEDIDA por esta inicialização (nunca autodeclarada
        # pelos próprios agentes) para ATLAS/NAVIGATOR/FORGE. Todas as
        # capacidades "reais" (multimodal, web research) começam
        # `available=False` — nunca declaradas disponíveis sem
        # provider/tool funcional; quem quiser ativá-las precisa
        # registrar explicitamente um provider real.
        self.capability_registry = CapabilityRegistry()
        # CORREÇÃO (item 10): registro dos 25 agentes — ATLAS consulta
        # este registro (junto do capability_registry) ANTES de
        # delegar, nunca seleciona por nome/fallback genérico.
        self.agent_registry = build_default_agent_registry()
        self.capability_registry.register(
            agent="ATLAS", capability=Capability.ORCHESTRATION,
            level=CompetencyLevel.MASTER, granted_by="system_bootstrap", provider="internal",
            available=True, authorized=True, health=CapabilityHealth.HEALTHY,
        )
        self.capability_registry.register(
            agent="ATLAS", capability=Capability.REASONING,
            level=CompetencyLevel.MASTER, granted_by="system_bootstrap", provider="internal",
            available=True, authorized=True, health=CapabilityHealth.HEALTHY,
        )
        self.capability_registry.register(
            agent="ATLAS", capability=Capability.ALL_ROUNDER,
            level=CompetencyLevel.MASTER, granted_by="system_bootstrap", provider="internal",
            available=True, authorized=True, health=CapabilityHealth.HEALTHY,
        )
        self.capability_registry.register(
            agent="NAVIGATOR", capability=Capability.AGENTIC_EXECUTION,
            level=CompetencyLevel.SENIOR, granted_by=self.principal.subject, provider="internal",
            available=True, authorized=True, health=CapabilityHealth.HEALTHY,
        )
        self.capability_registry.register(
            agent="NAVIGATOR", capability=Capability.ALL_ROUNDER,
            level=CompetencyLevel.PLENO, granted_by=self.principal.subject, provider="internal",
            available=True, authorized=True, health=CapabilityHealth.HEALTHY,
        )
        self.capability_registry.register(
            agent="FORGE", capability=Capability.REPAIR,
            level=CompetencyLevel.SENIOR, granted_by=self.principal.subject, provider="internal",
            available=True, authorized=True, health=CapabilityHealth.HEALTHY,
        )
        self.capability_registry.register(
            agent="FORGE", capability=Capability.ALL_ROUNDER,
            level=CompetencyLevel.PLENO, granted_by=self.principal.subject, provider="internal",
            available=True, authorized=True, health=CapabilityHealth.HEALTHY,
        )

        # BLOCO FINAL 1 (item 1 — 25 AGENTES: eliminar SCAFFOLD onde
        # ainda existirem): instancia os 20 agentes reais, conectando-os
        # aos sistemas internos JÁ REAIS desta própria inicialização
        # (`self.facade.crm`/`.sales_tracker`/`.finance`, `self.library`)
        # — nunca fabricando um sistema paralelo. Os 9 dependentes de
        # provider externo (web research + criativos) ficam com o
        # `NullProvider` padrão (honesto UNAVAILABLE) até que uma
        # integração real de rede/API seja conectada pelo operador.
        self.quantitative_learning_loop = QuantitativeLearningLoop(db_path=self.db_path)
        # COMANDO 2/6 — Gêmeo Digital + memória de decisões + ciclo
        # adaptativo (mesmo db_path: sobrevivem a restart juntos).
        self.twin = DigitalTwin(db_path=self.db_path)
        self.decisions = DecisionLedger(db_path=self.db_path)
        self.evidence_gate = EvidenceGate(self.twin)
        # COMANDO 4/6 — laço agentic-first sobre o MESMO registro de agentes.
        from atlas.agentic import AgenticCoordinator

        self.agentic = AgenticCoordinator(registry=self.agent_registry, audit=self.facade.audit,
                                          actor=self.principal.subject)
        self.adaptive_cycle = AdaptiveStrategyCycle(
            twin=self.twin, learning_loop=self.quantitative_learning_loop, ledger=self.decisions,
            gate=self.evidence_gate, actor=self.principal.subject, db_path=self.db_path,
        )
        self.metrica = MetricaAgent(finance_ledger=self.facade.finance, sales_tracker=self.facade.sales_tracker, audit=self.facade.audit)
        self.echo = EchoAgent(learning_loop=self.quantitative_learning_loop, audit=self.facade.audit)
        self.veritas = VeritasAgent(audit=self.facade.audit)  # evidence_registry conectado sob demanda pelo chamador
        self.juris = JurisAgent(audit=self.facade.audit)
        self.prisma = PrismaAgent(audit=self.facade.audit)
        self.libra = LibraAgent(library=self.library, audit=self.facade.audit)
        self.mercury = MercuryAgent(crm=self.facade.crm, finance_ledger=self.facade.finance, sales_tracker=self.facade.sales_tracker, audit=self.facade.audit)
        self.nexus = NexusAgent(crm=self.facade.crm, audit=self.facade.audit)
        self.relay = RelayAgent(crm=self.facade.crm, audit=self.facade.audit)
        self.argos = ArgosAgent(learning_loop=self.quantitative_learning_loop, audit=self.facade.audit)
        self.scriptor = ScriptorAgent(audit=self.facade.audit)
        self.orion = OrionAgent(audit=self.facade.audit)
        self.sage = SageAgent(audit=self.facade.audit)
        self.venture = VentureAgent(audit=self.facade.audit)
        self.pulse = PulseAgent(audit=self.facade.audit)
        self.aurora = AuroraAgent(audit=self.facade.audit)
        self.pixel = PixelAgent(audit=self.facade.audit)
        self.frame = FrameAgent(audit=self.facade.audit)
        self.vox = VoxAgent(audit=self.facade.audit)
        self.sonora = SonoraAgent(audit=self.facade.audit)
        # MICROCORREÇÃO (eliminar PARTIAL: SENTINEL/BRIDGE): usa nomes
        # de atributo DISTINTOS (`sentinel_agent`/`bridge_agent`) —
        # `self.sentinel`/`self.bridge` já existem e são usados
        # ativamente em outros pontos deste arquivo (nunca sobrescritos).
        # Ambos envolvem as MESMAS instâncias reais já construídas
        # (`self.quality_supervisor`/`self.bridge`), nunca um sistema
        # paralelo.
        self.sentinel_agent = SentinelAgent(quality_supervisor=self.quality_supervisor, audit=self.facade.audit)
        self.bridge_agent = BridgeAgent(hub=self.bridge, audit=self.facade.audit)

        # BLOCO FINAL 2 (item 1 — ACCESS & EXECUTION HUB): conecta o
        # cofre real de tokens, rate limiter real, executor de
        # navegador real, e o orquestrador NAVIGATOR REAL — todos
        # compartilhando `self.bridge` (BridgeHub já real) e
        # `self.escalation_desk`/`self.facade.audit` já existentes.
        self.token_vault = TokenVault(db_path=self.db_path)
        self.rate_limiter = RateLimiter()
        self.browser_executor = BrowserExecutor()
        self.navigator_real = NavigatorReal(
            bridge_hub=self.bridge, escalation_desk=self.escalation_desk, browser_executor=self.browser_executor,
            db_path=self.db_path, audit=self.facade.audit,
        )
        # adapters reais por plataforma -- registrados com o MESMO
        # vault/rate_limiter, nunca instâncias paralelas desconectadas.
        self.navigator_real.register_adapter(platform="youtube", adapter=YouTubeAdapter(vault=self.token_vault, rate_limiter=self.rate_limiter))
        self.navigator_real.register_adapter(platform="facebook", adapter=MetaGraphAdapter(sub_platform="facebook", vault=self.token_vault, rate_limiter=self.rate_limiter))
        self.navigator_real.register_adapter(platform="instagram", adapter=MetaGraphAdapter(sub_platform="instagram", vault=self.token_vault, rate_limiter=self.rate_limiter))
        self.navigator_real.register_adapter(platform="tiktok", adapter=TikTokAdapter(vault=self.token_vault, rate_limiter=self.rate_limiter))
        self.navigator_real.register_adapter(platform="hotmart", adapter=HotmartAdapter(vault=self.token_vault, rate_limiter=self.rate_limiter))
        self.navigator_real.register_adapter(platform="monetizze", adapter=MonetizzeAdapter(vault=self.token_vault, rate_limiter=self.rate_limiter))
        self.navigator_real.register_adapter(platform="google_analytics", adapter=GoogleAnalyticsAdapter(vault=self.token_vault, rate_limiter=self.rate_limiter))
        self.navigator_real.register_adapter(platform="google_ads", adapter=GoogleAdsAdapter(vault=self.token_vault, rate_limiter=self.rate_limiter))

        # BLOCO FINAL 2 (item 3 — RADAR REAL)
        # MARCO 2 - LOCAL READONLY EXECUTION
        # Caminho real, explicitamente registrado; nenhum bypass de BridgeHub.
        from bridge.hub import ConnectorType, HealthStatus
        from atlas.execution.local_atlas_adapter import LocalAtlasAdapter

        self.bridge.register_connector(
            platform="local",
            connector_type=ConnectorType.LOCAL_EXECUTION,
            is_configured=True,
            enabled=True,
            authenticated=True,
            scopes=["local_readonly"],
            required_scopes=["local_readonly"],
            health=HealthStatus.OK,
            permissions_ok=True,
            notes="Local readonly executor governed by BridgeHub/NavigatorReal.",
        )

        self.navigator_real.register_adapter(
            platform="local",
            adapter=LocalAtlasAdapter(),
        )

        self.radar_real = RadarReal(browser_executor=self.browser_executor)

        # BLOCO FINAL 2 (item 5 — automação externa: webhooks)
        self.webhook_dispatcher = WebhookDispatcher()

        # BLOCO FINAL 2 (item 6 — operação orgânica primeiro): usa o
        # MESMO EscalationDesk -- zero gasto sem aprovação explícita.
        self.spend_guard = SpendGuard(escalation_desk=self.escalation_desk, db_path=self.db_path)

        # BLOCO FINAL 2 (item 7 — observabilidade estruturada)
        self.execution_observability = ExecutionObservability(db_path=self.db_path)

        for register_fn in (
            register_metrica_capabilities, register_echo_capabilities, register_veritas_capabilities,
            register_juris_capabilities, register_prisma_capabilities, register_libra_capabilities,
            register_mercury_capabilities, register_nexus_capabilities, register_relay_capabilities,
            register_argos_capabilities, register_scriptor_capabilities, register_sentinel_capabilities,
            register_bridge_capabilities,
        ):
            register_fn(self.capability_registry, granted_by=self.principal.subject)
        register_web_research_capabilities(self.capability_registry, granted_by=self.principal.subject)
        register_creative_capabilities(self.capability_registry, granted_by=self.principal.subject)

        self.multimodal_reasoner = MultimodalReasoner()
        # BLOCO FINAL 1 (item 2): instala analisadores REAIS para as
        # modalidades genuinamente processáveis sem provider externo
        # (TEXT/STRUCTURED_DATA/TABLE) — as demais seguem honestamente
        # MOCK/UNAVAILABLE até que um provider real seja conectado.
        install_real_analyzers(self.multimodal_reasoner)

        # EXPANSÃO — RADAR WEB COMERCIAL + Visão Evolutiva
        self.radar_web = RadarWebComercial()
        self.strategy_history = StrategyHistoryStore(db_path=self.db_path)
        self.annual_review = AnnualReviewEngine(radar=self.radar_web, history=self.strategy_history)
        self.adoption_pipeline = AdoptionPipeline()
        self.delegation_registry = DelegationRegistry()
        # CORREÇÃO (item 3): scheduler+worker PRÓPRIOS do RADAR WEB
        # COMERCIAL — separados do RADAR DE OPORTUNIDADES
        # (`self.radar_scheduler`/`self.radar_worker`), nunca
        # iniciados automaticamente na construção (mesma disciplina já
        # aprovada do RADAR DE OPORTUNIDADES — quem decide QUANDO
        # rodar é o runtime/chamador explícito).
        self.radar_web_scheduler = RadarWebScheduler(
            job_queue=self.facade.job_queue, radar=self.radar_web, db_path=self.db_path,
        )
        self.radar_web_worker = RadarWebWorker(scheduler=self.radar_web_scheduler)

    # ------------------------------------------------------------------
    # item 3: objetivos do Owner -> plano operacional
    # ------------------------------------------------------------------

    def receive_objective(
        self, owner_principal: Principal, *, objective: str, channel_id: str
    ) -> OperationalPlan:
        """Só aceita objetivos vindos de uma sessão VERIFICADA do Owner
        — ATLAS nunca "decide" sozinho qual é o objetivo estratégico, ele
        só o transforma em plano operacional."""
        require_owner(owner_principal)
        plan = self.planner.build_plan(objective=objective, channel_id=channel_id)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_receive_objective", resource=plan.plan_id,
            outcome="SUCCESS", details=sanitize_dict({"channel_id": channel_id}),
        )
        return plan

    # ------------------------------------------------------------------
    # itens 6-9: oportunidades comerciais / MultiGerenciador Comercial
    # ------------------------------------------------------------------

    def evaluate_opportunities(
        self,
        *,
        channel_id: str,
        platform: str,
        affiliate_platform: str,
        niche: str,
        funnel_request: FunnelSelectionInput,
        audience_fit_by_product: dict[str, float] | None = None,
    ) -> OpportunityAssessment:
        assessment = self.opportunity_evaluator.assess(
            channel_id=channel_id, platform=platform, affiliate_platform=affiliate_platform,
            niche=niche, funnel_request=funnel_request, audience_fit_by_product=audience_fit_by_product,
        )
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_evaluate_opportunities", resource=f"channel:{channel_id}",
            outcome="SUCCESS", details={"opportunities_found": len(assessment.opportunities)},
        )
        return assessment

    # ------------------------------------------------------------------
    # itens 10-12: análise de performance com dados reais
    # ------------------------------------------------------------------

    def analyze_performance(
        self,
        *,
        channel_id: str | None = None,
        marketing_spend: float | None = None,
        new_customers: int | None = None,
        avg_revenue_per_customer: float | None = None,
        avg_customer_lifespan_periods: float | None = None,
    ) -> PerformanceReport:
        """Usa o PRÓPRIO Principal do ATLAS — sujeito ao mesmo
        isolamento por canal que qualquer outro principal. Se o ATLAS
        não tiver escopo para o canal pedido, a `GovernedFacade`
        recusa (IsolationViolation), exatamente como recusaria para
        qualquer outro chamador."""
        return self.performance_analyst.analyze(
            self.principal, channel_id=channel_id, marketing_spend=marketing_spend,
            new_customers=new_customers, avg_revenue_per_customer=avg_revenue_per_customer,
            avg_customer_lifespan_periods=avg_customer_lifespan_periods,
        )

    # ------------------------------------------------------------------
    # itens 13-14: Quality Gates -> bloqueio ou escalonamento
    # ------------------------------------------------------------------

    def supervise_video(self, subject: dict) -> SupervisionOutcome:
        outcome = self.quality_supervisor.review_video(subject)
        self._record_supervision(outcome, resource_label="video")
        return outcome

    def supervise_commercial_scaling(self, subject: dict) -> SupervisionOutcome:
        outcome = self.quality_supervisor.review_commercial_scaling(subject)
        self._record_supervision(outcome, resource_label="commercial_scaling")
        return outcome

    def supervise_affiliate_compliance(self, subject: dict) -> SupervisionOutcome:
        outcome = self.quality_supervisor.review_affiliate_compliance(subject)
        self._record_supervision(outcome, resource_label="affiliate_compliance")
        return outcome

    def _record_supervision(self, outcome: SupervisionOutcome, *, resource_label: str) -> None:
        self.facade.audit.record(
            actor=self.principal.subject, action=f"atlas_supervise_{resource_label}",
            resource=outcome.gate_name, outcome="SUCCESS",
            details={"decision": outcome.decision.value, "verdict": outcome.gate_result.verdict.value},
        )
        if outcome.decision.value == "ESCALATE":
            self.escalation_desk.escalate(
                reason=f"Quality Gate '{outcome.gate_name}' exige revisão humana "
                f"(veredito: {outcome.gate_result.verdict.value})",
                context={"gate": outcome.gate_name, "reasons": outcome.gate_result.reasons},
                actor=self.principal.subject,
            )

    # ------------------------------------------------------------------
    # item 15: ações críticas — SEMPRE escalonadas, nunca executadas
    # ------------------------------------------------------------------

    def request_critical_action(
        self, *, action_type: str, details: dict, risk: str = "HIGH", idempotency_key: str | None = None,
    ) -> EscalationItem:
        if action_type not in CRITICAL_ACTION_TYPES:
            raise ValueError(
                f"tipo de ação crítica desconhecido: '{action_type}' — "
                f"esperado um de {sorted(CRITICAL_ACTION_TYPES)}"
            )
        return self.escalation_desk.escalate(
            reason=f"ação crítica requer autoridade do Owner: {action_type}",
            context={"action_type": action_type, **details},
            actor=self.principal.subject, operation=action_type, risk=risk,
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------------
    # item 16: escalonamento explícito de qualquer outro motivo
    # ------------------------------------------------------------------

    def escalate(self, *, reason: str, context: dict) -> EscalationItem:
        return self.escalation_desk.escalate(reason=reason, context=context, actor=self.principal.subject)

    # ------------------------------------------------------------------
    # item 17: Learning Loop
    # ------------------------------------------------------------------

    def record_learning_observation(self, *, scope_key: str, outcome: str, context: dict, limitations: str = ""):
        return self.learning.record_observation(
            scope_key=scope_key, outcome=outcome, context=context, limitations=limitations,
            actor=self.principal.subject,
        )

    # ------------------------------------------------------------------
    # ATLAS+RADAR item 1: Radar Permanente de Oportunidades Comerciais
    # ------------------------------------------------------------------
    #
    # O radar em si (`atlas.radar.engine.CommercialOpportunityRadar`)
    # não faz nenhuma checagem de autoridade — ele só descobre e
    # classifica dados, nunca executa nada. As checagens de hierarquia
    # do ATLAS entram aqui: `scan_opportunities`/`rank_opportunities`
    # são operações de LEITURA/análise (qualquer principal ATLAS pode
    # chamar, sem exigir Owner), mas NENHUM resultado do radar jamais
    # aciona uma ação crítica automaticamente — isso continua
    # exclusivamente em `request_critical_action`.

    def scan_opportunities(
        self, *, channel_types: set[ChannelType] | None = None, niche: str | None = None,
    ) -> list[CommercialOpportunity]:
        opportunities = self.radar.scan(channel_types=channel_types, niche=niche)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_radar_scan", resource=niche or "GLOBAL",
            outcome="SUCCESS", details={"opportunities_found": len(opportunities)},
        )
        return opportunities

    def rank_opportunities(
        self,
        opportunities: list[CommercialOpportunity],
        *,
        policy_compatible_by_id: dict[str, bool] | None = None,
        risk_level_by_id: dict[str, str] | None = None,
    ) -> list[RankedOpportunity]:
        ranked = self.radar.rank(
            opportunities, policy_compatible_by_id=policy_compatible_by_id,
            risk_level_by_id=risk_level_by_id, learning_loop=self.facade.mgc.learning_loop,
        )
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_radar_rank", resource="ranking",
            outcome="SUCCESS", details={"count": len(ranked)},
        )
        return ranked

    def supervise_opportunity(
        self, opportunity: CommercialOpportunity, *, policy_compatible: bool | None = None,
        risk_level: str | None = None,
    ) -> SupervisionOutcome:
        """item 5: integra o Radar ao mesmo Quality Gate/decisão
        PROCEED|BLOCK|ESCALATE usado para vídeo/comercial/afiliados."""
        subject = {
            "opportunity": opportunity,
            "policy_compatible": policy_compatible,
            "risk_level": risk_level,
            "contradictory_data": self.radar.has_contradictory_signals(opportunity),
        }
        outcome = self.quality_supervisor.review_opportunity(subject)
        self._record_supervision(outcome, resource_label="opportunity")
        return outcome

    def record_opportunity_outcome(
        self, *, opportunity: CommercialOpportunity, outcome: str, context: dict, limitations: str = "",
    ):
        """item 6: aprendizado com resultados comerciais REAIS de uma
        oportunidade do radar — nunca altera o ranking/gate diretamente,
        só acumula observação para consulta futura (ver `atlas.learning`)."""
        scope_key = self.radar.learning_scope_key(opportunity)
        return self.record_learning_observation(
            scope_key=scope_key, outcome=outcome, context=context, limitations=limitations,
        )

    # ------------------------------------------------------------------
    # ATLAS+RADAR — execução recorrente/controlada do Radar (correção
    # residual). Reutiliza `core.jobs.queue.JobQueue` (via
    # `atlas.radar.scheduler.RadarScheduler`) — nunca um agendador novo,
    # nunca uma dependência de serviço pago. `run_scheduled_radar_scan`
    # só executa `radar.scan()` (leitura/descoberta) — nenhuma ação
    # crítica é sequer alcançável a partir daqui.
    # ------------------------------------------------------------------

    def enable_radar_schedule(self, *, interval_seconds: float = 3600.0):
        state = self.radar_scheduler.enable(interval_seconds=interval_seconds)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_radar_schedule_enable", resource="radar_schedule",
            outcome="SUCCESS", details={"interval_seconds": interval_seconds},
        )
        return state

    def disable_radar_schedule(self):
        state = self.radar_scheduler.disable()
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_radar_schedule_disable", resource="radar_schedule",
            outcome="SUCCESS", details={},
        )
        return state

    def radar_schedule_status(self):
        return self.radar_scheduler.status()

    def run_scheduled_radar_scan(self, *, niche: str | None = None):
        job = self.radar_scheduler.run_if_due(niche=niche)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_radar_scheduled_scan", resource="radar_schedule",
            outcome="SUCCESS", details={"job_state": job.state.value if job is not None else "NOT_DUE"},
        )
        return job

    def start_radar_worker(self) -> None:
        """Inicia o worker/loop interno (item exigido pela microcorreção
        final) — a partir daqui, ciclos devidos rodam sozinhos, sem
        exigir chamadas manuais a `run_scheduled_radar_scan()`."""
        self.radar_worker.start()
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_radar_worker_start", resource="radar_worker",
            outcome="SUCCESS", details={},
        )

    def stop_radar_worker(self, *, timeout: float = 10.0) -> None:
        """Encerra o worker de forma limpa (join com timeout) — nunca
        deixa thread órfã."""
        self.radar_worker.stop(timeout=timeout)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_radar_worker_stop", resource="radar_worker",
            outcome="SUCCESS", details={},
        )

    # ------------------------------------------------------------------
    # CORREÇÃO (item 3) — worker/loop do RADAR WEB COMERCIAL (separado
    # do RADAR DE OPORTUNIDADES acima)
    # ------------------------------------------------------------------

    def start_radar_web_worker(self) -> None:
        self.radar_web_worker.start()
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_radar_web_worker_start", resource="radar_web_worker",
            outcome="SUCCESS", details={},
        )

    def stop_radar_web_worker(self, *, timeout: float = 10.0) -> None:
        self.radar_web_worker.stop(timeout=timeout)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_radar_web_worker_stop", resource="radar_web_worker",
            outcome="SUCCESS", details={},
        )

    # ------------------------------------------------------------------
    # ATLAS+RADAR item 2: desempenho comercial multidimensional
    # ------------------------------------------------------------------

    def analyze_performance_multidimensional(
        self,
        *,
        channel_id: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        funnel_id: str | None = None,
        platform: str | None = None,
        traffic_source: str | None = None,
    ) -> DimensionalPerformanceReport:
        return self.multidimensional_analyst.analyze(
            self.principal, channel_id=channel_id, product_id=product_id, campaign_id=campaign_id,
            funnel_id=funnel_id, platform=platform, traffic_source=traffic_source,
        )

    def breakdown_performance(
        self, *, group_by: str, channel_id: str | None = None, **fixed_filters,
    ) -> dict:
        """Cruzamento de dimensões (ex: "produto x canal" ->
        `breakdown_performance(group_by="product_id", channel_id="chan_1")`,
        ou "campanha x canal" -> `group_by="campaign_id"`)."""
        return self.multidimensional_analyst.breakdown(
            self.principal, group_by=group_by, channel_id=channel_id, **fixed_filters,
        )

    # ------------------------------------------------------------------
    # EXPANSÃO item 5: coordenação multiagente — delegação formal
    # ------------------------------------------------------------------

    def delegate_mission(
        self,
        *,
        agent: str,
        objective: str,
        context: dict | None = None,
        permissions: list | None = None,
        tools_allowed: list | None = None,
        restrictions: list | None = None,
        success_criteria: list | None = None,
        deadline: float | None = None,
        dependencies: list | None = None,
        evidence_expected: list | None = None,
        mission_id: str | None = None,
        task_id: str | None = None,
    ) -> MissionAssignment:
        """Cria e persiste uma delegação formal — ATLAS acompanha o
        `status` (QUEUED/PLANNED/RUNNING/.../VERIFIED/CANCELLED) via
        `self.missions`; a EXECUÇÃO em si é responsabilidade do agente
        destinatário (NAVIGATOR/FORGE/especialista)."""
        assignment = MissionAssignment.new(
            agent=agent, objective=objective, mission_id=mission_id, task_id=task_id,
            context=context or {}, permissions=permissions or [], tools_allowed=tools_allowed or [],
            restrictions=restrictions or [], success_criteria=success_criteria or [], deadline=deadline,
            dependencies=dependencies or [], evidence_expected=evidence_expected or [],
        )
        created = self.missions.create(assignment)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_delegate_mission",
            resource=f"{created.mission_id}:{created.task_id}", outcome="SUCCESS",
            details={"agent": agent, "objective": objective},
        )
        return created

    def update_mission_status(
        self, mission_id: str, task_id: str, *, status: MissionStatus, result: dict | None = None,
        error: str | None = None,
    ) -> MissionAssignment:
        updated = self.missions.transition(mission_id, task_id, status=status, result=result, error=error)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_mission_status_update",
            resource=f"{mission_id}:{task_id}", outcome="SUCCESS", details={"status": status.value},
        )
        return updated

    # ------------------------------------------------------------------
    # EXPANSÃO item 7: consulta à Biblioteca Mestre antes de tarefas
    # ------------------------------------------------------------------

    def consult_library(self, *, area: str | None = None, tag: str | None = None) -> list:
        """Só devolve conhecimento VALIDADO — nunca um aprendizado ainda
        pendente de revisão."""
        return self.library.search(area=area, tag=tag, only_validated=True)

    # ------------------------------------------------------------------
    # EXPANSÃO item 3: ciclo de raciocínio padrão (OBSERVAR..REPORTAR)
    # ------------------------------------------------------------------

    def start_reasoning_trace(self, *, objective: str) -> ReasoningTrace:
        return ReasoningTrace.start(agent=self.principal.subject, objective=objective)

    def run_mission_with_reasoning(
        self, mission_id: str, task_id: str, *, navigator_tasks: list, channel_id: str | None = None,
    ) -> tuple:
        """CORREÇÃO (raciocínio: integração operacional real).

        Aplica o ciclo COMPLETO (OBSERVE->...->REPORT) a uma missão JÁ
        delegada, usando o NAVIGATOR de verdade (nunca inventa
        resultado) via `core.reasoning.adapters.NavigatorTaskExecutor`.

        CORREÇÃO (item 1 — agentic-first: executor deve respeitar o
        agente designado): este método SEMPRE executa via NAVIGATOR —
        antes de prosseguir, verifica que `mission.agent == "NAVIGATOR"`
        OU que existe uma `DelegationRecord` válida autorizando o
        NAVIGATOR a executar em nome do agente designado
        (`self.delegation_registry`). Sem isso, a missão NUNCA executa
        — vai para `BLOCKED` com o motivo exato registrado, e
        `InvalidExecutorError` é levantada (nunca silenciosamente
        ignorada). Isso fecha o bug onde `agent='FORGE'` podia ser
        executado pelo NAVIGATOR e terminar `VERIFIED`.

        Transiciona a missão: RUNNING -> COMPLETED -> VERIFIED (se
        todas as subtarefas foram genuinamente bem-sucedidas) ou
        RUNNING -> FAILED (caso contrário) — nunca declara VERIFIED
        sem evidência real do NAVIGATOR."""
        from core.coordination.delegation import InvalidExecutorError, resolve_authorized_executor
        from core.reasoning.adapters import NavigatorResultVerifier, NavigatorTaskExecutor
        from core.reasoning.engine import FixedTaskPlanner, ReasoningEngine

        mission = self.missions.get(mission_id, task_id)
        executing_agent = "NAVIGATOR"

        try:
            executor_kind = resolve_authorized_executor(
                designated_agent=mission.agent, executing_agent=executing_agent, mission_id=mission_id,
                task_id=task_id, delegation_registry=self.delegation_registry,
            )
        except InvalidExecutorError as exc:
            if mission.status not in (MissionStatus.VERIFIED, MissionStatus.FAILED, MissionStatus.CANCELLED):
                self.missions.transition(mission_id, task_id, status=MissionStatus.BLOCKED, error=str(exc))
            self.facade.audit.record(
                actor=self.principal.subject, action="atlas_invalid_executor_blocked",
                resource=f"{mission_id}:{task_id}", outcome="ERROR",
                details={"designated_agent": mission.agent, "attempted_executor": executing_agent, "reason": str(exc)},
            )
            raise

        self.missions.start_running(
            mission_id, task_id, agent_principal=self.navigator_principal,
            requested_tools=mission.tools_allowed or None,
        )

        executor_adapter = NavigatorTaskExecutor(
            self.navigator, mission_id=mission_id, task_id=task_id, channel_id=channel_id,
            tools_allowed=mission.tools_allowed or None,
        )
        engine = ReasoningEngine(
            planner=FixedTaskPlanner(navigator_tasks), executor=executor_adapter, verifier=NavigatorResultVerifier(),
        )
        trace, results = engine.run(
            agent=self.principal.subject, objective=mission.objective, context=mission.context,
            available_tools=mission.tools_allowed,
        )

        all_verified = bool(results) and all(r["verified"] for r in results)
        self.missions.transition(mission_id, task_id, status=MissionStatus.COMPLETED, result={"reasoning_results": results})
        if all_verified:
            self.missions.verify(
                mission_id, task_id,
                evidence={"trace_id": trace.trace_id, "success_criteria_met": True, "results": results},
            )
        else:
            simulated = [r for r in results
                         if ((r.get("result") or {}).get("evidence") or {}).get("mode") == "MOCK"]
            self.missions.transition(
                mission_id, task_id, status=MissionStatus.FAILED,
                error=("SIMULATED_NOT_VERIFIED: subtarefa(s) executada(s) só em MOCK_FALLBACK — nenhuma "
                       "execução externa real ocorreu; simulação nunca é VERIFIED")
                if simulated else "ciclo de raciocínio não verificou todas as subtarefas (ver trace/results)",
            )

        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_run_mission_with_reasoning",
            resource=f"{mission_id}:{task_id}", outcome="SUCCESS",
            details={
                "trace_id": trace.trace_id, "all_verified": all_verified, "tasks": len(results),
                "designated_agent": mission.agent, "executing_agent": executing_agent, "executor_kind": executor_kind,
            },
        )
        return trace, results

    def delegate_task(
        self, *, mission_id: str, task_id: str, delegated_agent: str, reason: str,
        permissions: list | None = None, capabilities: list | None = None, task_scope: str = "",
        evidence: dict | None = None,
    ):
        """CORREÇÃO (item 1): único mecanismo EXPLÍCITO e auditável
        para permitir que um agente diferente do `mission.agent`
        designado execute uma missão/tarefa. Sempre registra
        `original_agent`/`delegated_agent`/`reason`/`permissions`/
        `capabilities`/`task_scope`/`evidence` — nunca uma delegação
        silenciosa. O agente ORIGINAL continua responsável pela
        coordenação (a missão permanece com `mission.agent` inalterado
        — só a AUTORIZAÇÃO de execução muda)."""
        mission = self.missions.get(mission_id, task_id)
        record = self.delegation_registry.delegate(
            mission_id=mission_id, task_id=task_id, original_agent=mission.agent, delegated_agent=delegated_agent,
            reason=reason, permissions=permissions, capabilities=capabilities, task_scope=task_scope,
            evidence=evidence,
        )
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_delegate_task", resource=f"{mission_id}:{task_id}",
            outcome="SUCCESS",
            details={
                "original_agent": record.original_agent, "delegated_agent": record.delegated_agent,
                "reason": record.reason, "task_scope": record.task_scope,
            },
        )
        return record

    # ------------------------------------------------------------------
    # EXPANSÃO — item 15/16: ATLAS + RADAR WEB COMERCIAL
    # ------------------------------------------------------------------

    def consult_radar_web(
        self, *, question_context: dict, niche: str | None = None, mechanisms: set | None = None,
    ) -> dict:
        """ATLAS pergunta ao RADAR WEB COMERCIAL — mas NUNCA aceita a
        resposta automaticamente (item 15). Devolve os candidatos com
        evidence/confidence explícitos para que ATLAS (ou o Owner)
        avalie considerando contexto, custo, risco, nicho, canal e
        capacidade operacional — a decisão de adotar continua exigindo
        o pipeline `adoption_pipeline` (item 17), nunca automática."""
        discovered = self.radar_web.discover(niche=niche, mechanisms=mechanisms)
        classified = [self.radar_web.classify_maturity(s) for s in discovered]
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_consult_radar_web", resource=niche or "GLOBAL",
            outcome="SUCCESS", details={"question_context_keys": list(question_context.keys()), "found": len(classified)},
        )
        return {
            "candidates": classified, "question_context": question_context,
            "note": "ATLAS não aceita automaticamente — avaliar evidence/confidence/contexto/custo/risco antes de qualquer decisão",
        }

    def run_annual_strategy_review(self, *, year: int, niche: str | None = None) -> dict:
        """Item 12 — revisão estratégica anual obrigatória do RADAR WEB
        COMERCIAL, delegada ao `AnnualReviewEngine` (nunca apaga
        histórico anterior)."""
        result = self.annual_review.run_annual_review(year=year, niche=niche)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_annual_strategy_review", resource=str(year),
            outcome="SUCCESS", details={"discovered": result["discovered"], "entries_recorded": result["entries_recorded"]},
        )
        return result

    def promote_strategy_to_library(
        self, *, adoption_record_id: str, title: str, content: str, area: str = "commercial_strategy",
        tags: list | None = None,
    ):
        """Item 14 — fluxo DISCOVERY->EVIDENCE->ANALYSIS->VALIDATION->
        SENTINEL/QUALITY GATE->LIBRARY CANDIDATE->APPROVED KNOWLEDGE->
        VERSIONED HISTORY. Só promove conhecimento vindo de um
        `AdoptionRecord` já `ADOPTED` (testado antes de virar padrão —
        item 17); nunca sobrescreve conhecimento existente
        (`MasterLibrary.add`/`propose_revision` já garante
        versionamento)."""
        from radar_web.adoption import AdoptionStatus

        record = self.adoption_pipeline.get(adoption_record_id)
        if record.status != AdoptionStatus.ADOPTED:
            raise ValueError(
                f"só é possível promover à Biblioteca Mestre um registro ADOPTED — "
                f"estado atual: '{record.status.value}'"
            )
        entry = self.library.add(
            title=title, area=area, content=content, origin="radar_web_comercial",
            responsible_agent=self.principal.subject, tags=tags or [],
        )
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_promote_strategy_to_library",
            resource=entry.entry_id, outcome="SUCCESS", details={"adoption_record_id": adoption_record_id},
        )
        return entry

    # ------------------------------------------------------------------
    # CORREÇÃO (item 5 — identidade canônica dos agentes)
    # ------------------------------------------------------------------

    def resolve_agent_alias(self, name_or_alias: str) -> str:
        """Resolve `agent_id`, nome oficial, ou qualquer alias
        registrado (ex: 'navigator', 'navigator@system') para o MESMO
        `agent_id` CANÔNICO ('NAVIGATOR'). Sempre registrado em
        auditoria. Alias inválido levanta `UnknownAgentAlias`
        explicitamente — nunca falha silenciosamente nem adivinha."""
        try:
            canonical = self.agent_registry.resolve_canonical_id(name_or_alias)
        except UnknownAgentAlias as exc:
            self.facade.audit.record(
                actor=self.principal.subject, action="atlas_resolve_agent_alias", resource=str(name_or_alias),
                outcome="ERROR", details={"error": str(exc)},
            )
            raise
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_resolve_agent_alias", resource=str(name_or_alias),
            outcome="SUCCESS", details={"canonical_id": canonical},
        )
        return canonical

    # ------------------------------------------------------------------
    # EXPANSÃO — item 4/23: ALL-ROUNDER + colaboração multiagente
    # ------------------------------------------------------------------

    def request_all_rounder_assistance(self, *, from_agent: str, to_agent: str, reason: str) -> bool:
        """Regras do item 4: o agente atua primeiro no seu domínio;
        colabora fora dele SÓ quando `to_agent` genuinamente possui
        `Capability.ALL_ROUNDER` registrada e usável — nunca ultrapassa
        permissões, nunca assume autoridade que não possui (ações
        críticas continuam exigindo Owner Approval, inalterado).

        CORREÇÃO (item 5): `to_agent` aceita agent_id, nome oficial ou
        qualquer alias válido ('NAVIGATOR', 'navigator',
        'navigator@system' resolvem para o mesmo agente) — resolvido
        via `resolve_agent_alias` (auditado). Alias desconhecido nunca
        é tratado como "não usável" silenciosamente — a exceção
        propaga."""
        canonical_to_agent = self.resolve_agent_alias(to_agent)
        usable = self.capability_registry.is_available(agent=canonical_to_agent, capability=Capability.ALL_ROUNDER)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_request_all_rounder_assistance",
            resource=canonical_to_agent, outcome="SUCCESS" if usable else "ERROR",
            details={"from_agent": from_agent, "to_agent": canonical_to_agent, "reason": reason, "granted": usable},
        )
        return usable

    # ------------------------------------------------------------------
    # CORREÇÃO (item 9 — ALL-ROUNDER: resolução de conflito) +
    # (item 10 — ATLAS: orquestração corrigida) +
    # (item 13 — resolve_agent_for_task corrigido)
    # ------------------------------------------------------------------

    def resolve_agent_for_task(
        self, *, task_type: str, required_capability: Capability | None = None,
        preferred_agent: str | None = None,
    ) -> dict:
        """CORREÇÃO (item 10): ATLAS consulta `agent_registry` +
        `capability_registry` (saúde, especialização, disponibilidade,
        permissões, autoridade) ANTES de delegar — nunca seleciona um
        agente apenas por nome ou fallback genérico.

        CORREÇÃO (item 9): resolve conflito quando mais de um agente
        PODERIA executar a mesma tarefa, seguindo a prioridade exigida:

            1. especialista do domínio (`preferred_agent`, quando
               genuinamente capaz — `can_handle(task_type)` E
               capacidade usável);
            2. especialista secundário compatível (outro agente cujo
               contrato também sabe lidar com `task_type`);
            3. ALL-ROUNDER autorizado (capacidade `ALL_ROUNDER` usável,
               mesmo fora do domínio principal);
            4. escalonamento (nenhum agente capaz — `selected_agent`
               vem `None`, `tier` vem `"ESCALATION"`).

        Devolve um dict com `selected_agent`, `tier`, e `rationale`
        (justificativa operacional RESUMIDA e auditável — nunca
        chain-of-thought privado) — sempre registrado em auditoria."""

        def agent_is_operational(agent_id: str) -> bool:
            try:
                registration = self.agent_registry.get(agent_id)
            except (KeyError, UnknownAgentAlias):
                return False
            return registration.implementation_status in (ImplementationStatus.ACTIVE, ImplementationStatus.PARTIAL)

        def agent_can_handle_as_primary(agent_id: str) -> bool:
            """Usado nos tiers 1/2 — domínio PRINCIPAL apenas."""
            try:
                registration = self.agent_registry.get(agent_id)
            except (KeyError, UnknownAgentAlias):
                return False
            if not registration.contract.can_handle_as_primary(task_type=task_type):
                return False
            if required_capability is not None and not self.capability_registry.is_available(
                agent=registration.agent_id, capability=required_capability,
            ):
                return False
            return agent_is_operational(registration.agent_id)

        def agent_can_handle_as_all_rounder(agent_id: str) -> bool:
            """CORREÇÃO (item 13): usado SOMENTE no tier 3 — exige
            `can_handle(task_type)` (domínio principal OU escopo
            all-rounder do próprio contrato) E `Capability.ALL_ROUNDER`
            usável E, quando especificado, `required_capability`
            também usável. Uma tarefa que NINGUÉM — nem como
            especialista, nem como all-rounder — sabe genuinamente
            tratar NUNCA cai automaticamente em quem só tem
            `all_rounder_runtime_available=True` (nunca escolhido só
            por essa flag)."""
            try:
                registration = self.agent_registry.get(agent_id)
            except (KeyError, UnknownAgentAlias):
                return False
            if not registration.contract.can_handle(task_type=task_type):
                return False
            if not self.capability_registry.is_available(agent=registration.agent_id, capability=Capability.ALL_ROUNDER):
                return False
            if required_capability is not None and not self.capability_registry.is_available(
                agent=registration.agent_id, capability=required_capability,
            ):
                return False
            return agent_is_operational(registration.agent_id)

        rationale_parts = []
        selected_agent = None
        tier = "ESCALATION"

        # 1. especialista do domínio (preferred_agent)
        if preferred_agent is not None and agent_can_handle_as_primary(preferred_agent):
            selected_agent = self.agent_registry.resolve_canonical_id(preferred_agent)
            tier = "DOMAIN_SPECIALIST"
            rationale_parts.append(f"'{selected_agent}' é o especialista do domínio e está apto para '{task_type}'")
        else:
            if preferred_agent is not None:
                rationale_parts.append(f"'{preferred_agent}' não está apto para '{task_type}' — buscando alternativa")

            preferred_canonical = None
            if preferred_agent is not None:
                try:
                    preferred_canonical = self.agent_registry.resolve_canonical_id(preferred_agent)
                except UnknownAgentAlias:
                    preferred_canonical = None

            # 2. especialista secundário compatível (domínio principal, agente diferente do preferido)
            if selected_agent is None:
                for registration in self.agent_registry.all():
                    if registration.agent_id == preferred_canonical:
                        continue
                    if agent_can_handle_as_primary(registration.agent_id):
                        selected_agent = registration.agent_id
                        tier = "SECONDARY_SPECIALIST"
                        rationale_parts.append(f"'{registration.agent_id}' é especialista secundário compatível com '{task_type}'")
                        break

            # 3. ALL-ROUNDER autorizado — exige can_handle() genuíno,
            # nunca escolhido só por all_rounder=True
            if selected_agent is None:
                for registration in self.agent_registry.all():
                    if agent_can_handle_as_all_rounder(registration.agent_id):
                        selected_agent = registration.agent_id
                        tier = "ALL_ROUNDER"
                        rationale_parts.append(
                            f"'{registration.agent_id}' assumiu via ALL-ROUNDER (apto para '{task_type}' fora do domínio principal)"
                        )
                        break

        # 4. escalonamento
        if selected_agent is None:
            rationale_parts.append(f"nenhum agente apto (especialista ou ALL-ROUNDER genuíno) para '{task_type}' — escalando")

        result = {"selected_agent": selected_agent, "tier": tier, "rationale": "; ".join(rationale_parts)}
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_resolve_agent_for_task", resource=task_type,
            outcome="SUCCESS" if selected_agent else "ESCALATED",
            details={"selected_agent": selected_agent, "tier": tier, "rationale": result["rationale"]},
        )
        return result

    # ------------------------------------------------------------------
    # COMANDO 2/6 — Gêmeo Digital, decisão por evidência e consulta

    def execute_agent_for_task(
        self,
        *,
        task_type: str,
        payload: dict,
        required_capability: Capability | None = None,
        preferred_agent: str | None = None,
    ):
        """
        Resolve pelo mecanismo canonico do ATLAS e executa exatamente
        uma instancia runtime correspondente ao agent_id selecionado.

        Fail-closed:
        - nenhum agente resolvido => bloqueia;
        - nenhuma instancia => bloqueia;
        - mais de uma instancia => bloqueia;
        - identidade divergente => bloqueia;
        - sempre usa agent.execute(), nunca execute_real() diretamente.
        """
        resolution = self.resolve_agent_for_task(
            task_type=task_type,
            required_capability=required_capability,
            preferred_agent=preferred_agent,
        )

        selected = resolution.get("selected_agent")

        if not selected:
            self.facade.audit.record(
                actor=self.principal.subject,
                action="atlas_execute_agent_for_task",
                resource=task_type,
                outcome="ESCALATED",
                details={
                    "reason": "NO_AGENT_RESOLVED",
                    "resolution": resolution,
                },
            )
            raise RuntimeError(
                f"ATLAS_AGENT_EXECUTION_BLOCKED: "
                f"nenhum agente resolvido para {task_type!r}"
            )

        selected_id = str(selected).strip().upper()

        matches = []

        for attr_name, candidate in vars(self).items():
            candidate_id = getattr(candidate, "agent_id", None)
            executor = getattr(candidate, "execute", None)

            if candidate_id is None or not callable(executor):
                continue

            if str(candidate_id).strip().upper() == selected_id:
                matches.append((attr_name, candidate))

        if len(matches) != 1:
            self.facade.audit.record(
                actor=self.principal.subject,
                action="atlas_execute_agent_for_task",
                resource=task_type,
                outcome="ESCALATED",
                details={
                    "reason": "RUNTIME_INSTANCE_CARDINALITY_ERROR",
                    "selected_agent": selected_id,
                    "runtime_matches": [name for name, _ in matches],
                    "match_count": len(matches),
                },
            )
            raise RuntimeError(
                "ATLAS_AGENT_EXECUTION_BLOCKED: "
                f"agent_id={selected_id!r} possui {len(matches)} "
                "instancias runtime executaveis; esperado exatamente 1"
            )

        attr_name, agent = matches[0]

        runtime_id = str(
            getattr(agent, "agent_id", "")
        ).strip().upper()

        if runtime_id != selected_id:
            raise RuntimeError(
                "ATLAS_AGENT_EXECUTION_BLOCKED: "
                "identidade runtime divergente"
            )

        result = agent.execute(
            task_type=task_type,
            payload=payload,
        )

        status_obj = getattr(result, "status", None)
        status_text = str(
            getattr(status_obj, "value", status_obj) or "UNKNOWN"
        ).upper()

        self.facade.audit.record(
            actor=self.principal.subject,
            action="atlas_execute_agent_for_task",
            resource=task_type,
            outcome=(
                "SUCCESS"
                if status_text in {"SUCCESS", "COMPLETED"}
                else "ESCALATED"
            ),
            details={
                "selected_agent": selected_id,
                "runtime_attribute": attr_name,
                "agent_status": status_text,
                "tier": resolution.get("tier"),
            },
        )

        return result

    # ------------------------------------------------------------------
    def sync_digital_twin(self) -> dict:
        counts = self.twin.sync_from_operational_store(recorded_by=self.principal.subject)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_twin_sync", resource="digital_twin",
            outcome="SUCCESS", details=counts,
        )
        return counts

    def twin_state(self, entity_type: str, entity_id: str, attribute: str):
        return self.twin.reading(entity_type, entity_id, attribute)

    def decide_with_evidence(
        self, *, topic: str, scope: str, requirements: list, chosen: str, justification: str,
        alternatives: list | None = None,
    ):
        """Decisão CRÍTICA automática: só é registrada como DECIDED se
        toda evidência factual exigida estiver CURRENT; caso contrário
        fica BLOCKED, com os motivos — nunca executada."""
        assessment = self.evidence_gate.assess(requirements)
        if not assessment.allowed:
            record = self.decisions.record(
                topic=topic, scope=scope, status=DecisionStatus.BLOCKED, chosen=None, actor=self.principal.subject,
                alternatives=list(alternatives or []) + [chosen], evidence=assessment.reasons(),
                justification=f"bloqueada por evidência insuficiente (pretendido: {chosen})",
                uncertainty="; ".join(assessment.reasons()),
            )
        else:
            record = self.decisions.record(
                topic=topic, scope=scope, status=DecisionStatus.DECIDED, chosen=chosen, actor=self.principal.subject,
                alternatives=list(alternatives or []), evidence=assessment.fact_ids(), justification=justification,
            )
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_decision", resource=f"{scope}/{topic}",
            outcome="SUCCESS" if record.status == DecisionStatus.DECIDED else "BLOCKED",
            details={"decision_id": record.decision_id, "status": record.status.value},
        )
        return record

    def compare_alternatives(self, *, topic: str, scope: str, metric: str, alternatives: list,
                             higher_is_better: bool = True):
        """Compara alternativas SÓ pelo que é fato vigente. Alternativas
        sem evidência ficam marcadas e fora do ranking; sem nenhuma
        alternativa evidenciada, o ATLAS se abstém."""
        evaluated, insufficient = [], []
        for entity_type, entity_id in alternatives:
            rd = self.twin.reading(entity_type, entity_id, metric)
            if rd.usable_for_decision:
                evaluated.append((f"{entity_type}:{entity_id}", rd.fact_value, rd.facts[0].fact_id))
            else:
                insufficient.append(f"{entity_type}:{entity_id}={rd.status.value}")
        evaluated.sort(key=lambda t: t[1], reverse=higher_is_better)
        summary = [f"{n}={v}" for n, v, _ in evaluated] + insufficient
        if not evaluated:
            return self.decisions.record(
                topic=topic, scope=scope, status=DecisionStatus.ABSTAINED, actor=self.principal.subject,
                alternatives=summary, evidence=insufficient,
                justification=f"nenhuma alternativa com {metric} factual vigente — não há base para escolher",
                uncertainty="todas as alternativas sem evidência",
            )
        return self.decisions.record(
            topic=topic, scope=scope, status=DecisionStatus.DECIDED, chosen=evaluated[0][0],
            actor=self.principal.subject, alternatives=summary, evidence=[fid for _, _, fid in evaluated],
            justification=f"maior {metric} factual vigente entre as alternativas evidenciadas"
            if higher_is_better else f"menor {metric} factual vigente entre as alternativas evidenciadas",
            uncertainty=("alternativas sem evidência não comparadas: " + ", ".join(insufficient)) if insufficient
            else "comparação de fatos observados — não implica causalidade",
        )

    def assess_owner_proposal(self, *, strategy_id: str, proposal: str = "SCALE"):
        """Discordância fundamentada: avalia a proposta de escalar uma
        estratégia contra o que a realidade mediu."""
        s = self.adaptive_cycle.get(strategy_id)
        scope = s.scope_key if s else "unknown"
        if s is None:
            return self.decisions.record(
                topic=f"proposal:{proposal}", scope=scope, status=DecisionStatus.ABSTAINED,
                actor=self.principal.subject, justification="estratégia desconhecida — sem histórico para avaliar",
                uncertainty="nenhum dado", chosen=None,
            )
        evaluation = s.last_evaluation or "{}"
        if s.status == StrategyStatus.ABANDONED:
            status, why = DecisionStatus.DISAGREED, "a realidade REFUTOU esta estratégia"
        elif s.causal_status == CausalStatus.CORRELATIONAL_ONLY:
            status, why = DecisionStatus.DISAGREED, "só há correlação — escalar exige teste com grupo de controle"
        elif s.causal_status == CausalStatus.CAUSAL_SUPPORTED:
            status, why = DecisionStatus.DECIDED, "efeito validado com controle"
        else:
            status, why = DecisionStatus.ABSTAINED, "amostra real ainda insuficiente para escalar"
        return self.decisions.record(
            topic=f"proposal:{proposal}", scope=scope, status=status, actor=self.principal.subject,
            chosen=proposal if status == DecisionStatus.DECIDED else None,
            justification=f"{why} (avaliação: {evaluation})", evidence=[s.hypothesis_fact_id],
            uncertainty="" if status == DecisionStatus.DECIDED else why,
        )

    def consultation(self) -> "AtlasConsultation":
        return AtlasConsultation(self)

    # ------------------------------------------------------------------
    # COMANDO 4/6 — RADAR WEB COMERCIAL: observação real de página →
    # sinais comerciais rastreáveis → Gêmeo (classe preservada) → auditoria.
    # ------------------------------------------------------------------
    def radar_observe_page(self, *, url: str, entity_id: str, ttl_seconds: float = 3 * 24 * 3600.0) -> dict:
        from core.multimodal.commercial_signals import extract_commercial_signals, record_signals_in_twin
        from core.multimodal.real_webpage_analyzer import RealWebPageAnalyzer

        from core.operations import kill_switch as _ks   # v1.0.1

        _blocked = _ks.block_reason(self.db_path)
        if _blocked:
            return {"evidence": None, "signals": [], "recorded": [], "blocked": _blocked}
        evidence = RealWebPageAnalyzer().analyze(reference=url)
        signals = extract_commercial_signals(evidence)
        recorded = record_signals_in_twin(self.twin, entity_type="competition", entity_id=entity_id,
                                          signals=signals, ttl_seconds=ttl_seconds, recorded_by=self.principal.subject)
        self.facade.audit.record(
            actor=self.principal.subject, action="atlas_radar_observe_page", resource=f"competition:{entity_id}",
            outcome="SUCCESS" if evidence.confidence > 0 else "UNAVAILABLE",
            details={"url": url, "evidence_id": evidence.evidence_id, "reference_hash": evidence.reference_hash,
                     "facts": sum(1 for f in recorded if f.claim_type.value == "FACT"),
                     "non_factual": sum(1 for f in recorded if f.claim_type.value != "FACT"),
                     "unknown": sum(1 for s in signals if s.claim_type.value == "UNKNOWN")},
        )
        return {"evidence": evidence, "signals": signals, "recorded": recorded}


class AtlasConsultation:
    """Superfície de diálogo do ATLAS (COMANDO 2/6). Cada turno devolve
    uma resposta ESTRUTURADA em que toda afirmação traz a sua classe
    (FACT/INFERENCE/HYPOTHESIS/FORECAST) ou o estado explícito
    (DESCONHECIDO, VENCIDO, CONFLITO...). O contexto ativo (escopo) muda
    só por `switch_context`; decisões vêm do `DecisionLedger`
    persistente — a continuidade não depende do tamanho do diálogo nem
    de o processo ter reiniciado. A linguagem natural fica a cargo do
    modelo que opera o ATLAS; esta camada garante o CONTEÚDO."""

    def __init__(self, director: AtlasDirector) -> None:
        self.director = director
        self.context: tuple | None = None   # (entity_type, entity_id)
        self.turns: list = []

    def _answer(self, intent: str, payload: dict) -> dict:
        answer = {"turn": len(self.turns) + 1, "intent": intent,
                  "context": f"{self.context[0]}:{self.context[1]}" if self.context else None, **payload}
        self.turns.append(answer)
        return answer

    def switch_context(self, entity_type: str, entity_id: str) -> dict:
        self.context = (entity_type, entity_id)
        return self._answer("switch_context", {"statement": f"contexto ativo: {entity_type}:{entity_id}"})

    def _require_context(self) -> tuple:
        if self.context is None:
            raise ValueError("nenhum contexto ativo — use switch_context")
        return self.context

    def ask_state(self, attribute: str) -> dict:
        entity_type, entity_id = self._require_context()
        rd = self.director.twin.reading(entity_type, entity_id, attribute)
        if rd.status == ReadingStatus.CURRENT:
            statement = f"FATO OBSERVADO: {attribute} = {rd.fact_value!r}"
        elif rd.status in (ReadingStatus.LOW_CONFIDENCE, ReadingStatus.UNCONFIRMED):
            statement = f"FATO {rd.status.value}: {attribute} = {rd.fact_value!r} (não sustenta decisão)"
        elif rd.status == ReadingStatus.CONFLICT:
            statement = f"CONFLITO entre fontes para {attribute} — nenhum valor é afirmado"
        elif rd.status == ReadingStatus.EXPIRED:
            statement = f"VENCIDO: último fato de {attribute} expirou — estado atual desconhecido"
        elif rd.status == ReadingStatus.NOT_OBSERVED:
            statement = f"NÃO OBSERVADO: não há fato sobre {attribute}; só afirmações não factuais"
        else:
            statement = f"DESCONHECIDO: não há registro sobre {attribute}"
        return self._answer("ask_state", {
            "status": rd.status.value, "statement": statement, "fact_value": rd.fact_value,
            "facts": [{"value": f.value, "source": f.source_id, "ref": f.source_ref, "fact_id": f.fact_id}
                      for f in rd.facts],
            "non_factual": [{"class": f.claim_type.value, "value": f.value, "basis": f.basis} for f in rd.non_factual],
            "uncertainty": "; ".join(rd.notes),
        })

    def recall(self, topic: str) -> dict:
        entity_type, entity_id = self._require_context()
        scope = f"{entity_type}:{entity_id}"
        latest = self.director.decisions.latest(topic=topic, scope=scope)
        if latest is None:
            return self._answer("recall", {"status": "UNKNOWN",
                                           "statement": f"nenhuma decisão registrada sobre {topic} em {scope}"})
        return self._answer("recall", {
            "status": latest.status.value, "decision_id": latest.decision_id, "chosen": latest.chosen,
            "statement": latest.justification, "evidence": list(latest.evidence),
            "alternatives": list(latest.alternatives), "uncertainty": latest.uncertainty,
            "history_size": len(self.director.decisions.history(topic=topic, scope=scope)),
        })

    def why(self, decision_id: str) -> dict:
        rec = self.director.decisions.get(decision_id)
        if rec is None:
            return self._answer("why", {"status": "UNKNOWN", "statement": "decisão desconhecida"})
        return self._answer("why", {"status": rec.status.value, "statement": rec.justification,
                                    "evidence": list(rec.evidence), "uncertainty": rec.uncertainty,
                                    "alternatives": list(rec.alternatives)})

    def propose(self, strategy_id: str, proposal: str = "SCALE") -> dict:
        rec = self.director.assess_owner_proposal(strategy_id=strategy_id, proposal=proposal)
        return self._answer("propose", {"status": rec.status.value, "statement": rec.justification,
                                        "decision_id": rec.decision_id, "uncertainty": rec.uncertainty})
