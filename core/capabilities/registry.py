"""
core.capabilities.registry
==============================
Capability Registry — registro de capacidades por agente, NUNCA
autodeclarado (feito por autoridade externa — ATLAS/Owner —, auditável
via `granted_by`).

CORREÇÃO (item 7 — health real): substitui o `bridge.hub.HealthStatus`
genérico (OK/DEGRADED/DOWN/UNKNOWN) por `CapabilityHealth`, específico
e com semântica CORRIGIDA — o bug anterior tratava `UNKNOWN` como
utilizável (só bloqueava `DOWN`); agora:

    HEALTHY   -> utilizável
    DEGRADED  -> utilizável SOMENTE com `allow_degraded=True` explícito
                 (policy/fallback do chamador, nunca padrão implícito)
    UNKNOWN   -> NUNCA plenamente operacional sem probe/validação
    UNHEALTHY -> sempre bloqueado
    DISABLED  -> sempre bloqueado

Separa 4 conceitos distintos (nunca confundidos):
    COMPETÊNCIA (nível concedido — `CompetencyLevel`)
    CAPACIDADE  (o que o agente consegue tecnicamente fazer — `Capability`)
    AUTONOMIA   (se o agente pode agir sem supervisão passo a passo)
    AUTORIDADE  (se o agente tem permissão RBAC/Owner Approval para a ação)
Alta capacidade NUNCA implica autoridade irrestrita.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Capability(str, Enum):
    REASONING = "REASONING"
    PLANNING = "PLANNING"
    AGENTIC_EXECUTION = "AGENTIC_EXECUTION"
    MULTIMODAL_TEXT = "MULTIMODAL_TEXT"
    MULTIMODAL_IMAGE = "MULTIMODAL_IMAGE"
    MULTIMODAL_AUDIO = "MULTIMODAL_AUDIO"
    MULTIMODAL_VIDEO = "MULTIMODAL_VIDEO"
    WEB_RESEARCH = "WEB_RESEARCH"
    TOOL_USE = "TOOL_USE"
    BROWSER = "BROWSER"
    CODE = "CODE"
    DATA_ANALYSIS = "DATA_ANALYSIS"
    COMMERCIAL_INTELLIGENCE = "COMMERCIAL_INTELLIGENCE"
    QUALITY_CONTROL = "QUALITY_CONTROL"
    REPAIR = "REPAIR"
    ORCHESTRATION = "ORCHESTRATION"
    ALL_ROUNDER = "ALL_ROUNDER"


class CompetencyLevel(str, Enum):
    JUNIOR = "JUNIOR"
    PLENO = "PLENO"
    SENIOR = "SENIOR"
    ESPECIALISTA = "ESPECIALISTA"
    MASTER = "MASTER"


class CapabilityHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"
    UNHEALTHY = "UNHEALTHY"
    DISABLED = "DISABLED"


class EffectiveCapabilityStatus(str, Enum):
    """CORREÇÃO (item 8 — multimodal: capacidade real vs declarada).
    Status DERIVADO (nunca setado diretamente) — distingue uma
    capacidade genuinamente `ACTIVE` (provider real + saudável) de uma
    que só existe em `MOCK` (nunca apresentada como análise real)."""
    ACTIVE = "ACTIVE"
    MOCK = "MOCK"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class CapabilityRegistration:
    agent: str
    capability: Capability
    level: CompetencyLevel
    granted_by: str
    provider: str = "MOCK"
    available: bool = False
    authorized: bool = False
    health: CapabilityHealth = CapabilityHealth.UNKNOWN
    last_checked: float | None = None
    last_success: float | None = None
    failure_reason: str | None = None
    latency_ms: float | None = None
    last_verified: float = field(default_factory=time.time)

    def is_usable(self, *, allow_degraded: bool = False) -> bool:
        """CORREÇÃO (item 7): nunca usável sem `available`+`authorized`.
        Daí em diante, a saúde decide: HEALTHY sempre usável; DEGRADED
        só com `allow_degraded=True` explícito; UNKNOWN/UNHEALTHY/
        DISABLED NUNCA usável."""
        if not (self.available and self.authorized):
            return False
        if self.health == CapabilityHealth.HEALTHY:
            return True
        if self.health == CapabilityHealth.DEGRADED:
            return allow_degraded
        return False  # UNKNOWN, UNHEALTHY, DISABLED

    @property
    def effective_status(self) -> EffectiveCapabilityStatus:
        """CORREÇÃO (item 8): status DERIVADO — nunca declara ACTIVE
        quando o provider é MOCK, mesmo se `available=True`."""
        if not (self.available and self.authorized):
            return EffectiveCapabilityStatus.UNAVAILABLE
        if self.provider == "MOCK":
            return EffectiveCapabilityStatus.MOCK
        if self.health == CapabilityHealth.HEALTHY:
            return EffectiveCapabilityStatus.ACTIVE
        if self.health == CapabilityHealth.DEGRADED:
            return EffectiveCapabilityStatus.PARTIAL
        return EffectiveCapabilityStatus.UNAVAILABLE


class CapabilityRegistry:
    def __init__(self) -> None:
        self._registrations: dict = {}

    def register(
        self, *, agent: str, capability: Capability, level: CompetencyLevel, granted_by: str,
        provider: str = "MOCK", available: bool = False, authorized: bool = False,
        health: CapabilityHealth = CapabilityHealth.UNKNOWN,
    ) -> CapabilityRegistration:
        reg = CapabilityRegistration(
            agent=agent, capability=capability, level=level, granted_by=granted_by, provider=provider,
            available=available, authorized=authorized, health=health,
        )
        self._registrations[(agent, capability)] = reg
        return reg

    def revoke(self, *, agent: str, capability: Capability) -> None:
        self._registrations.pop((agent, capability), None)

    def get(self, *, agent: str, capability: Capability) -> CapabilityRegistration | None:
        return self._registrations.get((agent, capability))

    def is_available(self, *, agent: str, capability: Capability, allow_degraded: bool = False) -> bool:
        reg = self.get(agent=agent, capability=capability)
        return reg is not None and reg.is_usable(allow_degraded=allow_degraded)

    def capabilities_for(self, agent: str) -> list:
        return [r for (a, _c), r in self._registrations.items() if a == agent]

    def agents_with_capability(self, capability: Capability, *, only_usable: bool = True) -> list:
        results = [r for (_a, c), r in self._registrations.items() if c == capability]
        if only_usable:
            results = [r for r in results if r.is_usable()]
        return results

    def all(self) -> list:
        return list(self._registrations.values())

    # ------------------------------------------------------------------
    # CORREÇÃO (item 7): health probe/update explícito
    # ------------------------------------------------------------------

    def record_health_check(
        self, *, agent: str, capability: Capability, health: CapabilityHealth, success: bool,
        failure_reason: str | None = None, latency_ms: float | None = None,
    ) -> CapabilityRegistration:
        """Único caminho para MUDAR a saúde de uma capability — nunca
        setada "otimisticamente" no registro inicial sem uma checagem
        real. Registra `last_checked` sempre; `last_success` só quando
        `success=True`."""
        reg = self.get(agent=agent, capability=capability)
        if reg is None:
            raise KeyError(f"capability não registrada: ({agent}, {capability.value})")
        reg.health = health
        reg.last_checked = time.time()
        reg.latency_ms = latency_ms
        if success:
            reg.last_success = time.time()
            reg.failure_reason = None
        else:
            reg.failure_reason = failure_reason
        return reg
