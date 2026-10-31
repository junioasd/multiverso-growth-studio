"""
core.adapters.base
===================
Contrato base para todos os adapters (Seção 61: Provider Lock-in —
todo provider externo deve passar por um adapter trocável).

Regra crítica (Seção 46): quando não conectado, usar mock. Nunca
afirmar execução/publicação/venda real se apenas mock foi executado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AdapterMode(str, Enum):
    REAL = "REAL"
    MOCK = "MOCK"


@dataclass
class AdapterResult:
    mode: AdapterMode
    success: bool
    data: dict = field(default_factory=dict)
    error: str | None = None

    def is_real(self) -> bool:
        return self.mode == AdapterMode.REAL


class BaseAdapter:
    """Toda adapter concreta deve declarar `provider_name` e implementar
    `is_configured()` para decidir entre modo REAL e MOCK."""

    provider_name: str = "base"

    def is_configured(self) -> bool:  # pragma: no cover - abstrato
        raise NotImplementedError

    def mode(self) -> AdapterMode:
        return AdapterMode.REAL if self.is_configured() else AdapterMode.MOCK
