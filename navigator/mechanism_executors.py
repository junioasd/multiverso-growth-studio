"""
navigator.mechanism_executors
=================================
CORREÇÃO (item 2 — BRIDGE -> NAVIGATOR -> EXECUTOR REAL).

Antes desta correção, o NAVIGATOR sempre usava um único
`PlatformExecutionAdapter` MOCK, independente do mecanismo que o
BRIDGE selecionou — podendo gerar evidência contraditória
(`selected_mechanism=OFFICIAL_API` mas `mode=MOCK`).

Agora CADA mecanismo (`ConnectorType`) tem sua própria classe de
executor. Por padrão (nenhuma integração externa real configurada
neste projeto), os executores de mecanismos "reais"
(`OFFICIAL_API`/`MCP_PLUGIN`/`WEBHOOK`/`BROWSER_AUTOMATION`/
`COMPUTER_USE_RPA`) levantam erro claro em vez de fingir sucesso —
nunca silenciosamente caem para MOCK. `MOCK_FALLBACK` é o ÚNICO
mecanismo cujo executor sempre responde com `AdapterMode.MOCK`,
explicitamente rotulado como tal.

Uma integração real (fora deste lote) se conecta aqui registrando um
executor concreto via `NavigatorExecutor.register_mechanism_executor()`
— nunca alterando o motor de seleção do BRIDGE.
"""

from __future__ import annotations

from dataclasses import dataclass

from bridge.hub import ConnectorType
from core.adapters.base import AdapterMode, AdapterResult, BaseAdapter
from navigator.models import NavigatorAction


@dataclass
class MechanismExecutor(BaseAdapter):
    """Base para o executor de UM mecanismo específico numa
    plataforma. `mechanism` identifica qual `ConnectorType` esta
    instância implementa — usado para a checagem de consistência
    `selected_mechanism` == mecanismo do executor que realmente
    rodou (ver `navigator/executor.py`)."""

    mechanism: ConnectorType = ConnectorType.MOCK_FALLBACK
    provider_name: str = "generic"

    def is_configured(self) -> bool:
        return False

    def execute(self, *, action: NavigatorAction, payload: dict) -> AdapterResult:  # pragma: no cover - abstrato
        raise NotImplementedError


class OfficialApiExecutor(MechanismExecutor):
    """Sem integração real de API oficial configurada neste ambiente —
    nunca finge sucesso; levanta erro claro. Uma integração real se
    pluga substituindo esta classe (via
    `register_mechanism_executor`), nunca alterando o NAVIGATOR."""

    def __init__(self, *, provider_name: str = "generic") -> None:
        super().__init__(mechanism=ConnectorType.OFFICIAL_API, provider_name=provider_name)

    def execute(self, *, action: NavigatorAction, payload: dict) -> AdapterResult:
        raise RuntimeError(
            f"nenhuma integração real de API oficial configurada para '{self.provider_name}' neste ambiente"
        )


class ConnectorMcpExecutor(MechanismExecutor):
    def __init__(self, *, provider_name: str = "generic") -> None:
        super().__init__(mechanism=ConnectorType.MCP_PLUGIN, provider_name=provider_name)

    def execute(self, *, action: NavigatorAction, payload: dict) -> AdapterResult:
        raise RuntimeError(f"nenhum conector/MCP real configurado para '{self.provider_name}' neste ambiente")


class WebhookExecutor(MechanismExecutor):
    def __init__(self, *, provider_name: str = "generic") -> None:
        super().__init__(mechanism=ConnectorType.WEBHOOK, provider_name=provider_name)

    def execute(self, *, action: NavigatorAction, payload: dict) -> AdapterResult:
        raise RuntimeError(f"nenhum webhook real configurado para '{self.provider_name}' neste ambiente")


class BrowserAutomationExecutor(MechanismExecutor):
    def __init__(self, *, provider_name: str = "generic") -> None:
        super().__init__(mechanism=ConnectorType.BROWSER_AUTOMATION, provider_name=provider_name)

    def execute(self, *, action: NavigatorAction, payload: dict) -> AdapterResult:
        raise RuntimeError(f"nenhum navegador automatizado real configurado para '{self.provider_name}'")


class ComputerUseExecutor(MechanismExecutor):
    def __init__(self, *, provider_name: str = "generic") -> None:
        super().__init__(mechanism=ConnectorType.COMPUTER_USE_RPA, provider_name=provider_name)

    def execute(self, *, action: NavigatorAction, payload: dict) -> AdapterResult:
        raise RuntimeError(f"nenhum Computer Use/RPA real configurado para '{self.provider_name}'")


class MockFallbackExecutor(MechanismExecutor):
    """O ÚNICO mecanismo cujo executor sempre responde — explicitamente
    rotulado MOCK, nunca disfarçado."""

    def __init__(self, *, provider_name: str = "generic") -> None:
        super().__init__(mechanism=ConnectorType.MOCK_FALLBACK, provider_name=provider_name)

    def is_configured(self) -> bool:
        return False  # is_configured=False -> mode() cai em MOCK (correto: é o fallback MOCK)

    def execute(self, *, action: NavigatorAction, payload: dict) -> AdapterResult:
        return AdapterResult(
            mode=AdapterMode.MOCK, success=True,
            data={
                "platform": self.provider_name, "action": action.value,
                "note": "MOCK — nenhuma chamada real de API/navegador/RPA foi feita",
            },
        )


_EXECUTOR_CLASS_BY_MECHANISM = {
    ConnectorType.OFFICIAL_API: OfficialApiExecutor,
    ConnectorType.MCP_PLUGIN: ConnectorMcpExecutor,
    ConnectorType.WEBHOOK: WebhookExecutor,
    ConnectorType.BROWSER_AUTOMATION: BrowserAutomationExecutor,
    ConnectorType.COMPUTER_USE_RPA: ComputerUseExecutor,
    ConnectorType.MOCK_FALLBACK: MockFallbackExecutor,
}


def build_default_mechanism_executor(mechanism: ConnectorType, *, provider_name: str) -> MechanismExecutor:
    executor_class = _EXECUTOR_CLASS_BY_MECHANISM[mechanism]
    return executor_class(provider_name=provider_name)
