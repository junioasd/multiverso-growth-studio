"""
core.governance.idempotency
==============================
MICROCORREÇÃO 2 (auditoria): a idempotência anterior indexava apenas
pela `idempotency_key` crua, sem considerar canal/projeto/operação —
uma chave reaproveitada em outro canal podia retornar o registro do
canal errado. Cada serviço agora compõe a chave de idempotência como
`<escopo>::<idempotency_key>` (ex: `chan_1::retry-42`), e associa um
"fingerprint" do conteúdo da operação. Reuso da mesma chave composta
com fingerprint diferente é sempre rejeitado (`IdempotencyConflict`)
em vez de silenciosamente devolver ou sobrescrever o registro anterior.

Este módulo contém apenas o vocabulário compartilhado (exceção +
helper de fingerprint serializável) — cada serviço mantém seu próprio
índice e lock, pois cada um tem sua própria unidade de persistência.
"""

from __future__ import annotations

import json


class IdempotencyConflict(Exception):
    """A mesma idempotency_key (dentro do mesmo escopo) foi reutilizada
    com conteúdo/parâmetros incompatíveis com a chamada original."""


def stable_fingerprint(*parts) -> str:
    """Serializa `parts` (tupla de valores primitivos JSON-compatíveis)
    de forma determinística, para comparação e persistência."""
    return json.dumps(list(parts), sort_keys=True, default=str)
