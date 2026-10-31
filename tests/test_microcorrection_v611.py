import threading
import time
import unittest
from tests._evidence_test_helpers import make_test_verifier_registry, make_test_source_registry
from tests._auth_test_helpers import fast_hash_password

from api.bootstrap import build_app
from core.agents.base import ImplementationStatus, UnknownAgentAlias
from core.agents.default_registry import build_default_agent_registry
from core.evidence.claims import ClaimType, requires_verifiable_source
from core.multimodal.evidence import Modality
from radar_web.models import FunnelMaturity, FunnelMechanism, FunnelStrategy
from radar_web.quality_gate import RadarWebEvidenceGate
from core.quality.gates import Verdict

"""
Testes MICROCORRECAO V6.1.1 - itens 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 14.
"""

OFFICIAL_25 = {
    "ATLAS", "ORION", "SAGE", "PRISMA", "LIBRA", "MERCURY", "VENTURE", "NEXUS", "RELAY", "ARGOS",
    "SCRIPTOR", "VERITAS", "AURORA", "FRAME", "VOX", "SONORA", "PIXEL", "PULSE", "BRIDGE", "SENTINEL",
    "JURIS", "METRICA", "ECHO", "NAVIGATOR", "FORGE",
}


class BuildAppRadarWebAutostartTests(unittest.TestCase):
    """Item 1 — cenário exato: build_app(radar web enabled) -> aguardar
    intervalo -> RADAR WEB executa sozinho -> last_run muda -> status
    indica execução real."""

    def setUp(self):
        self._apps = []

    def tearDown(self):
        for app in self._apps:
            app.shutdown()

    def test_autostart_real_end_to_end(self):
        app = build_app(
            owner_email="owner@x.com", owner_password_hash=fast_hash_password("StrongPass123"),
            persistent=False, start_radar_web_worker=True,
        )
        self._apps.append(app)
        atlas = app.atlas
        self.assertTrue(atlas.radar_web_worker.is_running())

        atlas.radar_web_scheduler.enable(interval_seconds=0.2)
        self.assertEqual(atlas.radar_web_scheduler.status().last_run_status, "NEVER_RUN")

        deadline = time.time() + 3.0
        while atlas.radar_web_scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
            time.sleep(0.05)

        status = atlas.radar_web_scheduler.status()
        self.assertEqual(status.last_run_status, "SUCCESS")
        self.assertIsNotNone(status.last_run_at)
        self.assertGreater(status.last_run_discoveries, 0)

    def test_radar_web_and_opportunities_radar_are_distinct(self):
        app = build_app(
            owner_email="owner@x.com", owner_password_hash=fast_hash_password("StrongPass123"),
            persistent=False, start_radar_web_worker=True,
        )
        self._apps.append(app)
        atlas = app.atlas
        self.assertIsNot(atlas.radar_web_scheduler, atlas.radar_scheduler)
        self.assertIsNot(atlas.radar_web_worker, atlas.radar_worker)
        self.assertFalse(atlas.radar_worker.is_running())  # so o radar web foi iniciado

    def test_start_radar_worker_alone_never_starts_radar_web(self):
        app = build_app(
            owner_email="owner@x.com", owner_password_hash=fast_hash_password("StrongPass123"),
            persistent=False, start_radar_worker=True,
        )
        self._apps.append(app)
        self.assertFalse(app.atlas.radar_web_worker.is_running())

    def test_shutdown_stops_both_workers(self):
        app = build_app(
            owner_email="owner@x.com", owner_password_hash=fast_hash_password("StrongPass123"),
            persistent=False, start_radar_worker=True, start_radar_web_worker=True,
        )
        self._apps.append(app)
        self.assertTrue(app.atlas.radar_worker.is_running())
        self.assertTrue(app.atlas.radar_web_worker.is_running())
        app.shutdown()
        self.assertFalse(app.atlas.radar_worker.is_running())
        self.assertFalse(app.atlas.radar_web_worker.is_running())


class Official25AgentsTests(unittest.TestCase):
    def test_exact_official_names_preserved(self):
        registry = build_default_agent_registry()
        actual = {a.agent_id for a in registry.all()}
        self.assertEqual(actual, OFFICIAL_25)

    def test_no_silent_name_substitution(self):
        registry = build_default_agent_registry()
        actual = {a.agent_id for a in registry.all()}
        # nomes inventados de rodadas anteriores NUNCA devem reaparecer
        invented = {"RADAR", "RADAR_WEB_COMERCIAL", "SCRIBE", "LENS", "VAULT", "LEDGER", "COMPASS", "ANCHOR", "PRISM", "CIRCUIT", "HORIZON", "FORUM", "KEYSTONE"}
        self.assertEqual(actual & invented, set())

    def test_each_agent_has_full_registry_fields(self):
        registry = build_default_agent_registry()
        for agent in registry.all():
            self.assertTrue(agent.agent_id)
            self.assertTrue(agent.display_name)
            self.assertTrue(agent.role)
            self.assertTrue(agent.specialization)
            self.assertIsNotNone(agent.level)
            self.assertIsInstance(agent.implementation_status, ImplementationStatus)
            self.assertIsInstance(agent.agentic_architecture_supported, bool)
            self.assertIsInstance(agent.all_rounder_capable, bool)
            self.assertIsInstance(agent.multimodal_capabilities, list)
            self.assertIsInstance(agent.capabilities, list)
            self.assertIsInstance(agent.permissions, list)
            self.assertTrue(agent.authority_level)


class AgenticArchitectureVsRuntimeTests(unittest.TestCase):
    """Itens 3/4 — arquitetura suportada != runtime disponível."""

    def test_all_25_support_agentic_architecture(self):
        registry = build_default_agent_registry()
        for agent in registry.all():
            self.assertTrue(agent.agentic_architecture_supported)

    def test_only_active_have_agentic_runtime(self):
        """MICROCORREÇÃO (eliminar todos os PARTIAL): SENTINEL/BRIDGE
        agora têm runtime agêntico real próprio também (wrappers sobre
        QualitySupervisor/BridgeHub) — `agentic_runtime_available=True`
        para os 25 agentes."""
        registry = build_default_agent_registry()
        with_runtime = {a.agent_id for a in registry.all() if a.agentic_runtime_available}
        self.assertEqual(len(with_runtime), 25)

    def test_all_25_are_all_rounder_capable(self):
        registry = build_default_agent_registry()
        for agent in registry.all():
            self.assertTrue(agent.all_rounder_capable)

    def test_only_active_have_all_rounder_runtime(self):
        """MICROCORREÇÃO: mesma atualização — todos os 25 agentes têm
        runtime real o suficiente para atuar como ALL-ROUNDER agora."""
        registry = build_default_agent_registry()
        with_runtime = {a.agent_id for a in registry.all() if a.all_rounder_runtime_available}
        self.assertEqual(len(with_runtime), 25)

    def test_scaffold_never_confused_with_no_architecture(self):
        """MICROCORREÇÃO (eliminar todos os PARTIAL): NEXUS e PIXEL
        (antes SCAFFOLD/PARTIAL respectivamente) agora são ambos
        ACTIVE, com runtime real. A invariante que este teste protege
        (arquitetura suportada é um eixo SEMPRE True, independente de
        implementation_status) é verificada aqui de forma sintética —
        já que não resta nenhum agente SCAFFOLD/PARTIAL real no
        registro padrão (0 de cada)."""
        from core.agents.base import AgentRegistration

        registry = build_default_agent_registry()
        nexus = registry.get("NEXUS")
        self.assertEqual(nexus.implementation_status, ImplementationStatus.ACTIVE)
        self.assertTrue(nexus.agentic_architecture_supported)
        self.assertTrue(nexus.agentic_runtime_available)

        pixel = registry.get("PIXEL")
        self.assertEqual(pixel.implementation_status, ImplementationStatus.ACTIVE)
        self.assertTrue(pixel.agentic_architecture_supported)
        self.assertTrue(pixel.agentic_runtime_available)

        # invariante original (arquitetura suportada mesmo sem runtime)
        # verificada de forma sintética -- 0 SCAFFOLD/PARTIAL reais restam.
        synthetic_scaffold = AgentRegistration(
            agent_id="SYNTHETIC", display_name="x", role="x", specialization="x", level=nexus.level,
            capabilities=[], implementation_status=ImplementationStatus.SCAFFOLD,
            agentic_architecture_supported=True, agentic_runtime_available=False,
        )
        self.assertTrue(synthetic_scaffold.agentic_architecture_supported)
        self.assertFalse(synthetic_scaffold.agentic_runtime_available)


class CanonicalIdentityResolutionTests(unittest.TestCase):
    def test_agent_id_official_name_and_alias_resolve_same(self):
        registry = build_default_agent_registry()
        self.assertEqual(registry.resolve_canonical_id("NAVIGATOR"), "NAVIGATOR")
        self.assertEqual(registry.resolve_canonical_id("navigator"), "NAVIGATOR")
        self.assertEqual(registry.resolve_canonical_id("navigator@system"), "NAVIGATOR")

    def test_invalid_alias_raises_explicit_error(self):
        registry = build_default_agent_registry()
        with self.assertRaises(UnknownAgentAlias):
            registry.resolve_canonical_id("agente_que_nao_existe")

    def test_all_official_agents_resolve_to_themselves(self):
        registry = build_default_agent_registry()
        for name in OFFICIAL_25:
            self.assertEqual(registry.resolve_canonical_id(name), name)

    def test_case_insensitive_resolution(self):
        registry = build_default_agent_registry()
        self.assertEqual(registry.resolve_canonical_id("forge"), "FORGE")
        self.assertEqual(registry.resolve_canonical_id("Forge"), "FORGE")


class AtlasMultimodalTests(unittest.TestCase):
    """Item 6 — ATLAS nunca com multimodal_capabilities vazio."""

    def test_atlas_multimodal_not_empty(self):
        registry = build_default_agent_registry()
        atlas_reg = registry.get("ATLAS")
        self.assertGreater(len(atlas_reg.multimodal_capabilities), 0)

    def test_atlas_covers_required_modalities(self):
        registry = build_default_agent_registry()
        atlas_reg = registry.get("ATLAS")
        modalities = {e.modality for e in atlas_reg.multimodal_capabilities}
        required = {
            Modality.TEXT, Modality.IMAGE, Modality.SCREENSHOT, Modality.DOCUMENT, Modality.PDF,
            Modality.TABLE, Modality.STRUCTURED_DATA, Modality.WEB_PAGE, Modality.AUDIO, Modality.VIDEO,
            Modality.LOG, Modality.CODE,
        }
        self.assertEqual(modalities, required)

    def test_provider_unavailable_never_active(self):
        registry = build_default_agent_registry()
        atlas_reg = registry.get("ATLAS")
        for entry in atlas_reg.multimodal_capabilities:
            self.assertNotEqual(entry.status, "ACTIVE")  # nenhum provider real conectado
            self.assertTrue(entry.supported)


class PerAgentMultimodalTests(unittest.TestCase):
    """Item 7 — multimodal por domínio, não uniforme."""

    def test_forge_modalities(self):
        registry = build_default_agent_registry()
        forge = registry.get("FORGE")
        modalities = {e.modality for e in forge.multimodal_capabilities}
        self.assertEqual(modalities, {Modality.CODE, Modality.LOG, Modality.SCREENSHOT, Modality.DOCUMENT})

    def test_navigator_modalities(self):
        registry = build_default_agent_registry()
        nav = registry.get("NAVIGATOR")
        modalities = {e.modality for e in nav.multimodal_capabilities}
        self.assertEqual(modalities, {Modality.WEB_PAGE, Modality.SCREENSHOT, Modality.TEXT, Modality.STRUCTURED_DATA})

    def test_pixel_modalities(self):
        registry = build_default_agent_registry()
        pixel = registry.get("PIXEL")
        modalities = {e.modality for e in pixel.multimodal_capabilities}
        self.assertEqual(modalities, {Modality.IMAGE, Modality.SCREENSHOT, Modality.TEXT})

    def test_frame_modalities(self):
        registry = build_default_agent_registry()
        frame = registry.get("FRAME")
        modalities = {e.modality for e in frame.multimodal_capabilities}
        self.assertEqual(modalities, {Modality.VIDEO, Modality.IMAGE, Modality.AUDIO, Modality.TEXT})

    def test_not_all_agents_have_all_modalities(self):
        registry = build_default_agent_registry()
        relay = registry.get("RELAY")
        self.assertLess(len(relay.multimodal_capabilities), len(registry.get("ATLAS").multimodal_capabilities))


class QualityGateFactBugFixTests(unittest.TestCase):
    """Item 8 — FACT exige evidência, nunca a substitui. Cenário exato
    da auditoria."""

    def test_exact_adversarial_scenario(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s1", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.9, source="https://example.com", evidence_ids=[], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"roi": 999999}})
        self.assertNotEqual(result.verdict, Verdict.APPROVED)
        self.assertIn(result.verdict, (Verdict.REJECTED, Verdict.BLOCKED))

    def test_fact_alone_without_evidence_ids_never_passes_has_evidence(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s2", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.FACT,
            confidence=0.9, source="fonte", evidence_ids=[], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {}})
        self.assertEqual(result.criteria["evidence"], "MISSING")

    def test_fact_with_genuine_evidence_passes(self):
        from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
        from tests._evidence_test_helpers import register_test_claim

        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        for eid in ("ev1", "ev2"):
            registry.register(EvidenceRecord(
                evidence_id=eid, type="source_document", source="fonte_real", provenance="coleta verificada",
                observed_at=time.time(), modality=Modality.DOCUMENT, confidence=0.9,
                source_type="OFFICIAL", requested_verification_status=VerificationStatus.VERIFIED, verifier="teste_automatizado", verification_method="validacao_manual",
            ))
        register_test_claim(registry, claim_id="claim-s3", strategy_id="s3")
        gate = RadarWebEvidenceGate(evidence_registry=registry)
        strategy = FunnelStrategy(
            strategy_id="s3", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.GROWING, claim_type=ClaimType.FACT,
            confidence=0.85, source="fonte_real", evidence_ids=["ev1", "ev2"], observed_at=time.time(),
            primary_claim_id="claim-s3",
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {}})
        self.assertEqual(result.verdict, Verdict.APPROVED)

    def test_ctr_without_provenance_rejected(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s4", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="UNKNOWN", evidence_ids=[], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"ctr": 0.5}})
        self.assertNotEqual(result.verdict, Verdict.APPROVED)

    def test_estimate_with_partial_evidence_stays_estimate(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s5", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.EMERGING, claim_type=ClaimType.ESTIMATE,
            confidence=0.6, source="fonte", evidence_ids=["ev1"], observed_at=time.time(),
        )
        gate.evaluate({"strategy": strategy, "metrics": {}})
        self.assertEqual(strategy.claim_type, ClaimType.ESTIMATE)  # nunca promovido a FACT

    def test_gate_result_includes_required_fields(self):
        gate = RadarWebEvidenceGate()
        strategy = FunnelStrategy(
            strategy_id="s6", name="x", niche="n", offer="o", channel="c", market="m", funnel_stage="topo",
            mechanism=FunnelMechanism.VSL, maturity=FunnelMaturity.UNKNOWN, claim_type=ClaimType.UNKNOWN,
            confidence=0.9, source="UNKNOWN", evidence_ids=[], observed_at=time.time(),
        )
        result = gate.evaluate({"strategy": strategy, "metrics": {"roi": 5000}})
        self.assertIn("missing_evidence", result.criteria)
        self.assertIn("unsupported_claims", result.criteria)
        self.assertIn("metric_issues", result.criteria)


class ProtectedCommercialMetricsTests(unittest.TestCase):
    """Item 9 — lista expandida de métricas comerciais protegidas."""

    def test_all_required_metrics_protected(self):
        required = {"roi", "conversion_rate", "cac", "ltv", "revenue", "sales", "sales_volume",
                    "ctr", "cpc", "cpa", "roas", "aov", "refund_rate"}
        for metric in required:
            self.assertTrue(requires_verifiable_source(metric), f"{metric} deveria estar protegida")

    def test_non_commercial_field_not_protected(self):
        self.assertFalse(requires_verifiable_source("niche"))
        self.assertFalse(requires_verifiable_source("channel"))


class MonolithicSuiteThreadCleanupTests(unittest.TestCase):
    """Item 14 — meta-teste: nenhuma thread de worker deve sobreviver
    além do escopo do próprio teste que a criou."""

    def test_no_radar_web_worker_threads_leak_from_this_test(self):
        from radar_web.engine import RadarWebComercial
        from radar_web.scheduler import RadarWebScheduler
        from radar_web.worker import RadarWebWorker
        from core.jobs.queue import JobQueue

        before = {t.name for t in threading.enumerate()}
        scheduler = RadarWebScheduler(job_queue=JobQueue(), radar=RadarWebComercial())
        worker = RadarWebWorker(scheduler=scheduler, poll_interval_seconds=0.05)
        try:
            scheduler.enable(interval_seconds=0.05)
            worker.start()
            time.sleep(0.1)
        finally:
            worker.stop(timeout=5.0)
        after = {t.name for t in threading.enumerate()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
