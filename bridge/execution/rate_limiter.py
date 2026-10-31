"""
bridge.execution.rate_limiter
==================================
BLOCO FINAL 2 (item 1 — rate limits). `RateLimiter` — algoritmo REAL
de token bucket (balde de tokens), isolado por chave (plataforma+conta/
canal — nunca um limite global compartilhado entre contas diferentes,
o que vazaria capacidade de uma conta para outra). Baseado em tempo
real (`time.monotonic()` — nunca afetado por ajuste de relógio do
sistema), thread-safe (`threading.Lock` por chave).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class RateLimitDecision:
    allowed: bool
    remaining_tokens: float
    retry_after_seconds: float = 0.0


class _TokenBucket:
    __slots__ = ("capacity", "refill_rate_per_second", "tokens", "last_refill", "lock")

    def __init__(self, *, capacity: float, refill_rate_per_second: float) -> None:
        self.capacity = capacity
        self.refill_rate_per_second = refill_rate_per_second
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate_per_second)
            self.last_refill = now

    def try_consume(self, *, cost: float = 1.0) -> RateLimitDecision:
        with self.lock:
            self._refill()
            if self.tokens >= cost:
                self.tokens -= cost
                return RateLimitDecision(allowed=True, remaining_tokens=self.tokens)
            deficit = cost - self.tokens
            retry_after = deficit / self.refill_rate_per_second if self.refill_rate_per_second > 0 else float("inf")
            return RateLimitDecision(allowed=False, remaining_tokens=self.tokens, retry_after_seconds=retry_after)


class RateLimiter:
    """Registro de rate limiters isolados por chave (ex:
    `"youtube:channel_123"`, `"hotmart:conta_abc"`) — cada chave tem
    seu PRÓPRIO balde, nunca compartilhado. Configuração de
    capacidade/taxa é por chave, permitindo limites diferentes por
    plataforma (ex: YouTube Data API tem cotas bem menores que um
    webhook interno)."""

    def __init__(self) -> None:
        self._buckets: dict = {}
        self._configs: dict = {}
        self._lock = threading.Lock()

    def configure(self, *, key: str, capacity: float, refill_rate_per_second: float) -> None:
        with self._lock:
            self._configs[key] = (capacity, refill_rate_per_second)
            self._buckets[key] = _TokenBucket(capacity=capacity, refill_rate_per_second=refill_rate_per_second)

    def try_acquire(self, *, key: str, cost: float = 1.0) -> RateLimitDecision:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # CORREÇÃO: chave nunca configurada explicitamente --
                # aplica um padrão conservador (1 req/s, capacidade 5)
                # em vez de permitir ilimitado por omissão.
                bucket = _TokenBucket(capacity=5.0, refill_rate_per_second=1.0)
                self._buckets[key] = bucket
        return bucket.try_consume(cost=cost)

    def remaining(self, *, key: str) -> float:
        bucket = self._buckets.get(key)
        if bucket is None:
            return 0.0
        with bucket.lock:
            bucket._refill()
            return bucket.tokens
