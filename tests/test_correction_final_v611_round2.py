import time
import unittest
from tests._evidence_test_helpers import make_test_verifier_registry, make_test_source_registry

from core.evidence.claims import ClaimType
from core.evidence.metric_evidence import MetricEvidence
from core.evidence.provenance import ProvenanceRecord
from core.evidence.registry import EvidenceRecord, EvidenceRegistry, EvidenceScope, VerificationStatus
from core.multimodal.evidence import Modality
from core.quality.gates import Verdict
from radar_web.adoption import (
    AdoptionPipeline, AdoptionStatus, AdoptionValidationFailed, MetricValidationFailed, QualityGateRejection,
)
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy
from radar_web.quality_gate import RadarWebEvidenceGate

"""
Testes CORRECAO FINAL V6.1.1 ROUND2 - itens 1-11.
"""

OFFICIAL_ROLES = {
    "ATLAS": "Diretor Operacional MASTER, orquestração geral.",
    "ORION": "Radar permanente de mercados, tendências, nichos e oportunidades.",
    "SAGE": "Pesquisa de público, concorrência, dores, desejos e comportamento.",
    "PRISMA": "Inteligência de dados, padrões e oportunidades.",
    "LIBRA": "Curadoria da BIBLIOTECA MESTRE, conhecimento e aprendizado validado.",
    "MERCURY": "MultiGerenciador Comercial, vendas multicanal e monetização.",
    "VENTURE": "Descoberta de produtos físicos, infoprodutos, afiliados, serviços e oportunidades comerciais.",
    "NEXUS": "Funis, ofertas, CTAs, upsell, downsell, order bump e conversão.",
    "RELAY": "Leads, CRM, relacionamento, remarketing e recuperação de oportunidades.",
    "ARGOS": "Estratégia de conteúdo e planejamento editorial multicanal.",
    "SCRIPTOR": "Roteiros, textos, copy e storytelling.",
    "VERITAS": "Pesquisa factual, checagem, fontes e confiabilidade.",
    "AURORA": "Direção criativa e conceito visual.",
    "FRAME": "Produção/montagem/edição de vídeo 2D/3D/híbrido.",
    "VOX": "Narração humanizada, emoção, ritmo e vozes.",
    "SONORA": "Música, efeitos, áudio, mixagem e sincronização.",
    "PIXEL": "Thumbnails, imagens, criativos e peças visuais.",
    "PULSE": "SEO, distribuição, publicação e crescimento multicanal.",
    "BRIDGE": "MULTIVERSO ACCESS & EXECUTION HUB, conexões e integrações.",
    "SENTINEL": "Quality Gates e controle de qualidade.",
    "JURIS": "Copyright, licenças, políticas e compliance.",
    "METRICA": "Métricas multidimensionais, receita, CAC, LTV, ROI, margem e performance.",
    "ECHO": "Aprendizado contínuo a partir de resultados.",
    "NAVIGATOR": "Executor operacional externo multicanal.",
    "FORGE": "Manutenção, diagnóstico, reparo, confiabilidade e recuperação estrutural.",
}


def make_verified_record(*, evidence_id, strategy_id=None, metric_name=None, confidence=0.9, scope=None):
    kwargs = dict(
        evidence_id=evidence_id, type="x", source="fonte_real", provenance="coleta",
        observed_at=time.time(), modality=Modality.DOCUMENT, confidence=confidence,
        related_strategy_id=strategy_id, related_metric_name=metric_name,
        source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED,
        verifier="teste_automatizado", verification_method="validacao_manual",
    )
    if scope is not None:
        kwargs["scope"] = scope
    return EvidenceRecord(**kwargs)


class EvidenceRecordRealValidationTests(unittest.TestCase):
    """Item 4 — EvidenceRecord VERIFIED nunca é apenas declarativo."""

    def test_D_declarative_verified_rejected(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        record = EvidenceRecord(
            evidence_id="ev-bad", type="x", source="", provenance="", observed_at=0, modality=Modality.TEXT,
            confidence=0, integrity_reference=None, source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED,
        )
        registered = registry.register(record)
        self.assertNotEqual(registered.verification_status, VerificationStatus.VERIFIED)

    def test_requested_status_never_bypasses_validation_via_set_status(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="", provenance="", observed_at=0, modality=Modality.TEXT,
        ))
        result = registry.set_verification_status("ev1", VerificationStatus.VERIFIED)
        self.assertNotEqual(result.verification_status, VerificationStatus.VERIFIED)

    def test_fully_valid_record_grants_verified(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registered = registry.register(make_verified_record(evidence_id="ev-good"))
        self.assertEqual(registered.verification_status, VerificationStatus.VERIFIED)

    def test_missing_verifier_blocks_verified(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        record = EvidenceRecord(
            evidence_id="ev-noverifier", type="x", source="fonte", provenance="prov", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.9, source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED,
            verifier=None, verification_method="m",
        )
        registered = registry.register(record)
        self.assertNotEqual(registered.verification_status, VerificationStatus.VERIFIED)

    def test_zero_confidence_blocks_verified(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        record = EvidenceRecord(
            evidence_id="ev-zero-conf", type="x", source="fonte", provenance="prov", observed_at=time.time(),
            modality=Modality.TEXT, confidence=0.0, source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED,
            verifier="v", verification_method="m",
        )
        registered = registry.register(record)
        self.assertNotEqual(registered.verification_status, VerificationStatus.VERIFIED)


class StrategyMetricBindingTests(unittest.TestCase):
    """Itens 2/3 — binding forte estratégia↔evidência e métrica↔evidência."""

    def test_C_evidence_from_strategy_A_never_accepted_for_B(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record(evidence_id="ev-a", strategy_id="A"))
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy_b = FunnelStrategy(
            strategy_id="B", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="fonte", evidence_ids=["ev-a"], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy_b})
        self.assertEqual(result.criteria["evidence"], "MISSING")
        self.assertTrue(any("STRATEGY_BINDING_ERROR" in c for c in result.criteria["unsupported_claims"]))

    def test_benchmark_scope_allows_cross_strategy_reuse(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record(evidence_id="ev-bench", strategy_id="A", scope=EvidenceScope.BENCHMARK))
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy_c = FunnelStrategy(
            strategy_id="C", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="fonte", evidence_ids=["ev-bench"], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy_c})
        self.assertEqual(result.criteria["evidence"], "PASS")

    def test_B_metric_name_mismatch_binding_error(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record(evidence_id="ev-ctr", metric_name="ctr"))
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=[], observed_at=time.time(),
        )
        roi_bound_to_ctr = MetricEvidence(
            metric_name="roi", value=1.5, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-ctr"],
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"roi": roi_bound_to_ctr}})
        self.assertTrue(any("METRIC_BINDING_ERROR" in m for m in result.criteria["metric_issues"]))
        self.assertNotEqual(result.verdict, Verdict.APPROVED)

    def test_metric_aliases_normalize_to_same_canonical(self):
        from core.evidence.registry import canonical_metric_name

        self.assertEqual(canonical_metric_name("roi"), canonical_metric_name("ROI"))
        self.assertEqual(canonical_metric_name("roi"), canonical_metric_name("return_on_investment"))

    def test_different_metrics_never_merged(self):
        from core.evidence.registry import canonical_metric_name

        self.assertNotEqual(canonical_metric_name("roi"), canonical_metric_name("ctr"))
        self.assertNotEqual(canonical_metric_name("ctr"), canonical_metric_name("cac"))
        self.assertNotEqual(canonical_metric_name("cac"), canonical_metric_name("roas"))

    def test_F_complete_metric_evidence_with_correct_binding_evaluated_normally(self):
        from tests._evidence_test_helpers import register_test_claim

        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record(evidence_id="ev-strategy", strategy_id="sF"))
        registry.register(make_verified_record(evidence_id="ev-roi", strategy_id="sF", metric_name="roi"))
        register_test_claim(registry, claim_id="claim-sF", strategy_id="sF")
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="sF", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=["ev-strategy"], observed_at=time.time(),
            primary_claim_id="claim-sF",
        )
        roi_evidence = MetricEvidence(
            metric_name="ROI", value=1.8, source="planilha", provenance="dados",
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


class ProvenanceRecordTests(unittest.TestCase):
    """Item 5 — ProvenanceRecord com todos os campos mínimos."""

    def test_all_required_fields_present(self):
        pr = ProvenanceRecord(
            source_type="internal_analytics", source_reference="dashboard://x",
            collection_method="automated_export", collector="agent",
        )
        for field_name in (
            "provenance_id", "source_type", "source_reference", "collection_method", "observed_at",
            "collected_at", "collector", "verification_method", "verification_status", "integrity_reference",
            "related_evidence_ids", "related_strategy_id",
        ):
            self.assertTrue(hasattr(pr, field_name))

    def test_E_trust_me_string_never_valid_for_protected_metric(self):
        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(
            evidence_registry=_registry_with_verified_strategy_evidence("s1"),
        ))
        record = _advance_to_metrics(pipeline, "s1")
        with self.assertRaises(MetricValidationFailed):
            pipeline.record_metrics(record.record_id, metrics={"roi": 999999}, provenance="trust me")


def _registry_with_verified_strategy_evidence(strategy_id):
    from tests._evidence_test_helpers import register_test_claim

    registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
    registry.register(make_verified_record(evidence_id="ev1", strategy_id=strategy_id))
    register_test_claim(registry, claim_id=f"claim-{strategy_id}", strategy_id=strategy_id)
    return registry


def _advance_to_metrics(pipeline, strategy_id):
    record = pipeline.start(strategy_id)
    pipeline.classify(record.record_id)
    pipeline.collect_evidence(record.record_id, evidence={"x": 1})
    pipeline.analyze(record.record_id)
    pipeline.propose_hypothesis(record.record_id, hypothesis="x")
    strategy = FunnelStrategy(
        strategy_id=strategy_id, name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
        mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
        confidence=0.85, source="fonte_real", evidence_ids=["ev1"], observed_at=time.time(),
        primary_claim_id=f"claim-{strategy_id}",
    )
    pipeline.run_quality_gate(record.record_id, strategy=strategy)
    return pipeline.get(record.record_id)


class MetricEvidenceNeverReachesAdoptedTests(unittest.TestCase):
    """Item 1 — MetricEvidence inválida nunca chega a ADOPTED."""

    def test_A_nonexistent_evidence_id_blocks_at_record_metrics(self):
        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(
            evidence_registry=_registry_with_verified_strategy_evidence("s1"),
        ))
        record = _advance_to_metrics(pipeline, "s1")
        fake_roi = MetricEvidence(
            metric_name="roi", value=999999, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-nonexistent"],
        )
        with self.assertRaises(MetricValidationFailed):
            pipeline.record_metrics(record.record_id, metrics={"roi": fake_roi}, provenance="teste")
        final = pipeline.get(record.record_id)
        self.assertEqual(final.status, AdoptionStatus.METRIC_UNVERIFIED)

    def test_metric_unverified_state_reachable_and_never_skips_to_adopted(self):
        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(
            evidence_registry=_registry_with_verified_strategy_evidence("s1"),
        ))
        record = _advance_to_metrics(pipeline, "s1")
        fake_roi = MetricEvidence(
            metric_name="roi", value=1, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=[],
        )
        try:
            pipeline.record_metrics(record.record_id, metrics={"roi": fake_roi}, provenance="teste")
        except MetricValidationFailed:
            pass
        final = pipeline.get(record.record_id)
        self.assertEqual(final.status, AdoptionStatus.METRIC_UNVERIFIED)
        with self.assertRaises(Exception):
            pipeline.validate(record.record_id, decision="ADOPTED")

    def test_metric_unverified_can_only_go_to_retest(self):
        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(
            evidence_registry=_registry_with_verified_strategy_evidence("s1"),
        ))
        record = _advance_to_metrics(pipeline, "s1")
        fake_roi = MetricEvidence(
            metric_name="roi", value=1, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=[],
        )
        try:
            pipeline.record_metrics(record.record_id, metrics={"roi": fake_roi}, provenance="teste")
        except MetricValidationFailed:
            pass
        retried = pipeline.retest(record.record_id)
        self.assertEqual(retried.status, AdoptionStatus.CONTROLLED_TEST)


class ValidateRevalidationTests(unittest.TestCase):
    """Item 7 — validate() nunca aceita ADOPTED cegamente."""

    def test_adopted_requires_full_revalidation_success(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record(evidence_id="ev1", strategy_id="s1"))
        registry.register(make_verified_record(evidence_id="ev-roi", strategy_id="s1", metric_name="roi"))
        from tests._evidence_test_helpers import register_test_claim
        register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")
        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=registry))
        record = _advance_to_metrics(pipeline, "s1")
        roi_evidence = MetricEvidence(
            metric_name="roi", value=1.8, source="planilha", provenance="dados",
            observed_at=time.time(), context="campanha", measurement_type="calculado", confidence=0.9,
            evidence_ids=["ev-roi"],
            provenance_record=ProvenanceRecord(
                source_type="OWN_OPERATION_DATA", source_reference="planilha", collection_method="export", collector="agent",
                verifier="teste_automatizado", verification_method="validacao_manual",
                integrity_reference="sha256:teste", related_evidence_ids=["ev-roi"],
            ),
        )
        pipeline.record_metrics(record.record_id, metrics={"roi": roi_evidence}, provenance="teste controlado")
        final = pipeline.validate(record.record_id, decision="ADOPTED")
        self.assertEqual(final.status, AdoptionStatus.ADOPTED)

    def test_tampered_metrics_after_gate_still_caught_at_validate(self):
        """Revalidação em `validate()` — mesmo que `record_metrics()`
        tenha aceitado, uma adulteração posterior do registro é
        pega."""
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record(evidence_id="ev1", strategy_id="s1"))
        registry.register(make_verified_record(evidence_id="ev-roi", strategy_id="s1", metric_name="roi"))
        from tests._evidence_test_helpers import register_test_claim
        register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")
        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=registry))
        record = _advance_to_metrics(pipeline, "s1")
        roi_evidence = MetricEvidence(
            metric_name="roi", value=1.8, source="planilha", provenance="dados",
            observed_at=time.time(), context="campanha", measurement_type="calculado", confidence=0.9,
            evidence_ids=["ev-roi"],
            provenance_record=ProvenanceRecord(
                source_type="OWN_OPERATION_DATA", source_reference="planilha", collection_method="export", collector="agent",
                verifier="teste_automatizado", verification_method="validacao_manual",
                integrity_reference="sha256:teste", related_evidence_ids=["ev-roi"],
            ),
        )
        pipeline.record_metrics(record.record_id, metrics={"roi": roi_evidence}, provenance="teste")
        # simula evidencia revogada DEPOIS de record_metrics, antes de validate()
        registry.set_verification_status("ev-roi", VerificationStatus.INVALID)
        with self.assertRaises(AdoptionValidationFailed):
            pipeline.validate(record.record_id, decision="ADOPTED")
        final = pipeline.get(record.record_id)
        self.assertEqual(final.status, AdoptionStatus.REJECTED)


class EndToEndPipelineTests(unittest.TestCase):
    """Item 9 — pipeline ponta a ponta: cenário inválido e válido."""

    def test_invalid_scenario_blocked(self):
        from tests._evidence_test_helpers import register_test_claim

        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record(evidence_id="ev1", strategy_id="s1"))
        register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")
        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=registry))
        record = _advance_to_metrics(pipeline, "s1")
        fake_roi = MetricEvidence(
            metric_name="roi", value=999999, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-fake"],
        )
        with self.assertRaises(MetricValidationFailed):
            pipeline.record_metrics(record.record_id, metrics={"roi": fake_roi}, provenance="x")
        final = pipeline.get(record.record_id)
        self.assertIn(final.status, (AdoptionStatus.METRIC_UNVERIFIED,))
        self.assertNotEqual(final.status, AdoptionStatus.ADOPTED)

    def test_valid_scenario_reaches_adopted_with_full_trail(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record(evidence_id="ev1", strategy_id="s2"))
        registry.register(make_verified_record(evidence_id="ev-roi", strategy_id="s2", metric_name="roi"))
        from tests._evidence_test_helpers import register_test_claim
        register_test_claim(registry, claim_id="claim-s2", strategy_id="s2")
        pipeline = AdoptionPipeline(quality_gate=RadarWebEvidenceGate(evidence_registry=registry))
        record = _advance_to_metrics(pipeline, "s2")
        roi_evidence = MetricEvidence(
            metric_name="roi", value=1.8, source="planilha", provenance="dados",
            observed_at=time.time(), context="campanha", measurement_type="calculado", confidence=0.9,
            evidence_ids=["ev-roi"],
            provenance_record=ProvenanceRecord(
                source_type="OWN_OPERATION_DATA", source_reference="planilha", collection_method="export", collector="agent",
                verifier="teste_automatizado", verification_method="validacao_manual",
                integrity_reference="sha256:teste", related_evidence_ids=["ev-roi"],
            ),
        )
        pipeline.record_metrics(record.record_id, metrics={"roi": roi_evidence}, provenance="teste controlado real")
        final = pipeline.validate(record.record_id, decision="ADOPTED")
        self.assertEqual(final.status, AdoptionStatus.ADOPTED)
        self.assertIsNotNone(final.quality_gate_result)
        self.assertIsNotNone(final.strategy)
        self.assertEqual(final.strategy.strategy_id, "s2")


class OfficialAgentMatrixTests(unittest.TestCase):
    """Itens 10/11 — matriz oficial restaurada, papel separado de status."""

    def test_all_25_official_roles_exact(self):
        from core.agents.default_registry import build_default_agent_registry

        registry = build_default_agent_registry()
        for agent_id, role in OFFICIAL_ROLES.items():
            self.assertEqual(registry.get(agent_id).official_role, role)

    def test_official_role_never_changes_with_implementation_status(self):
        """CORREÇÃO (BLOCO FINAL 1): NEXUS saiu de SCAFFOLD para ACTIVE
        — a invariante que este teste protege (official_role/
        specialization nunca muda por causa de implementation_status)
        continua válida, agora com o status real e atualizado."""
        from core.agents.base import ImplementationStatus
        from core.agents.default_registry import build_default_agent_registry

        registry = build_default_agent_registry()
        nexus = registry.get("NEXUS")
        self.assertEqual(nexus.implementation_status, ImplementationStatus.ACTIVE)
        self.assertEqual(nexus.official_role, OFFICIAL_ROLES["NEXUS"])

    def test_runtime_capabilities_empty_for_scaffold(self):
        """CORREÇÃO (BLOCO FINAL 1 — item 1): os 7 agentes citados
        aqui (SAGE/LIBRA/ORION/ARGOS/PULSE/RELAY/METRICA) saíram de
        SCAFFOLD — agora têm runtime real (ACTIVE ou PARTIAL) e,
        portanto, `runtime_capabilities` NÃO-vazio. A invariante
        original (SCAFFOLD -> `runtime_capabilities` sempre vazio)
        continua protegida abaixo, verificada diretamente contra a
        propriedade — de forma sintética, já que não há mais nenhum
        agente SCAFFOFLD real no registro padrão (0 restantes)."""
        from core.agents.base import AgentRegistration, ImplementationStatus
        from core.agents.default_registry import build_default_agent_registry
        from core.capabilities.registry import Capability, CompetencyLevel

        registry = build_default_agent_registry()
        for agent_id in ("SAGE", "LIBRA", "ORION", "ARGOS", "PULSE", "RELAY", "METRICA"):
            reg = registry.get(agent_id)
            self.assertIn(reg.implementation_status, (ImplementationStatus.ACTIVE, ImplementationStatus.PARTIAL))
            self.assertGreater(len(reg.runtime_capabilities), 0)

        # invariante original — SCAFFOLD sempre runtime_capabilities=[] —
        # verificada de forma sintética (0 agentes SCAFFOLD reais restam).
        synthetic_scaffold = AgentRegistration(
            agent_id="SYNTHETIC", display_name="x", role="x", specialization="x", level=CompetencyLevel.JUNIOR,
            capabilities=[Capability.REASONING], implementation_status=ImplementationStatus.SCAFFOLD,
        )
        self.assertEqual(synthetic_scaffold.runtime_capabilities, [])

    def test_runtime_capabilities_nonempty_for_active(self):
        from core.agents.default_registry import build_default_agent_registry

        registry = build_default_agent_registry()
        for agent_id in ("ATLAS", "NAVIGATOR", "FORGE"):
            self.assertGreater(len(registry.get(agent_id).runtime_capabilities), 0)


if __name__ == "__main__":
    unittest.main()


class ProvenanceRecordInMetricEvidenceTests(unittest.TestCase):
    """Item 6 — FACT + métrica crítica exige ProvenanceRecord válida
    quando fornecida."""

    def test_invalid_provenance_record_blocks_metric(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record(evidence_id="ev-roi", metric_name="roi"))
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=[], observed_at=time.time(),
        )
        bad_provenance = ProvenanceRecord(source_type="", source_reference="", collection_method="", collector="")
        me = MetricEvidence(
            metric_name="roi", value=1.5, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-roi"],
            provenance_record=bad_provenance,
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"roi": me}})
        self.assertNotEqual(result.verdict, Verdict.APPROVED)

    def test_valid_provenance_record_allows_normal_evaluation(self):
        from tests._evidence_test_helpers import register_test_claim

        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(make_verified_record(evidence_id="ev-strategy", strategy_id="s1"))
        registry.register(make_verified_record(evidence_id="ev-roi", strategy_id="s1", metric_name="roi"))
        register_test_claim(registry, claim_id="claim-s1", strategy_id="s1")
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=["ev-strategy"], observed_at=time.time(),
            primary_claim_id="claim-s1",
        )
        good_provenance = ProvenanceRecord(
            source_type="OWN_OPERATION_DATA", source_reference="planilha", collection_method="export", collector="agent",
            verifier="teste_automatizado", verification_method="validacao_manual",
            integrity_reference="sha256:teste", related_evidence_ids=["ev-roi"],
        )
        me = MetricEvidence(
            metric_name="roi", value=1.5, source="x", provenance="x", observed_at=time.time(),
            context="x", measurement_type="x", confidence=0.9, evidence_ids=["ev-roi"],
            provenance_record=good_provenance,
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"roi": me}})
        self.assertEqual(result.verdict, Verdict.APPROVED)
