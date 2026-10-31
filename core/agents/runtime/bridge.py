"""
core.agents.runtime.bridge
==============================
MICROCORREÇÃO — item 1 (25 AGENTES: eliminar PARTIAL). BRIDGE agora
tem runtime REAL — wrapper fino sobre `bridge.hub.BridgeHub` (já
real, já testado, nunca reimplementado aqui). Nunca declara um
conector disponível sem que `BridgeHub.available_connectors()`/
`best_available_execution_path()` genuinamente o confirme.
"""

from __future__ import annotations

from bridge.hub import BridgeHub, ConnectorType
from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability


class BridgeAgent(RealAgentContract):
    agent_id = "BRIDGE"
    primary_task_types = frozenset({
        "REGISTER_CONNECTOR", "LIST_CONNECTORS", "BEST_EXECUTION_PATH", "CREDENTIAL_STATUS",
    })
    all_rounder_task_types = frozenset({"LIST_CONNECTORS"})
    max_input_items = 200

    def __init__(self, *, hub: BridgeHub | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.hub = hub if hub is not None else BridgeHub()

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type == "REGISTER_CONNECTOR":
            return self._register_connector(payload)
        if task_type == "LIST_CONNECTORS":
            return self._list_connectors(payload)
        if task_type == "BEST_EXECUTION_PATH":
            return self._best_execution_path(payload)
        if task_type == "CREDENTIAL_STATUS":
            return self._credential_status(payload)
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _register_connector(self, payload: dict) -> AgentTaskResult:
        platform = payload.get("platform")
        connector_type_str = payload.get("connector_type")
        if not platform or not connector_type_str:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="REGISTER_CONNECTOR", reason="platform e connector_type são obrigatórios",
            )
        try:
            connector_type = ConnectorType(connector_type_str)
        except ValueError:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="REGISTER_CONNECTOR", reason=f"connector_type inválido: '{connector_type_str}'",
            )
        conn = self.hub.register_connector(
            platform=platform, connector_type=connector_type, is_configured=payload.get("is_configured", False),
            enabled=payload.get("enabled", True), authenticated=payload.get("authenticated", False),
            scopes=payload.get("scopes"), required_scopes=payload.get("required_scopes"),
            notes=payload.get("notes", ""),
        )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="REGISTER_CONNECTOR",
            output={"connector_id": conn.connector_id, "is_viable": conn.is_viable()},
            evidence_ids=[conn.connector_id], confidence=1.0,
        )

    def _list_connectors(self, payload: dict) -> AgentTaskResult:
        connectors = self.hub.available_connectors(platform=payload.get("platform"))
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="LIST_CONNECTORS",
            output={"connectors": [
                {"connector_id": c.connector_id, "platform": c.platform, "connector_type": c.connector_type.value,
                 "is_viable": c.is_viable()}
                for c in connectors
            ]},
            evidence_ids=[c.connector_id for c in connectors], confidence=1.0,
        )

    def _best_execution_path(self, payload: dict) -> AgentTaskResult:
        platform = payload.get("platform")
        if not platform:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="BEST_EXECUTION_PATH", reason="platform é obrigatório",
            )
        allowed = set(payload["allowed_mechanisms"]) if payload.get("allowed_mechanisms") else None
        best = self.hub.best_available_execution_path(platform=platform, allowed_mechanisms=allowed)
        if best is None:
            return AgentTaskResult.completed(
                agent_id=self.agent_id, task_type="BEST_EXECUTION_PATH",
                output={"available": False, "reason": f"nenhum conector viável para '{platform}'"}, confidence=1.0,
            )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="BEST_EXECUTION_PATH",
            output={"available": True, "connector_id": best.connector_id, "connector_type": best.connector_type.value},
            evidence_ids=[best.connector_id], confidence=1.0,
        )

    def _credential_status(self, payload: dict) -> AgentTaskResult:
        reference_id = payload.get("reference_id")
        if not reference_id:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="CREDENTIAL_STATUS", reason="reference_id é obrigatório",
            )
        try:
            status = self.hub.credential_status(reference_id)
        except KeyError:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="CREDENTIAL_STATUS", reason=f"reference_id '{reference_id}' não encontrado",
            )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="CREDENTIAL_STATUS", output={"status": status.value},
            evidence_ids=[reference_id], confidence=1.0,
        )


def register_bridge_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="BRIDGE", capability=Capability.TOOL_USE, level=CompetencyLevel.MASTER,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
