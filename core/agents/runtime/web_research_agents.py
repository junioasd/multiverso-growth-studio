"""
core.agents.runtime.web_research_agents
============================================
BLOCO FINAL 1 (item 3 — INTELIGÊNCIA WEB/MERCADO) — ORION, SAGE,
VENTURE, PULSE: os 4 agentes cuja função oficial depende de pesquisa
real na web (tendências, concorrência, produtos, distribuição/SEO).

Neste ambiente, sem acesso à rede, nenhum provider real está
configurado — cada agente SEMPRE reporta `UNAVAILABLE` de forma
explícita quando chamado, nunca fabrica uma "tendência" ou
"concorrente" inventado. A arquitetura (task types, validação de
entrada, integração com o provider) é real e está pronta para receber
um provider de busca real quando a instalação tiver acesso a rede/API
configurada — ver `core.agents.runtime.providers.ExternalProvider`.
"""

from __future__ import annotations

from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.agents.runtime.http_search_provider import HttpSearchProvider
from core.agents.runtime.providers import ExternalProvider
from core.capabilities.registry import Capability


class ProviderDependentAgent(RealAgentContract):
    """Base comum — SEMPRE separa claramente `available` (dado real
    devolvido pelo provider) de `UNAVAILABLE` (provider não
    configurado). Nunca preenche o resultado com um placeholder que
    pareça dado real.

    CORREÇÃO (item 2 — remover NullProvider como implementação de
    produção): o provider PADRÃO agora é `HttpSearchProvider()` — um
    adapter REAL (`urllib.request`, stdlib) que lê credencial de
    variável de ambiente e faz uma chamada HTTP genuína quando
    configurado. Nunca mais um stub permanentemente indisponível —
    `is_configured()` reflete a presença REAL de credencial no
    ambiente, nunca hardcoded como sempre falso."""

    provider_operation: str = "UNSET"
    _default_provider_factory = HttpSearchProvider

    def __init__(self, *, provider: ExternalProvider | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.provider = provider if provider is not None else self._default_provider_factory()

    def _call_provider(self, *, task_type: str, params: dict) -> AgentTaskResult:
        if not self.provider.is_configured():
            return AgentTaskResult.unavailable(
                agent_id=self.agent_id, task_type=task_type,
                reason=(
                    f"nenhum provider real configurado para '{self.provider_operation}' — esta instalação não "
                    "tem acesso a rede/API externa configurada; zero dados fabricados"
                ),
            )
        response = self.provider.call(operation=self.provider_operation, params=params)
        if not response.available:
            return AgentTaskResult.unavailable(agent_id=self.agent_id, task_type=task_type, reason=response.reason)
        # CORREÇÃO (item 3 — separar dado real, inferência e
        # indisponibilidade): o tipo de dado (FACT/ESTIMATE/INFERENCE/
        # UNKNOWN) reportado pelo provider é sempre exposto
        # explicitamente no output — nunca escondido/assumido.
        output = dict(response.data)
        output["_data_classification"] = response.claim_type.value
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type=task_type, output=output,
            evidence_ids=[f"provider:{response.provider_name}:{response.fetched_at}"], confidence=response.confidence,
        )


class OrionAgent(ProviderDependentAgent):
    agent_id = "ORION"
    primary_task_types = frozenset({"SCAN_MARKET_OPPORTUNITIES", "MONITOR_NICHE_TRENDS"})
    all_rounder_task_types = frozenset({"SCAN_MARKET_OPPORTUNITIES"})
    provider_operation = "MARKET_RADAR"
    max_input_items = 50

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type not in self.primary_task_types and task_type not in self.all_rounder_task_types:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")
        return self._call_provider(task_type=task_type, params=payload)


class SageAgent(ProviderDependentAgent):
    agent_id = "SAGE"
    primary_task_types = frozenset({"RESEARCH_AUDIENCE", "RESEARCH_COMPETITION"})
    all_rounder_task_types = frozenset({"RESEARCH_AUDIENCE"})
    provider_operation = "AUDIENCE_RESEARCH"
    max_input_items = 50

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type not in self.primary_task_types and task_type not in self.all_rounder_task_types:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")
        return self._call_provider(task_type=task_type, params=payload)


class VentureAgent(ProviderDependentAgent):
    agent_id = "VENTURE"
    primary_task_types = frozenset({"DISCOVER_PRODUCTS", "DISCOVER_AFFILIATE_OFFERS"})
    all_rounder_task_types = frozenset({"DISCOVER_PRODUCTS"})
    provider_operation = "PRODUCT_DISCOVERY"
    max_input_items = 50

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type not in self.primary_task_types and task_type not in self.all_rounder_task_types:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")
        return self._call_provider(task_type=task_type, params=payload)


class PulseAgent(ProviderDependentAgent):
    agent_id = "PULSE"
    primary_task_types = frozenset({"CHECK_SEO_RANKING", "DISTRIBUTE_CONTENT"})
    all_rounder_task_types = frozenset({"CHECK_SEO_RANKING"})
    provider_operation = "SEO_DISTRIBUTION"
    max_input_items = 50

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type not in self.primary_task_types and task_type not in self.all_rounder_task_types:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")
        return self._call_provider(task_type=task_type, params=payload)


def register_web_research_capabilities(capability_registry, *, granted_by: str) -> None:
    """CORREÇÃO: registra as capacidades como tecnicamente suportadas
    (`available=False` — nenhum provider real conectado neste
    ambiente) — nunca `available=True` sem um provider real por trás."""
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    for agent_id, level in (
        ("ORION", CompetencyLevel.MASTER), ("SAGE", CompetencyLevel.ESPECIALISTA),
        ("VENTURE", CompetencyLevel.ESPECIALISTA), ("PULSE", CompetencyLevel.MASTER),
    ):
        capability_registry.register(
            agent=agent_id, capability=Capability.WEB_RESEARCH, level=level, granted_by=granted_by,
            provider="NONE", available=False, authorized=True, health=CapabilityHealth.UNKNOWN,
        )
