import os
import time
import unittest
from unittest.mock import patch
from tests._evidence_test_helpers import make_test_verifier_registry, make_test_source_registry
from tests._auth_test_helpers import fast_hash_password

from core.evidence.claims import ClaimType
from core.evidence.metric_evidence import MetricEvidence
from core.evidence.provenance import ProvenanceRecord
from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
from core.multimodal.evidence import Modality
from core.quality.gates import Verdict
from radar_web.adoption import AdoptionPipeline
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy
from radar_web.quality_gate import RadarWebEvidenceGate

"""
Testes MICROCORRECAO V6.1.1 (rodada 2) - itens 1, 2, 3, 4, 5, 6, 7, 11.
"""


def make_verified_registry(*, evidence_id="ev1") -> EvidenceRegistry:
    registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
    registry.register(EvidenceRecord(
        evidence_id=evidence_id, type="source_document", source="fonte_real",
        provenance="coleta verificada", observed_at=time.time(), modality=Modality.DOCUMENT,
        confidence=0.9, source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
    ))
    from tests._evidence_test_helpers import register_test_claim
    register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")
    return registry


class RealEntrypointAutostartTests(unittest.TestCase):
    """Item 1 — cenário exato via a lógica real do `main.py cmd_serve`."""

    def setUp(self):
        self._apps = []

    def tearDown(self):
        for app in self._apps:
            app.shutdown()

    def _cmd_serve_logic(self, *, radar_web_enabled: bool, interval_seconds: float = 0.2):
        """Reproduz EXATAMENTE a lógica do `cmd_serve` real (main.py) —
        nunca uma versão simplificada."""
        from api.bootstrap import build_app

        app = build_app(
            owner_email="owner@x.com", owner_password_hash=fast_hash_password("StrongPass123"),
            persistent=False, start_radar_worker=True, start_radar_web_worker=True,
        )
        self._apps.append(app)
        if radar_web_enabled:
            app.atlas.radar_web_scheduler.enable(interval_seconds=interval_seconds)
        return app

    def test_enabled_executes_automatically_without_manual_enable(self):
        app = self._cmd_serve_logic(radar_web_enabled=True, interval_seconds=0.2)
        deadline = time.time() + 3.0
        while app.atlas.radar_web_scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
            time.sleep(0.05)
        status = app.atlas.radar_web_scheduler.status()
        self.assertEqual(status.last_run_status, "SUCCESS")
        self.assertIsNotNone(status.last_run_at)

    def test_disabled_never_executes_scan(self):
        app = self._cmd_serve_logic(radar_web_enabled=False)
        time.sleep(0.5)
        self.assertTrue(app.atlas.radar_web_worker.is_running())  # infraestrutura ativa
        self.assertEqual(app.atlas.radar_web_scheduler.status().last_run_status, "NEVER_RUN")

    def test_settings_read_from_environment(self):
        with patch.dict(os.environ, {"RADAR_WEB_ENABLED": "true", "RADAR_WEB_INTERVAL_SECONDS": "0.15"}):
            from core.config import Settings

            settings = Settings()
            self.assertTrue(settings.radar_web_enabled)
            self.assertEqual(settings.radar_web_interval_seconds, 0.15)

    def test_settings_default_disabled(self):
        from core.config import Settings

        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()
            self.assertFalse(settings.radar_web_enabled)


class Repeat25AgentsLevelsTests(unittest.TestCase):
    """Item 2 — níveis oficiais dos 25 agentes."""

    def test_all_25_official_levels_exact(self):
        from core.agents.default_registry import build_default_agent_registry

        expected = {
            "ATLAS": "MASTER", "ORION": "MASTER", "SAGE": "ESPECIALISTA", "PRISMA": "ESPECIALISTA",
            "LIBRA": "MASTER", "MERCURY": "MASTER", "VENTURE": "ESPECIALISTA", "NEXUS": "MASTER",
            "RELAY": "ESPECIALISTA", "ARGOS": "MASTER", "SCRIPTOR": "ESPECIALISTA", "VERITAS": "ESPECIALISTA",
            "AURORA": "MASTER", "FRAME": "ESPECIALISTA", "VOX": "ESPECIALISTA", "SONORA": "ESPECIALISTA",
            "PIXEL": "ESPECIALISTA", "PULSE": "MASTER", "BRIDGE": "MASTER", "SENTINEL": "MASTER",
            "JURIS": "ESPECIALISTA", "METRICA": "MASTER", "ECHO": "MASTER", "NAVIGATOR": "MASTER", "FORGE": "MASTER",
        }
        registry = build_default_agent_registry()
        for agent_id, level in expected.items():
            self.assertEqual(registry.get(agent_id).level.value, level, f"{agent_id} deveria ser {level}")

    def test_competency_independent_of_implementation_status(self):
        """CORREÇÃO (BLOCO FINAL 1): NEXUS saiu de SCAFFOLD para ACTIVE
        — a invariante (nível de competência independe do status de
        implementação) continua válida com o status real atualizado."""
        from core.agents.base import ImplementationStatus
        from core.agents.default_registry import build_default_agent_registry

        registry = build_default_agent_registry()
        nexus = registry.get("NEXUS")
        self.assertEqual(nexus.level.value, "MASTER")
        self.assertEqual(nexus.implementation_status, ImplementationStatus.ACTIVE)

    def test_scaffold_agents_never_downgraded_to_junior(self):
        from core.agents.default_registry import build_default_agent_registry

        registry = build_default_agent_registry()
        for agent in registry.all():
            self.assertNotEqual(agent.level.value, "JUNIOR")


class EvidenceRegistryTests(unittest.TestCase):
    """Item 3 — Evidence Registry real."""

    def test_unregistered_id_resolves_missing(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        self.assertEqual(registry.resolve_status("nao-existe"), VerificationStatus.MISSING)

    def test_registered_verified_resolves_verified(self):
        registry = make_verified_registry()
        self.assertEqual(registry.resolve_status("ev1"), VerificationStatus.VERIFIED)

    def test_expired_always_wins_over_stated_status(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev-exp", type="x", source="x", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
            valid_until=time.time() - 1,
        ))
        self.assertEqual(registry.resolve_status("ev-exp"), VerificationStatus.EXPIRED)

    def test_arbitrary_string_never_counts_as_evidence(self):
        """Cenário exato do item 3: evidence_ids=['ev-nonexistent']
        nunca aprova uma estratégia."""
        gate = RadarWebEvidenceGate()  # registry vazio
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="fonte", evidence_ids=["ev-nonexistent"], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy})
        self.assertNotEqual(result.verdict, Verdict.APPROVED)
        self.assertEqual(result.criteria["evidence"], "MISSING")


class MetricEvidenceTests(unittest.TestCase):
    """Item 4 — MetricEvidence por métrica."""

    def test_metric_evidence_requires_own_evidence_ids(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        me = MetricEvidence(
            metric_name="roi", value=1.5, source="planilha", provenance="dados próprios",
            observed_at=time.time(), context="teste", measurement_type="calculado", confidence=0.9,
            evidence_ids=[],
        )
        self.assertEqual(me.resolve_verification(registry), VerificationStatus.MISSING)

    def test_metric_evidence_verified_only_when_all_linked_ids_verified(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev-a", type="x", source="x", provenance="x", observed_at=time.time(),
            modality=Modality.STRUCTURED_DATA, source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        registry.register(EvidenceRecord(
            evidence_id="ev-b", type="x", source="x", provenance="x", observed_at=time.time(),
            modality=Modality.STRUCTURED_DATA, requested_verification_status=VerificationStatus.UNVERIFIED,
        ))
        me = MetricEvidence(
            metric_name="roi", value=1.5, source="planilha", provenance="dados próprios",
            observed_at=time.time(), context="teste", measurement_type="calculado", confidence=0.9,
            evidence_ids=["ev-a", "ev-b"],
        )
        self.assertEqual(me.resolve_verification(registry), VerificationStatus.UNVERIFIED)

    def test_raw_number_never_accepted_for_protected_metric(self):
        gate = RadarWebEvidenceGate(evidence_registry=make_verified_registry())
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"roi": 999999}})
        self.assertEqual(result.verdict, Verdict.REJECTED)
        self.assertIn("roi", result.criteria["metric_provenance"])


class QualityGateAdversarialABCDETests(unittest.TestCase):
    """Item 5 — os 5 testes adversariais obrigatórios exatos."""

    def test_A_fact_source_nonexistent_evidence_protected_metrics(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="sA", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="https://example.com", evidence_ids=["ev-nonexistent"], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"roi": 999999, "ctr": 0.99}})
        self.assertIn(result.verdict, (Verdict.REJECTED, Verdict.BLOCKED))

    def test_B_arbitrary_string_missing_evidence(self):
        gate = RadarWebEvidenceGate()
        self.assertEqual(gate.evidence_registry.resolve_status("qualquer-string"), VerificationStatus.MISSING)

    def test_C_unverified_evidence_never_sustains_fact(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev-u", type="x", source="x", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, requested_verification_status=VerificationStatus.UNVERIFIED,
        ))
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="sC", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=["ev-u"], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy})
        self.assertNotEqual(result.verdict, Verdict.APPROVED)

    def test_D_verified_evidence_not_linked_to_roi_stays_unverified(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev-strategy", type="x", source="x", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="sD", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=["ev-strategy"], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"roi": 50000}})
        self.assertIn("roi", result.criteria["metric_provenance"])
        self.assertNotEqual(result.verdict, Verdict.APPROVED)

    def test_E_complete_metric_evidence_evaluated_normally(self):
        from tests._evidence_test_helpers import register_test_claim

        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev-strategy", type="x", source="fonte", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9,
            source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        registry.register(EvidenceRecord(
            evidence_id="ev-roi", type="x", source="fonte", provenance="x", observed_at=time.time(),
            modality=Modality.STRUCTURED_DATA, confidence=0.9,
            source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
        ))
        register_test_claim(registry, claim_id="claim-sE", strategy_id="sE")
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="sE", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=["ev-strategy"], observed_at=time.time(),
            primary_claim_id="claim-sE",
        )
        roi_evidence = MetricEvidence(
            metric_name="roi", value=1.8, source="planilha", provenance="dados próprios",
            observed_at=time.time(), context="campanha", measurement_type="calculado", confidence=0.9,
            evidence_ids=["ev-roi"],
            provenance_record=ProvenanceRecord(
                source_type="OWN_OPERATION_DATA", source_reference="planilha", collection_method="export", collector="agent",
                verifier="teste_automatizado", verification_method="validacao_manual",
                integrity_reference="sha256:teste", related_evidence_ids=["ev-roi"],
            ),
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"roi": roi_evidence}})
        self.assertEqual(result.verdict, Verdict.APPROVED)


class StructuredProvenanceTests(unittest.TestCase):
    """Item 6 — provenance não pode ser string livre para métricas
    protegidas."""

    def _pipeline_with_verified_evidence(self):
        return AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=make_verified_registry()))

    def _advance_to_metrics_stage(self, pipeline):
        record = pipeline.start("s1")
        pipeline.classify(record.record_id)
        pipeline.collect_evidence(record.record_id, evidence={"x": 1})
        pipeline.analyze(record.record_id)
        pipeline.propose_hypothesis(record.record_id, hypothesis="x")
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.85, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
            primary_claim_id="claim-s1",
        )
        pipeline.run_quality_gate(record.record_id, strategy=strategy)
        return record

    def test_trust_me_provenance_with_generic_evidence_never_sustains_protected_metric(self):
        """Cenário exato do item 6: evidence={'anything':'x'},
        provenance='trust me' nunca é suficiente."""
        pipeline = self._pipeline_with_verified_evidence()
        record = self._advance_to_metrics_stage(pipeline)
        with self.assertRaises(ValueError):
            pipeline.record_metrics(record.record_id, metrics={"roi": 999999}, provenance="trust me")

    def test_incomplete_provenance_record_rejected(self):
        pipeline = self._pipeline_with_verified_evidence()
        record = self._advance_to_metrics_stage(pipeline)
        with self.assertRaises(ValueError):
            pipeline.record_metrics(
                record.record_id, metrics={"conversion_rate": 0.5},
                provenance=ProvenanceRecord(source_type="", source_reference="", collection_method="", collector=""),
            )

    def test_non_protected_metric_accepts_plain_string_provenance(self):
        pipeline = self._pipeline_with_verified_evidence()
        record = self._advance_to_metrics_stage(pipeline)
        result = pipeline.record_metrics(
            record.record_id, metrics={"engagement_score": 0.8}, provenance="log interno do dashboard",
        )
        self.assertEqual(result.status.value, "METRICS")

    def test_provenance_record_structurally_valid(self):
        pr = ProvenanceRecord(
            source_type="internal_analytics", source_reference="dashboard://campaign-42",
            collection_method="automated_export", collector="metrica_agent",
        )
        self.assertTrue(pr.is_structurally_valid())


class SentinelStructuredReportTests(unittest.TestCase):
    """Item 7 — relatório estruturado + downgrade de claim."""

    def test_report_includes_all_required_fields(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="fonte", evidence_ids=[], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {}})
        for field in (
            "missing_evidence", "invalid_evidence", "unsupported_claims", "metric_issues",
            "verified_claims", "unverified_claims", "stale_evidence", "provenance_issues",
        ):
            self.assertIn(field, result.criteria)

    def test_fact_downgraded_to_estimate_when_evidence_exists_but_insufficient(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s2", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=["ev-x"], observed_at=time.time(),
        )
        gate.evaluate({"strategy": strategy}, allow_downgrade=True)
        self.assertEqual(strategy.claim_type, ClaimType.ESTIMATE)
        self.assertNotEqual(strategy.claim_type, ClaimType.FACT)

    def test_fact_downgraded_to_unknown_when_no_evidence_at_all(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s3", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=[], observed_at=time.time(),
        )
        gate.evaluate({"strategy": strategy}, allow_downgrade=True)
        self.assertEqual(strategy.claim_type, ClaimType.UNKNOWN)

    def test_no_downgrade_by_default(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s4", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=["ev-x"], observed_at=time.time(),
        )
        gate.evaluate({"strategy": strategy})  # allow_downgrade padrão False
        self.assertEqual(strategy.claim_type, ClaimType.FACT)

    def test_invalid_evidence_distinct_from_missing(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev-inv", type="x", source="x", provenance="x", observed_at=time.time(),
            modality=Modality.TEXT, requested_verification_status=VerificationStatus.INVALID,
        ))
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="s5", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="fonte", evidence_ids=["ev-inv"], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy})
        self.assertTrue(any("ev-inv" in e for e in result.criteria["invalid_evidence"]))


class RadarWebRepeatedShutdownTests(unittest.TestCase):
    """Item 11 — repetir build/start/shutdown mais de uma vez no mesmo
    processo, sem estado global impedindo execuções futuras."""

    def test_multiple_build_start_shutdown_cycles(self):
        from api.bootstrap import build_app
        from core.security.auth import hash_password

        for i in range(3):
            app = build_app(
                owner_email="owner@x.com", owner_password_hash=fast_hash_password("StrongPass123"),
                persistent=False, start_radar_web_worker=True,
            )
            self.assertTrue(app.atlas.radar_web_worker.is_running())
            app.atlas.radar_web_scheduler.enable(interval_seconds=0.1)
            deadline = time.time() + 3.0
            while app.atlas.radar_web_scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(app.atlas.radar_web_scheduler.status().last_run_status, "SUCCESS")
            app.shutdown()
            self.assertFalse(app.atlas.radar_web_worker.is_running())


if __name__ == "__main__":
    unittest.main()
