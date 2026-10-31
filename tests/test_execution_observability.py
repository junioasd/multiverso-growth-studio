import os
import tempfile
import unittest

from bridge.execution.observability import ExecutionLogEntry, ExecutionObservability
from core.models import db as db_module

"""
Testes BLOCO FINAL 2 (item 7 — observabilidade): campos estruturados,
consultas reais por plataforma/conta/sucesso, agregação de custo
(nunca assume zero para o que não foi informado), taxa de falha,
persistência real através de restart.
"""


class ExecutionObservabilityInMemoryTests(unittest.TestCase):
    def test_query_by_platform(self):
        obs = ExecutionObservability()
        obs.record(ExecutionLogEntry.new(action="A", platform="youtube", success=True))
        obs.record(ExecutionLogEntry.new(action="B", platform="facebook", success=True))
        self.assertEqual(len(obs.query(platform="youtube")), 1)

    def test_query_by_success(self):
        obs = ExecutionObservability()
        obs.record(ExecutionLogEntry.new(action="A", platform="x", success=True))
        obs.record(ExecutionLogEntry.new(action="B", platform="x", success=False))
        self.assertEqual(len(obs.query(platform="x", success=False)), 1)

    def test_failure_rate_computed_correctly(self):
        obs = ExecutionObservability()
        obs.record(ExecutionLogEntry.new(action="A", platform="x", success=True))
        obs.record(ExecutionLogEntry.new(action="B", platform="x", success=False))
        obs.record(ExecutionLogEntry.new(action="C", platform="x", success=False))
        self.assertAlmostEqual(obs.failure_rate(platform="x"), 2 / 3)

    def test_failure_rate_none_when_no_entries(self):
        obs = ExecutionObservability()
        self.assertIsNone(obs.failure_rate(platform="nunca_usada"))

    def test_cost_aggregation_ignores_entries_without_cost(self):
        """Entradas sem `cost_amount` (nunca gastaram, ou custo
        desconhecido) NUNCA contam como custo zero na agregação —
        apenas somam o que foi genuinamente informado."""
        obs = ExecutionObservability()
        obs.record(ExecutionLogEntry.new(action="A", platform="google_ads", success=True, cost_amount=30.0, cost_currency="BRL"))
        obs.record(ExecutionLogEntry.new(action="B", platform="google_ads", success=True))  # sem custo -- nunca vira 0
        total = obs.total_cost_by_platform(platform="google_ads")
        self.assertEqual(total, {"BRL": 30.0})

    def test_navigator_result_convenience_method(self):
        from bridge.execution.navigator_real import NavigatorActionResult

        obs = ExecutionObservability()
        result = NavigatorActionResult(success=True, platform="youtube", action="GET_INFO", mechanism_used="OFFICIAL_API")
        entry = obs.record_navigator_result(result=result, criticality="ROUTINE", actor="teste")
        self.assertTrue(entry.success)
        self.assertEqual(entry.platform, "youtube")


class ExecutionObservabilityPersistenceTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_entries_survive_restart(self):
        obs1 = ExecutionObservability(db_path=self.db_path)
        obs1.record(ExecutionLogEntry.new(action="PUBLISH", platform="instagram", success=True, cost_amount=10.0, cost_currency="USD"))

        obs2 = ExecutionObservability(db_path=self.db_path)
        self.assertEqual(len(obs2._entries), 1)
        self.assertEqual(obs2.total_cost_by_platform(platform="instagram"), {"USD": 10.0})


if __name__ == "__main__":
    unittest.main()
