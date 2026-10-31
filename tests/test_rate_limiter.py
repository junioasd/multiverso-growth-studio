import unittest
from unittest.mock import patch

from bridge.execution.rate_limiter import RateLimiter

"""
Testes BLOCO FINAL 2 (item 1 — rate limits): balde de tokens real,
isolamento real por chave (nunca vaza capacidade entre contas/canais),
reabastecimento real baseado em tempo — usando `time.monotonic`
controlado para manter os testes rápidos e determinísticos (nunca
`time.sleep` real, mesma disciplina da correção de suite-order
stability).
"""


class RateLimiterTests(unittest.TestCase):
    def test_allows_up_to_capacity(self):
        limiter = RateLimiter()
        limiter.configure(key="k1", capacity=3, refill_rate_per_second=1.0)
        for _ in range(3):
            self.assertTrue(limiter.try_acquire(key="k1").allowed)

    def test_denies_beyond_capacity(self):
        limiter = RateLimiter()
        limiter.configure(key="k1", capacity=2, refill_rate_per_second=1.0)
        limiter.try_acquire(key="k1")
        limiter.try_acquire(key="k1")
        decision = limiter.try_acquire(key="k1")
        self.assertFalse(decision.allowed)
        self.assertGreater(decision.retry_after_seconds, 0)

    def test_different_keys_never_share_budget(self):
        """Isolamento real — esgotar o balde de uma conta/canal nunca
        afeta outra."""
        limiter = RateLimiter()
        limiter.configure(key="youtube:canalA", capacity=1, refill_rate_per_second=0.1)
        limiter.configure(key="youtube:canalB", capacity=1, refill_rate_per_second=0.1)
        self.assertTrue(limiter.try_acquire(key="youtube:canalA").allowed)
        self.assertFalse(limiter.try_acquire(key="youtube:canalA").allowed)
        # canal B nao foi tocado -- deve continuar com capacidade plena
        self.assertTrue(limiter.try_acquire(key="youtube:canalB").allowed)

    def test_unconfigured_key_never_unlimited(self):
        limiter = RateLimiter()
        decisions = [limiter.try_acquire(key="nunca_configurada") for _ in range(10)]
        # padrao conservador (capacidade 5) -- nem todas as 10 podem ser permitidas
        self.assertFalse(all(d.allowed for d in decisions))

    def test_refill_over_simulated_time(self):
        """Reabastecimento real baseado em tempo — usa
        `time.monotonic` controlado (nunca `time.sleep` real) para
        manter o teste rápido e determinístico. CRÍTICO: o balde
        precisa ser CRIADO dentro do bloco de patch também — criar
        antes usaria `time.monotonic()` REAL (um valor de referência
        grande e imprevisível) para `last_refill`, e comparar depois
        contra um `fake_time` pequeno geraria um `elapsed` negativo
        artificial, nunca reabastecendo (bug de teste, não do
        RateLimiter — corrigido aqui)."""
        limiter = RateLimiter()
        fake_time = [1000.0]
        with patch("time.monotonic", side_effect=lambda: fake_time[0]):
            limiter.configure(key="k1", capacity=1, refill_rate_per_second=1.0)
            self.assertTrue(limiter.try_acquire(key="k1").allowed)
            self.assertFalse(limiter.try_acquire(key="k1").allowed)
            fake_time[0] += 1.5  # avanca o tempo simulado em 1.5s
            self.assertTrue(limiter.try_acquire(key="k1").allowed)

    def test_remaining_reflects_real_state(self):
        limiter = RateLimiter()
        limiter.configure(key="k1", capacity=5, refill_rate_per_second=1.0)
        limiter.try_acquire(key="k1", cost=2)
        self.assertAlmostEqual(limiter.remaining(key="k1"), 3.0, delta=0.1)

    def test_cost_greater_than_one(self):
        limiter = RateLimiter()
        limiter.configure(key="k1", capacity=10, refill_rate_per_second=1.0)
        decision = limiter.try_acquire(key="k1", cost=5)
        self.assertTrue(decision.allowed)
        self.assertAlmostEqual(decision.remaining_tokens, 5.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
