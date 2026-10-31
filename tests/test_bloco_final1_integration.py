import os
import tempfile
import unittest

from api.bootstrap import build_app
from core.agents.runtime.contracts import AgentTaskStatus
from core.models import db as db_module
from core.security.auth import hash_password
from youtube_strategy.distribution_engine import ContentCharacteristics

"""
Testes BLOCO FINAL 1 — persistência real após restart do Learning Loop
(via GrowthStudioApp completo) e coordenação real entre agentes
(ECHO -> ARGOS -> YouTube distribution, todos compartilhando o mesmo
Learning Loop e nunca contaminando canais diferentes).
"""


class LearningLoopRestartPersistenceTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_validated_learning_survives_full_app_restart(self):
        app1 = build_app(owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), db_path=self.db_path)
        for _ in range(6):
            r = app1.atlas.echo.execute(task_type="RECORD_PREDICTION", payload={
                "predicting_agent_id": "ARGOS", "metric_name": "retention", "predicted_value": 0.5,
                "context": "x", "scope_channel_id": "c1",
            })
            app1.atlas.echo.execute(task_type="RECORD_OUTCOME", payload={"prediction_id": r.output["prediction_id"], "actual_value": 0.6})
        app1.atlas.echo.execute(task_type="PROMOTE_LEARNING", payload={
            "metric_name": "retention", "statement": "aprendizado persistido", "validated_by": "ECHO", "scope_channel_id": "c1",
        })
        app1.shutdown()

        app2 = build_app(owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), db_path=self.db_path)
        try:
            visible = app2.atlas.echo.execute(task_type="GET_VISIBLE_LEARNINGS", payload={"scope_channel_id": "c1"})
            self.assertEqual(len(visible.output["learnings"]), 1)
            self.assertEqual(visible.output["learnings"][0]["sample_size"], 6)
        finally:
            app2.shutdown()


class RealMultiAgentCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app(owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), persistent=False)

    def tearDown(self):
        self.app.shutdown()

    def test_echo_argos_youtube_distribution_share_same_learning(self):
        """ECHO valida um aprendizado -> ARGOS usa esse aprendizado
        para priorizar pauta -> o motor de distribuição do YouTube
        (mesmo Learning Loop) também enxerga o mesmo aprendizado —
        tudo através de instâncias REALMENTE compartilhadas, nunca
        sistemas paralelos desconectados."""
        atlas = self.app.atlas
        for _ in range(6):
            r = atlas.echo.execute(task_type="RECORD_PREDICTION", payload={
                "predicting_agent_id": "PULSE", "metric_name": "ctr", "predicted_value": 0.5,
                "context": "x", "scope_channel_id": "canal_real",
            })
            atlas.echo.execute(task_type="RECORD_OUTCOME", payload={"prediction_id": r.output["prediction_id"], "actual_value": 0.6})
        atlas.echo.execute(task_type="PROMOTE_LEARNING", payload={
            "metric_name": "ctr", "statement": "thumbnail com rosto humano converte melhor",
            "validated_by": "ECHO", "scope_channel_id": "canal_real",
        })

        # ARGOS enxerga o aprendizado
        plan = atlas.argos.execute(task_type="BUILD_CONTENT_PLAN", payload={
            "candidate_topics": ["thumbnail com rosto humano no proximo video", "topico aleatorio sem relacao"],
            "scope_channel_id": "canal_real",
        })
        self.assertEqual(plan.status, AgentTaskStatus.COMPLETED)
        self.assertGreater(plan.output["ranked_topics"][0]["priority_score"], 0)

        # o motor de distribuicao do YouTube (app.youtube_distribution) tambem enxerga
        dist_plan = self.app.youtube_distribution.plan_distribution(
            channel_id="canal_real", title="Video de teste", characteristics=ContentCharacteristics(duration_seconds=40),
            thumbnail_matches_content=True,
        )
        self.assertEqual(len(dist_plan["applicable_learnings"]), 1)

        # canal DIFERENTE nunca contamina
        dist_plan_other = self.app.youtube_distribution.plan_distribution(
            channel_id="outro_canal", title="Video de teste", characteristics=ContentCharacteristics(duration_seconds=40),
            thumbnail_matches_content=True,
        )
        self.assertEqual(len(dist_plan_other["applicable_learnings"]), 0)

    def test_veritas_can_verify_evidence_registered_by_pipeline(self):
        """Coordenação real: VERITAS consulta o MESMO EvidenceRegistry
        que o resto do sistema usaria (quando conectado explicitamente
        pelo chamador) — nunca um sistema de evidência paralelo."""
        import time as time_module

        from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
        from core.multimodal.evidence import Modality
        from tests._evidence_test_helpers import make_test_source_registry, make_test_verifier_registry

        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_real", provenance="x", observed_at=time_module.time(),
            modality=Modality.DOCUMENT, confidence=0.9, verifier="teste_automatizado",
            verification_method="validacao_manual", requested_verification_status=VerificationStatus.VERIFIED,
        ))
        self.app.atlas.veritas.evidence_registry = registry
        result = self.app.atlas.veritas.execute(task_type="VERIFY_CLAIM", payload={"evidence_ids": ["ev1"]})
        self.assertEqual(result.output["verdict"], "SUPPORTED")


if __name__ == "__main__":
    unittest.main()
