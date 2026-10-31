"""
core.agents.base
====================
Base transversal REAL e honesta para os 25 agentes principais.

CORREÇÃO (microcorreção — itens 3, 4, 5): separa explicitamente:

    agentic_architecture_supported  — TODOS os 25 suportam o contrato
                                       comum (goal understanding,
                                       planning, decomposition,
                                       delegation, tool selection,
                                       verification, critique, replan,
                                       escalation, evidence,
                                       collaboration) — arquitetura,
                                       não implementação.
    agentic_runtime_available       — se HÁ, agora, um executor real
                                       rodando esse contrato (só
                                       ATLAS/NAVIGATOR/FORGE hoje).

    all_rounder_capable             — TODOS podem carregar a
                                       capacidade arquitetural
                                       transversal ALL-ROUNDER.
    all_rounder_runtime_available   — se essa capacidade está
                                       REGISTRADA como usável agora no
                                       Capability Registry (nunca
                                       finge runtime inexistente).

`aliases` — nomes/identificadores alternativos que devem resolver
para o MESMO agente canônico (ex: 'navigator', 'navigator@system' ->
'NAVIGATOR') — ver `AgentRegistry.resolve_canonical_id()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from core.capabilities.registry import Capability, CapabilityHealth, CompetencyLevel
from core.multimodal.evidence import Modality


class ImplementationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PARTIAL = "PARTIAL"
    SCAFFOLD = "SCAFFOLD"
    UNAVAILABLE = "UNAVAILABLE"


class AgentNotImplemented(Exception):
    """Levantado por um agente SCAFFOLD/UNAVAILABLE quando algo tenta
    genuinamente executar através dele — nunca finge execução real."""


class UnknownAgentAlias(Exception):
    """Levantado quando um nome/alias não resolve para nenhum agente
    canônico registrado — nunca silenciosamente ignorado ou
    adivinhado."""


class BaseAgentContract(Protocol):
    def can_handle(self, *, task_type: str) -> bool: ...
    def can_handle_as_primary(self, *, task_type: str) -> bool: ...


@dataclass
class ScaffoldAgent:
    """Implementação padrão para agentes ainda não implementados de
    verdade — satisfaz `BaseAgentContract` honestamente: `can_handle()`
    sempre `False`, qualquer tentativa de execução real levanta
    `AgentNotImplemented` explicitamente (nunca um resultado
    inventado)."""

    agent_id: str

    def can_handle(self, *, task_type: str) -> bool:
        return False

    def can_handle_as_primary(self, *, task_type: str) -> bool:
        return False

    def execute(self, *args, **kwargs):
        raise AgentNotImplemented(
            f"agente '{self.agent_id}' está em SCAFFOLD nesta entrega — nenhuma execução real disponível"
        )


@dataclass
class MultimodalCapabilityEntry:
    """Item 6/7/8 — registro POR modalidade de um agente. Distingue
    `supported` (arquitetura reconhece a modalidade) de `available`
    (provider real funcional). `status` NUNCA é `ACTIVE` sem
    `available=True` e `provider` diferente de `MOCK`."""

    modality: Modality
    supported: bool = True
    available: bool = False
    provider: str = "MOCK"
    health: CapabilityHealth = CapabilityHealth.UNKNOWN
    authorized: bool = False

    @property
    def status(self) -> str:
        if not self.supported:
            return "UNSUPPORTED"
        if not self.available or not self.authorized:
            return "UNAVAILABLE"
        if self.provider == "MOCK":
            return "MOCK"
        if self.health == CapabilityHealth.HEALTHY:
            return "ACTIVE"
        if self.health == CapabilityHealth.DEGRADED:
            return "PARTIAL"
        return "UNAVAILABLE"


@dataclass
class AgentRegistration:
    agent_id: str
    display_name: str
    role: str
    specialization: str
    level: CompetencyLevel
    capabilities: list = field(default_factory=list)
    multimodal_capabilities: list = field(default_factory=list)  # list[MultimodalCapabilityEntry]
    all_rounder_capable: bool = True
    all_rounder_runtime_available: bool = False
    agentic_architecture_supported: bool = True
    agentic_runtime_available: bool = False
    available_tools: list = field(default_factory=list)
    permissions: list = field(default_factory=list)
    authority_level: str = "NONE"
    health: CapabilityHealth = CapabilityHealth.UNKNOWN
    provider: str = "internal"
    implementation_status: ImplementationStatus = ImplementationStatus.SCAFFOLD
    notes: str = ""
    aliases: list = field(default_factory=list)
    contract: object = None  # BaseAgentContract — None só é aceitável para SCAFFOLD puro

    # ---- compatibilidade com nomes antigos (não remove, apenas espelha) ----
    @property
    def name(self) -> str:
        return self.display_name

    @property
    def official_role(self) -> str:
        """CORREÇÃO (item 11 — implementation_status nunca muda o
        papel oficial do agente): alias EXPLÍCITO para `specialization`
        — a responsabilidade oficial acordada, que NUNCA muda por
        causa de `implementation_status` (SCAFFOLD/PARTIAL/ACTIVE) ou
        de `runtime_capabilities` (o que está de fato disponível
        agora). Os três eixos são independentes."""
        return self.specialization

    @property
    def runtime_capabilities(self) -> list:
        """O que está genuinamente disponível AGORA (nunca confundido
        com `official_role`) — capacidades cujo runtime está de fato
        implementado, hoje só para ACTIVE/PARTIAL."""
        if self.implementation_status in (ImplementationStatus.ACTIVE, ImplementationStatus.PARTIAL):
            return list(self.capabilities)
        return []

    @property
    def all_rounder(self) -> bool:
        return self.all_rounder_runtime_available

    @property
    def agentic_enabled(self) -> bool:
        return self.agentic_runtime_available


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict = {}
        self._alias_index: dict = {}

    def register(self, registration: AgentRegistration) -> AgentRegistration:
        if registration.implementation_status == ImplementationStatus.ACTIVE and registration.contract is None:
            raise ValueError(
                f"agente '{registration.agent_id}' declarado ACTIVE sem `contract` real — "
                "nunca declarar plenamente operacional sem implementação"
            )
        if registration.contract is None:
            registration.contract = ScaffoldAgent(agent_id=registration.agent_id)
        self._agents[registration.agent_id] = registration

        # CORREÇÃO (item 5): índice de alias — o próprio agent_id
        # sempre resolve para si mesmo (case-insensitive), mais
        # qualquer alias explicitamente declarado.
        self._alias_index[registration.agent_id.strip().upper()] = registration.agent_id
        for alias in registration.aliases:
            self._alias_index[alias.strip().upper()] = registration.agent_id
        return registration

    def resolve_canonical_id(self, name_or_alias: str) -> str:
        """CORREÇÃO (item 5): resolve `agent_id`, nome oficial ou
        qualquer alias registrado para o MESMO `agent_id` canônico.
        Nunca adivinha — alias desconhecido levanta
        `UnknownAgentAlias` explicitamente."""
        key = (name_or_alias or "").strip().upper()
        if key in self._alias_index:
            return self._alias_index[key]
        raise UnknownAgentAlias(f"'{name_or_alias}' não corresponde a nenhum agente ou alias registrado")

    def get(self, agent_id: str) -> AgentRegistration:
        canonical = self.resolve_canonical_id(agent_id)
        return self._agents[canonical]

    def all(self) -> list:
        return list(self._agents.values())

    def by_implementation_status(self, status: ImplementationStatus) -> list:
        return [a for a in self._agents.values() if a.implementation_status == status]

    def by_specialization(self, specialization: str) -> list:
        return [a for a in self._agents.values() if a.specialization == specialization]
