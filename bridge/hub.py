"""
bridge.hub
============
BRIDGE — MULTIVERSO Access & Execution Hub.

BRIDGE conecta. NAVIGATOR executa. ATLAS dirige. Este módulo NUNCA
executa uma ação de negócio — só gerencia a CAMADA DE CONEXÃO:
credenciais (só REFERÊNCIAS, nunca segredo em texto puro), escopos
OAuth2, renovação/revogação de token, e o REGISTRO de conectores/
plugins/MCP/webhooks disponíveis. Nenhuma chamada de API externa real
é feita nesta entrega — tudo aqui é MOCK, claramente identificado.

CORREÇÃO (item 5 — BRIDGE + NAVIGATOR, integração real):
`best_available_execution_path()` agora valida de verdade —
`is_configured`, `enabled`, `authenticated`, `scopes` (contra
`required_scopes`), `health` e `permissions_ok`. Um conector com
`is_configured=False` NUNCA é retornado como disponível. Quando
NENHUM caminho está genuinamente disponível, devolve `None` — NAVIGATOR
NUNCA finge execução nesse caso (ver `navigator/executor.py`).

O único mecanismo que É legitimamente sempre "disponível" nesta
entrega é o `MOCK_FALLBACK` — explicitamente rotulado como tal (nunca
disfarçado de mecanismo real), e sempre de MENOR prioridade (item 6 da
ordem obrigatória: "fallback autorizado").
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class ConnectorType(str, Enum):
    OFFICIAL_API = "OFFICIAL_API"
    MCP_PLUGIN = "MCP_PLUGIN"
    WEBHOOK = "WEBHOOK"
    BROWSER_AUTOMATION = "BROWSER_AUTOMATION"
    COMPUTER_USE_RPA = "COMPUTER_USE_RPA"
    LOCAL_EXECUTION = "LOCAL_EXECUTION"
    MOCK_FALLBACK = "MOCK_FALLBACK"  # item 6: "fallback autorizado" — sempre MOCK, nunca disfarçado


# Ordem obrigatória (Seção 1 do NAVIGATOR / item 5 desta correção).
EXECUTION_MECHANISM_PRIORITY = (
    ConnectorType.OFFICIAL_API, ConnectorType.MCP_PLUGIN, ConnectorType.WEBHOOK,
    ConnectorType.LOCAL_EXECUTION, ConnectorType.BROWSER_AUTOMATION, ConnectorType.COMPUTER_USE_RPA, ConnectorType.MOCK_FALLBACK,
)


class CredentialStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class HealthStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


@dataclass
class CredentialReference:
    """NUNCA guarda o segredo em si — só uma REFERÊNCIA (ex: nome da
    variável de ambiente / cofre externo onde o segredo mora de
    verdade)."""
    reference_id: str
    platform: str
    scopes: list = field(default_factory=list)
    status: CredentialStatus = CredentialStatus.NOT_CONFIGURED
    external_vault_key: str | None = None
    expires_at: float | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class ConnectorRegistration:
    connector_id: str
    platform: str
    connector_type: ConnectorType
    is_configured: bool = False
    enabled: bool = True
    authenticated: bool = False
    scopes: list = field(default_factory=list)
    required_scopes: list = field(default_factory=list)
    health: HealthStatus = HealthStatus.UNKNOWN
    permissions_ok: bool = False
    notes: str = ""

    def is_viable(self) -> bool:
        """CORREÇÃO — validação real: nunca considera disponível um
        conector `is_configured=False`/desabilitado. Mecanismos "reais"
        (todos exceto MOCK_FALLBACK) exigem também autenticação, os
        escopos exigidos, saúde OK e permissões confirmadas."""
        if not self.is_configured or not self.enabled:
            return False
        if self.connector_type == ConnectorType.MOCK_FALLBACK:
            return True  # mecanismo explicitamente MOCK — sempre viável por definição
        if not self.authenticated:
            return False
        if self.required_scopes and not set(self.required_scopes).issubset(set(self.scopes)):
            return False
        if self.health != HealthStatus.OK:
            return False
        if not self.permissions_ok:
            return False
        return True


class BridgeHub:
    def __init__(self) -> None:
        self._credentials: dict[str, CredentialReference] = {}
        self._connectors: dict[str, ConnectorRegistration] = {}

    # ------------------------------------------------------------------
    # Credenciais — só referências
    # ------------------------------------------------------------------

    def register_credential_reference(
        self, *, platform: str, scopes: list, external_vault_key: str,
    ) -> CredentialReference:
        ref = CredentialReference(
            reference_id=f"cred_{uuid.uuid4().hex[:12]}", platform=platform, scopes=list(scopes),
            status=CredentialStatus.NOT_CONFIGURED, external_vault_key=external_vault_key,
        )
        self._credentials[ref.reference_id] = ref
        return ref

    def activate_credential(self, reference_id: str, *, expires_at: float | None = None) -> CredentialReference:
        ref = self._credentials[reference_id]
        ref.status = CredentialStatus.ACTIVE
        ref.expires_at = expires_at
        return ref

    def revoke_credential(self, reference_id: str) -> CredentialReference:
        ref = self._credentials[reference_id]
        ref.status = CredentialStatus.REVOKED
        return ref

    def credential_status(self, reference_id: str) -> CredentialStatus:
        ref = self._credentials[reference_id]
        if ref.status == CredentialStatus.ACTIVE and ref.expires_at is not None and ref.expires_at < time.time():
            ref.status = CredentialStatus.EXPIRED
        return ref.status

    # ------------------------------------------------------------------
    # Conectores
    # ------------------------------------------------------------------

    def register_connector(
        self,
        *,
        platform: str,
        connector_type: ConnectorType,
        is_configured: bool = False,
        enabled: bool = True,
        authenticated: bool = False,
        scopes: list | None = None,
        required_scopes: list | None = None,
        health: HealthStatus = HealthStatus.UNKNOWN,
        permissions_ok: bool = False,
        notes: str = "",
    ) -> ConnectorRegistration:
        conn = ConnectorRegistration(
            connector_id=f"conn_{uuid.uuid4().hex[:12]}", platform=platform, connector_type=connector_type,
            is_configured=is_configured, enabled=enabled, authenticated=authenticated,
            scopes=list(scopes or []), required_scopes=list(required_scopes or []), health=health,
            permissions_ok=permissions_ok,
            notes=notes or "MOCK — nenhuma integração externa real neste lote",
        )
        self._connectors[conn.connector_id] = conn
        return conn

    def register_mock_fallback(self, *, platform: str, notes: str = "") -> ConnectorRegistration:
        """Registra o mecanismo de MENOR prioridade, sempre viável,
        explicitamente rotulado MOCK — nunca disfarçado de mecanismo
        real (item 6: 'fallback autorizado')."""
        return self.register_connector(
            platform=platform, connector_type=ConnectorType.MOCK_FALLBACK, is_configured=True, enabled=True,
            authenticated=True, health=HealthStatus.OK, permissions_ok=True,
            notes=notes or "Fallback MOCK explícito — nenhuma chamada real de API/navegador/RPA é feita.",
        )

    def available_connectors(self, *, platform: str | None = None) -> list:
        results = list(self._connectors.values())
        if platform is not None:
            results = [c for c in results if c.platform == platform]
        return results

    def best_available_execution_path(
        self, *, platform: str, allowed_mechanisms: set | None = None,
    ) -> ConnectorRegistration | None:
        """Só considera conectores genuinamente VIÁVEIS
        (`ConnectorRegistration.is_viable()`) — nunca um com
        `is_configured=False`. Devolve `None` quando nada está
        disponível (NAVIGATOR deve então reportar BLOCKED/UNAVAILABLE,
        nunca fingir execução).

        CORREÇÃO (item 1 — tools_allowed de ponta a ponta):
        `allowed_mechanisms`, quando informado (conjunto de
        `ConnectorType`), restringe a busca — um mecanismo fora desse
        conjunto NUNCA é retornado, mesmo que seja o único viável.
        `None` preserva o comportamento anterior (sem restrição)."""
        candidates = [c for c in self.available_connectors(platform=platform) if c.is_viable()]
        if allowed_mechanisms is not None:
            candidates = [c for c in candidates if c.connector_type in allowed_mechanisms]
        for connector_type in EXECUTION_MECHANISM_PRIORITY:
            for candidate in candidates:
                if candidate.connector_type == connector_type:
                    return candidate
        return None


def build_default_bridge(*, platforms: tuple) -> BridgeHub:
    """BridgeHub padrão para uso pelo NAVIGATOR quando nenhum é
    fornecido explicitamente — registra SOMENTE o fallback MOCK
    explícito para cada plataforma conhecida (nenhum mecanismo REAL é
    inventado; qualquer integração real precisaria ser registrada
    explicitamente via `register_connector(..., is_configured=True,
    authenticated=True, ...)` fora deste lote)."""
    hub = BridgeHub()
    for platform in platforms:
        hub.register_mock_fallback(platform=platform)
    return hub
