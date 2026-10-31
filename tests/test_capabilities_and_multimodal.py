import unittest

from core.capabilities.registry import Capability, CapabilityHealth, CapabilityRegistry, CompetencyLevel
from core.evidence.claims import ClaimType, requires_verifiable_source
from core.multimodal.evidence import Modality, MultimodalEvidence
from core.multimodal.models import MockModalityAnalyzer, MultimodalReasoner

"""
Testes CAPABILITY REGISTRY + MULTIMODAL.
"""


class CapabilityRegistryTests(unittest.TestCase):
    def test_unregistered_capability_never_available(self):
        registry = CapabilityRegistry()
        self.assertFalse(registry.is_available(agent="ATLAS", capability=Capability.CODE))

    def test_registered_but_not_authorized_not_usable(self):
        registry = CapabilityRegistry()
        registry.register(agent="ATLAS", capability=Capability.REASONING, level=CompetencyLevel.MASTER, granted_by="Owner")
        self.assertFalse(registry.is_available(agent="ATLAS", capability=Capability.REASONING))

    def test_unknown_health_never_treated_as_usable_even_with_available_authorized(self):
        """CORREÇÃO item 7 — cenário exato do bug: available=True,
        authorized=True, health=UNKNOWN NUNCA pode ser considerada
        utilizável (o bug anterior só bloqueava DOWN)."""
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.CODE, level=CompetencyLevel.SENIOR, granted_by="Owner",
            provider="internal", available=True, authorized=True, health=CapabilityHealth.UNKNOWN,
        )
        self.assertFalse(registry.is_available(agent="ATLAS", capability=Capability.CODE))

    def test_healthy_is_usable(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.CODE, level=CompetencyLevel.SENIOR, granted_by="Owner",
            provider="internal", available=True, authorized=True, health=CapabilityHealth.HEALTHY,
        )
        self.assertTrue(registry.is_available(agent="ATLAS", capability=Capability.CODE))

    def test_unhealthy_always_blocked(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.CODE, level=CompetencyLevel.SENIOR, granted_by="Owner",
            provider="internal", available=True, authorized=True, health=CapabilityHealth.UNHEALTHY,
        )
        self.assertFalse(registry.is_available(agent="ATLAS", capability=Capability.CODE))

    def test_disabled_always_blocked(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.CODE, level=CompetencyLevel.SENIOR, granted_by="Owner",
            provider="internal", available=True, authorized=True, health=CapabilityHealth.DISABLED,
        )
        self.assertFalse(registry.is_available(agent="ATLAS", capability=Capability.CODE))

    def test_degraded_blocked_by_default_without_policy(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.CODE, level=CompetencyLevel.SENIOR, granted_by="Owner",
            provider="internal", available=True, authorized=True, health=CapabilityHealth.DEGRADED,
        )
        self.assertFalse(registry.is_available(agent="ATLAS", capability=Capability.CODE))

    def test_degraded_usable_only_with_explicit_allow_degraded_policy(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.CODE, level=CompetencyLevel.SENIOR, granted_by="Owner",
            provider="internal", available=True, authorized=True, health=CapabilityHealth.DEGRADED,
        )
        self.assertTrue(registry.is_available(agent="ATLAS", capability=Capability.CODE, allow_degraded=True))

    def test_health_probe_updates_state(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.CODE, level=CompetencyLevel.SENIOR, granted_by="Owner",
            provider="internal", available=True, authorized=True, health=CapabilityHealth.UNKNOWN,
        )
        self.assertFalse(registry.is_available(agent="ATLAS", capability=Capability.CODE))
        registry.record_health_check(agent="ATLAS", capability=Capability.CODE, health=CapabilityHealth.HEALTHY, success=True)
        self.assertTrue(registry.is_available(agent="ATLAS", capability=Capability.CODE))

    def test_health_probe_records_failure_reason(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.CODE, level=CompetencyLevel.SENIOR, granted_by="Owner",
            provider="internal", available=True, authorized=True,
        )
        reg = registry.record_health_check(
            agent="ATLAS", capability=Capability.CODE, health=CapabilityHealth.UNHEALTHY, success=False,
            failure_reason="timeout no provider",
        )
        self.assertEqual(reg.failure_reason, "timeout no provider")
        self.assertIsNotNone(reg.last_checked)
        self.assertIsNone(reg.last_success)

    def test_mock_provider_never_reports_active_status(self):
        """CORREÇÃO item 8 — multimodal: provider MOCK nunca aparece
        como ACTIVE, mesmo com available/authorized/health perfeitos."""
        registry = CapabilityRegistry()
        reg = registry.register(
            agent="ATLAS", capability=Capability.MULTIMODAL_VIDEO, level=CompetencyLevel.SENIOR,
            granted_by="Owner", provider="MOCK", available=True, authorized=True,
            health=CapabilityHealth.HEALTHY,
        )
        self.assertEqual(reg.effective_status.value, "MOCK")

    def test_real_provider_healthy_reports_active(self):
        registry = CapabilityRegistry()
        reg = registry.register(
            agent="ATLAS", capability=Capability.MULTIMODAL_VIDEO, level=CompetencyLevel.SENIOR,
            granted_by="Owner", provider="real_vision_provider", available=True, authorized=True,
            health=CapabilityHealth.HEALTHY,
        )
        self.assertEqual(reg.effective_status.value, "ACTIVE")

    def test_unavailable_never_reports_active_or_mock(self):
        registry = CapabilityRegistry()
        reg = registry.register(
            agent="ATLAS", capability=Capability.MULTIMODAL_VIDEO, level=CompetencyLevel.SENIOR, granted_by="Owner",
        )
        self.assertEqual(reg.effective_status.value, "UNAVAILABLE")

    def test_fully_configured_capability_usable(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.ORCHESTRATION, level=CompetencyLevel.MASTER, granted_by="Owner",
            provider="internal", available=True, authorized=True, health=CapabilityHealth.HEALTHY,
        )
        self.assertTrue(registry.is_available(agent="ATLAS", capability=Capability.ORCHESTRATION))

    def test_down_health_never_usable_even_if_available_authorized(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="NAVIGATOR", capability=Capability.BROWSER, level=CompetencyLevel.SENIOR, granted_by="ATLAS",
            available=True, authorized=True, health=CapabilityHealth.UNHEALTHY,
        )
        self.assertFalse(registry.is_available(agent="NAVIGATOR", capability=Capability.BROWSER))

    def test_capability_never_self_granted_field_tracks_grantor(self):
        registry = CapabilityRegistry()
        reg = registry.register(agent="FORGE", capability=Capability.REPAIR, level=CompetencyLevel.SENIOR, granted_by="ATLAS")
        self.assertEqual(reg.granted_by, "ATLAS")

    def test_capabilities_for_agent(self):
        registry = CapabilityRegistry()
        registry.register(agent="ATLAS", capability=Capability.REASONING, level=CompetencyLevel.MASTER, granted_by="Owner")
        registry.register(agent="ATLAS", capability=Capability.PLANNING, level=CompetencyLevel.MASTER, granted_by="Owner")
        registry.register(agent="NAVIGATOR", capability=Capability.BROWSER, level=CompetencyLevel.SENIOR, granted_by="ATLAS")
        self.assertEqual(len(registry.capabilities_for("ATLAS")), 2)

    def test_agents_with_capability_filters_by_usability(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.ALL_ROUNDER, level=CompetencyLevel.MASTER, granted_by="Owner",
            available=True, authorized=True, health=CapabilityHealth.HEALTHY,
        )
        registry.register(agent="NAVIGATOR", capability=Capability.ALL_ROUNDER, level=CompetencyLevel.PLENO, granted_by="ATLAS")
        usable = registry.agents_with_capability(Capability.ALL_ROUNDER, only_usable=True)
        self.assertEqual(len(usable), 1)
        self.assertEqual(usable[0].agent, "ATLAS")

    def test_revoke_removes_capability(self):
        registry = CapabilityRegistry()
        registry.register(
            agent="ATLAS", capability=Capability.CODE, level=CompetencyLevel.SENIOR, granted_by="Owner",
            available=True, authorized=True,
        )
        registry.revoke(agent="ATLAS", capability=Capability.CODE)
        self.assertFalse(registry.is_available(agent="ATLAS", capability=Capability.CODE))


class ClaimTypeTests(unittest.TestCase):
    def test_never_fabricated_metrics_flagged(self):
        self.assertTrue(requires_verifiable_source("roi"))
        self.assertTrue(requires_verifiable_source("conversion_rate"))
        self.assertFalse(requires_verifiable_source("niche"))


class MultimodalEvidenceTests(unittest.TestCase):
    def test_confidence_must_be_in_range(self):
        with self.assertRaises(ValueError):
            MultimodalEvidence.new(modality=Modality.TEXT, source="x", provenance="x", confidence=1.5)

    def test_unavailable_evidence_always_zero_confidence(self):
        ev = MultimodalEvidence.unavailable(modality=Modality.VIDEO, reason="sem provider")
        self.assertEqual(ev.confidence, 0.0)
        self.assertEqual(ev.claim_type, ClaimType.UNKNOWN)

    def test_evidence_never_stores_raw_sensitive_content(self):
        ev = MultimodalEvidence.new(modality=Modality.IMAGE, source="screenshot.png", provenance="mock", reference_hash="abc123")
        self.assertFalse(hasattr(ev, "raw_content"))
        self.assertFalse(hasattr(ev, "binary_data"))


class MockModalityAnalyzerTests(unittest.TestCase):
    def test_never_fakes_analysis_without_provider(self):
        analyzer = MockModalityAnalyzer(modality=Modality.AUDIO)
        result = analyzer.analyze(reference="audio.mp3", context={})
        self.assertEqual(result.confidence, 0.0)
        self.assertIn("nenhum provider real", result.provenance)


class MultimodalReasonerTests(unittest.TestCase):
    def test_text_plus_image_correlation(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(
            requested_modalities=[Modality.TEXT, Modality.IMAGE],
            references={Modality.TEXT: "relatorio.txt", Modality.IMAGE: "screenshot.png"},
        )
        self.assertEqual(len(result.evidences), 2)
        self.assertEqual(result.missing_modalities, [])

    def test_screenshot_plus_instruction(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(
            requested_modalities=[Modality.SCREENSHOT, Modality.TEXT],
            references={Modality.SCREENSHOT: "shot.png", Modality.TEXT: "clique no botão azul"},
        )
        self.assertEqual(len(result.evidences), 2)

    def test_document_plus_table(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(
            requested_modalities=[Modality.DOCUMENT, Modality.TABLE],
            references={Modality.DOCUMENT: "doc.pdf", Modality.TABLE: "dados.csv"},
        )
        self.assertEqual(len(result.evidences), 2)

    def test_audio_metadata_only(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(requested_modalities=[Modality.AUDIO], references={Modality.AUDIO: "audio_meta_ref"})
        self.assertEqual(result.evidences[0].modality, Modality.AUDIO)

    def test_video_metadata_frames_simulated(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(requested_modalities=[Modality.VIDEO], references={Modality.VIDEO: "video_frames_ref"})
        self.assertEqual(result.evidences[0].modality, Modality.VIDEO)

    def test_combination_of_modalities(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(
            requested_modalities=[Modality.TEXT, Modality.IMAGE, Modality.AUDIO, Modality.VIDEO],
            references={Modality.TEXT: "a", Modality.IMAGE: "b", Modality.AUDIO: "c", Modality.VIDEO: "d"},
        )
        self.assertEqual(len(result.evidences), 4)

    def test_provider_unavailable_never_fabricates(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(requested_modalities=[Modality.CODE], references={Modality.CODE: "ref"})
        self.assertEqual(result.combined_confidence, 0.0)

    def test_missing_reference_recorded_as_missing_modality(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(requested_modalities=[Modality.PDF], references={})
        self.assertIn(Modality.PDF, result.missing_modalities)
        self.assertEqual(len(result.evidences), 0)

    def test_confidence_reduced_never_inflated_by_combination(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(
            requested_modalities=[Modality.TEXT, Modality.IMAGE], references={Modality.TEXT: "a", Modality.IMAGE: "b"},
        )
        self.assertEqual(result.combined_confidence, 0.0)

    def test_provenance_recorded_for_evidence(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(requested_modalities=[Modality.LOG], references={Modality.LOG: "app.log"})
        self.assertTrue(result.evidences[0].provenance)

    def test_register_real_analyzer_overrides_mock(self):
        class FakeRealVision:
            modality = Modality.IMAGE

            def analyze(self, *, reference, context=None):
                return MultimodalEvidence.new(
                    modality=Modality.IMAGE, source=reference, provenance="provider real simulado",
                    confidence=0.9, claim_type=ClaimType.FACT,
                )

        reasoner = MultimodalReasoner()
        reasoner.register_analyzer(modality=Modality.IMAGE, analyzer=FakeRealVision())
        result = reasoner.correlate(requested_modalities=[Modality.IMAGE], references={Modality.IMAGE: "x.png"})
        self.assertEqual(result.combined_confidence, 0.9)

    def test_related_mission_task_propagated(self):
        reasoner = MultimodalReasoner()
        result = reasoner.correlate(
            requested_modalities=[Modality.TEXT], references={Modality.TEXT: "x"},
            related_mission_id="m1", related_task_id="t1",
        )
        self.assertEqual(result.evidences[0].related_mission_id, "m1")
        self.assertEqual(result.evidences[0].related_task_id, "t1")


if __name__ == "__main__":
    unittest.main()
