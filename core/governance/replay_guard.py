"""
core.governance.replay_guard
==============================
Proteção contra replay/reprocessamento de operações governadas
(MICROCORREÇÃO DE HARDENING — item "stale/replayed operations").

Chamadores de uma operação mutável podem opcionalmente fornecer um
`operation_id` único (ex: um UUID gerado pelo cliente, ou o
idempotency_key do domínio). Se o mesmo `operation_id` for visto de
novo dentro da janela TTL, a operação é recusada sem ser reexecutada —
mesmo que a operação de domínio subjacente já tenha sua própria
idempotência (defesa em profundidade).

`operation_id=None` significa "sem proteção de replay solicitada" —
não é um erro, apenas não há verificação extra nesse nível (a
idempotência de domínio, quando aplicável, continua valendo).
"""

from __future__ import annotations

import threading
import time


class ReplayRejected(Exception):
    pass


class ReplayGuard:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_record(self, operation_id: str | None) -> None:
        if operation_id is None:
            return

        now = time.time()
        with self._lock:
            # Purga oportunista de entradas expiradas (mantém a estrutura
            # pequena sem precisar de um processo de limpeza separado).
            expired = [key for key, seen_at in self._seen.items() if now - seen_at > self.ttl_seconds]
            for key in expired:
                del self._seen[key]

            if operation_id in self._seen:
                raise ReplayRejected(
                    f"operação '{operation_id}' já foi processada recentemente "
                    f"(janela de replay: {self.ttl_seconds}s)"
                )
            self._seen[operation_id] = now

    def size(self) -> int:
        with self._lock:
            return len(self._seen)
