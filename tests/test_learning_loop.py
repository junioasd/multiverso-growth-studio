import os
import tempfile
import unittest

from core.learning.loop import LearningLoop, Prediction, ValidationStatus
from core.models import db as db_module

"""
Testes BLOCO FINAL 1 (item 6) — Learning Loop: registro real de
previsão/outcome, barra estatística de validação, isolamento de
escopo (canal/projeto), propagação cross-escopo só quando explícita,
persistência real entre restarts.
"""


class LearningLoopCoreTests(unittest.TestCase):
    def test_outcome_without_prediction_raises(self):
        loop = LearningLoop()
        with self.assertRaises(KeyError):
            loop.record_outcome(prediction_id="inexistente", actual_value=1.0)

    def test_single_observation_never_validates(self):
        """Nunca promove aprendizado a partir de uma única observação
        — amostra insuficiente."""
        loop = LearningLoop()
        pred = loop.record_prediction(Prediction.new(
            agent_id="ECHO", metric_name="ctr", predicted_value=0.05, context="x", scope_channel_id="c1",
        ))
        loop.record_outcome(prediction_id=pred.prediction_id, actual_value=0.06)
        status = loop.evaluate_learning(metric_name="ctr", scope_channel_id="c1")
        self.assertEqual(status, ValidationStatus.INSUFFICIENT_DATA)

    def test_inconsistent_direction_never_validates(self):
        """5+ observações, mas direção inconsistente -> CONTRADICTED,
        nunca promovido."""
        loop = LearningLoop()
        values = [(0.5, 0.6), (0.5, -0.1), (0.5, -0.2), (0.5, 0.6), (0.5, -0.3), (0.5, -0.4)]
        for predicted, actual in values:
            pred = loop.record_prediction(Prediction.new(
                agent_id="ECHO", metric_name="x", predicted_value=predicted, context="x", scope_channel_id="c1",
            ))
            loop.record_outcome(prediction_id=pred.prediction_id, actual_value=actual)
        status = loop.evaluate_learning(metric_name="x", scope_channel_id="c1")
        self.assertEqual(status, ValidationStatus.CONTRADICTED)
        with self.assertRaises(ValueError):
            loop.promote_validated_learning(
                metric_name="x", statement="tentativa inválida", validated_by="ECHO", scope_channel_id="c1",
            )

    def test_consistent_direction_with_enough_samples_validates(self):
        loop = LearningLoop()
        for i in range(6):
            pred = loop.record_prediction(Prediction.new(
                agent_id="ECHO", metric_name="retention", predicted_value=0.5, context="x", scope_channel_id="c1",
            ))
            loop.record_outcome(prediction_id=pred.prediction_id, actual_value=0.6 + i * 0.01)
        status = loop.evaluate_learning(metric_name="retention", scope_channel_id="c1")
        self.assertEqual(status, ValidationStatus.VALIDATED)
        learning = loop.promote_validated_learning(
            metric_name="retention", statement="gancho funciona", validated_by="ECHO", scope_channel_id="c1",
        )
        self.assertEqual(learning.sample_size, 6)
        self.assertFalse(learning.applies_globally)


class LearningLoopScopeIsolationTests(unittest.TestCase):
    """Item 6 — nunca contamina outros canais/projetos."""

    def _validate_for_channel(self, loop, channel_id):
        for i in range(6):
            pred = loop.record_prediction(Prediction.new(
                agent_id="ECHO", metric_name="ctr", predicted_value=0.5, context="x", scope_channel_id=channel_id,
            ))
            loop.record_outcome(prediction_id=pred.prediction_id, actual_value=0.6)
        return loop.promote_validated_learning(
            metric_name="ctr", statement="aprendizado local", validated_by="ECHO", scope_channel_id=channel_id,
        )

    def test_learning_never_visible_to_different_channel(self):
        loop = LearningLoop()
        self._validate_for_channel(loop, "channel_A")
        self.assertEqual(loop.learnings_visible_to(scope_channel_id="channel_B"), [])
        self.assertEqual(len(loop.learnings_visible_to(scope_channel_id="channel_A")), 1)

    def test_explicit_global_flag_propagates_across_channels(self):
        """Só propaga entre canais quando `applies_globally=True` foi
        decidido EXPLICITAMENTE por quem validou."""
        loop = LearningLoop()
        for i in range(6):
            pred = loop.record_prediction(Prediction.new(
                agent_id="ECHO", metric_name="ctr", predicted_value=0.5, context="x", scope_channel_id="channel_A",
            ))
            loop.record_outcome(prediction_id=pred.prediction_id, actual_value=0.6)
        loop.promote_validated_learning(
            metric_name="ctr", statement="principio universal", validated_by="ATLAS",
            scope_channel_id="channel_A", applies_globally=True,
        )
        visible_elsewhere = loop.learnings_visible_to(scope_channel_id="channel_B")
        self.assertEqual(len(visible_elsewhere), 1)
        self.assertTrue(visible_elsewhere[0].applies_globally)

    def test_project_scope_isolation(self):
        loop = LearningLoop()
        for i in range(6):
            pred = loop.record_prediction(Prediction.new(
                agent_id="ECHO", metric_name="roi", predicted_value=0.5, context="x", scope_project_id="proj_1",
            ))
            loop.record_outcome(prediction_id=pred.prediction_id, actual_value=0.6)
        loop.promote_validated_learning(
            metric_name="roi", statement="x", validated_by="ECHO", scope_project_id="proj_1",
        )
        self.assertEqual(loop.learnings_visible_to(scope_project_id="proj_2"), [])
        self.assertEqual(len(loop.learnings_visible_to(scope_project_id="proj_1")), 1)


class LearningLoopPersistenceTests(unittest.TestCase):
    """Persistência real — sobrevive a restart, mesma disciplina do
    resto do sistema."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_predictions_and_records_survive_restart(self):
        loop1 = LearningLoop(db_path=self.db_path)
        pred = loop1.record_prediction(Prediction.new(
            agent_id="ECHO", metric_name="ctr", predicted_value=0.5, context="x", scope_channel_id="c1",
        ))
        loop1.record_outcome(prediction_id=pred.prediction_id, actual_value=0.6)

        loop2 = LearningLoop(db_path=self.db_path)
        self.assertEqual(len(loop2.all_predictions()), 1)
        self.assertEqual(len(loop2.all_records()), 1)
        self.assertEqual(loop2.all_records()[0].actual_value, 0.6)

    def test_validated_learning_survives_restart(self):
        loop1 = LearningLoop(db_path=self.db_path)
        for i in range(6):
            pred = loop1.record_prediction(Prediction.new(
                agent_id="ECHO", metric_name="ctr", predicted_value=0.5, context="x", scope_channel_id="c1",
            ))
            loop1.record_outcome(prediction_id=pred.prediction_id, actual_value=0.6)
        loop1.promote_validated_learning(
            metric_name="ctr", statement="x", validated_by="ECHO", scope_channel_id="c1",
        )

        loop2 = LearningLoop(db_path=self.db_path)
        visible = loop2.learnings_visible_to(scope_channel_id="c1")
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].sample_size, 6)


if __name__ == "__main__":
    unittest.main()
