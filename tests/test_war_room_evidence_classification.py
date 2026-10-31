import unittest

from api.dashboard import OwnerWarRoom, PerformanceDashboard
from commercial.sales_tracking import SalesTracker
from core.models.entities import ValueType

"""
Testes de regressão — MICROCORREÇÃO 4, item 4
(CLASSIFICAÇÃO DE EVIDÊNCIA NO WAR ROOM).

Falha original: uma resposta de receita FORECAST era corretamente
rotulada no TEXTO como previsão, mas o campo estruturado
`based_on_real_data` vinha `True` — porque a lógica antiga só checava
"o valor é diferente de UNKNOWN?", nunca o tipo de evidência real.

Corrigido: cada resposta carrega um `evidence_type` explícito
("REAL"|"ESTIMATED"|"FORECAST"|"UNKNOWN"|"N/A"), e `based_on_real_data`
é sempre exatamente `evidence_type == "REAL"` — nunca inferido da mera
presença do valor.
"""


def _answer_for(answers, question_substring):
    matches = [a for a in answers if question_substring in a.question]
    assert matches, f"nenhuma resposta encontrada para '{question_substring}'"
    return matches[0]


class WarRoomEvidenceClassificationTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = PerformanceDashboard()
        self.war_room = OwnerWarRoom()

    def test_forecast_revenue_never_marked_as_based_on_real_data(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=7777)
        snap = self.dashboard.build_snapshot(sales_summary=tracker.summarize("c1"))
        answers = self.war_room.answer(snap)

        forecast_answer = _answer_for(answers, "PREVISTA/FORECAST")
        self.assertEqual(forecast_answer.evidence_type, "FORECAST")
        self.assertFalse(forecast_answer.based_on_real_data)
        self.assertIn("7777", forecast_answer.answer)  # texto continua correto

    def test_estimated_revenue_never_marked_as_based_on_real_data(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.ESTIMATED, amount=555)
        snap = self.dashboard.build_snapshot(sales_summary=tracker.summarize("c1"))
        answers = self.war_room.answer(snap)

        estimated_answer = _answer_for(answers, "ESTIMADA")
        self.assertEqual(estimated_answer.evidence_type, "ESTIMATED")
        self.assertFalse(estimated_answer.based_on_real_data)

    def test_real_revenue_correctly_marked_as_based_on_real_data(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=300)
        snap = self.dashboard.build_snapshot(sales_summary=tracker.summarize("c1"))
        answers = self.war_room.answer(snap)

        real_answer = _answer_for(answers, "REAL, líquida")
        self.assertEqual(real_answer.evidence_type, "REAL")
        self.assertTrue(real_answer.based_on_real_data)

    def test_zero_real_revenue_is_distinct_from_unknown(self):
        # nenhuma venda registrada -> real_revenue é 0.0 (zero
        # efetivamente computado), não ausência de dado.
        tracker = SalesTracker()
        snap = self.dashboard.build_snapshot(sales_summary=tracker.summarize("c1"))
        answers = self.war_room.answer(snap)

        real_answer = _answer_for(answers, "REAL, líquida")
        self.assertEqual(real_answer.evidence_type, "REAL")
        self.assertTrue(real_answer.based_on_real_data)
        self.assertIn("0.0", real_answer.answer)
        self.assertNotIn("UNKNOWN", real_answer.answer)

    def test_absence_of_sales_summary_is_unknown_not_real(self):
        # sem sales_summary NENHUM -> revenue fica UNKNOWN (ausência de
        # dado), nunca confundido com um zero comprovado.
        snap = self.dashboard.build_snapshot()  # nenhum argumento
        answers = self.war_room.answer(snap)

        real_answer = _answer_for(answers, "REAL, líquida")
        self.assertEqual(real_answer.evidence_type, "UNKNOWN")
        self.assertFalse(real_answer.based_on_real_data)
        self.assertIn("UNKNOWN", real_answer.answer)

    def test_mixed_scenario_each_evidence_type_classified_independently(self):
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=100)
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.ESTIMATED, amount=200)
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=300)
        snap = self.dashboard.build_snapshot(sales_summary=tracker.summarize("c1"))
        answers = self.war_room.answer(snap)

        real_answer = _answer_for(answers, "REAL, líquida")
        estimated_answer = _answer_for(answers, "ESTIMADA")
        forecast_answer = _answer_for(answers, "PREVISTA/FORECAST")

        self.assertEqual(real_answer.evidence_type, "REAL")
        self.assertTrue(real_answer.based_on_real_data)
        self.assertEqual(estimated_answer.evidence_type, "ESTIMATED")
        self.assertFalse(estimated_answer.based_on_real_data)
        self.assertEqual(forecast_answer.evidence_type, "FORECAST")
        self.assertFalse(forecast_answer.based_on_real_data)

    def test_based_on_real_data_always_matches_evidence_type_invariant(self):
        """Invariante estrutural: para QUALQUER resposta, based_on_real_data
        deve ser exatamente (evidence_type == 'REAL') — nunca calculado
        de outra forma."""
        tracker = SalesTracker()
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.FORECAST, amount=1)
        tracker.record_event(channel_id="c1", event_type="sale", value_type=ValueType.REAL, amount=1)
        snap = self.dashboard.build_snapshot(
            sales_summary=tracker.summarize("c1"), scope_label="c1", warnings=["teste"],
        )
        answers = self.war_room.answer(snap)
        for a in answers:
            self.assertEqual(
                a.based_on_real_data, a.evidence_type == "REAL",
                f"inconsistência em '{a.question}': evidence_type={a.evidence_type}, "
                f"based_on_real_data={a.based_on_real_data}",
            )

    def test_scope_label_is_not_a_business_evidence_type(self):
        snap = self.dashboard.build_snapshot(scope_label="chan_1")
        answers = self.war_room.answer(snap)
        scope_answer = _answer_for(answers, "Escopo")
        self.assertEqual(scope_answer.evidence_type, "N/A")
        self.assertFalse(scope_answer.based_on_real_data)

    def test_margin_from_finance_marked_as_real(self):
        from commercial.finance import FinanceLedger

        ledger = FinanceLedger()
        ledger.record(entry_type="revenue", amount=1000, channel_id="c1")
        snap = self.dashboard.build_snapshot(finance_snapshot=ledger.snapshot("c1"))
        answers = self.war_room.answer(snap)
        margin_answer = _answer_for(answers, "margem")
        self.assertEqual(margin_answer.evidence_type, "REAL")
        self.assertTrue(margin_answer.based_on_real_data)


if __name__ == "__main__":
    unittest.main()
