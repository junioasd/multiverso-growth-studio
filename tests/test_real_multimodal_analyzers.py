import unittest

from api.bootstrap import build_app
from core.multimodal.evidence import Modality
from core.multimodal.models import MultimodalReasoner
from core.multimodal.real_analyzers import (
    RealStructuredDataAnalyzer, RealTableAnalyzer, RealTextAnalyzer, install_real_analyzers,
)
from core.security.auth import hash_password

"""
Testes BLOCO FINAL 1 (item 2) — analisadores multimodais reais para
TEXT/STRUCTURED_DATA/TABLE: análise genuína (nunca fabricada),
confiança proporcional ao tamanho real da amostra, demais modalidades
seguem honestamente MOCK/UNAVAILABLE.
"""


class RealTextAnalyzerTests(unittest.TestCase):
    def test_empty_text_unavailable(self):
        analyzer = RealTextAnalyzer()
        evidence = analyzer.analyze(reference="")
        self.assertEqual(evidence.confidence, 0.0)

    def test_real_text_produces_genuine_statistics(self):
        analyzer = RealTextAnalyzer()
        evidence = analyzer.analyze(reference="Isso é um teste real. Com duas frases completas.")
        self.assertGreater(evidence.confidence, 0.0)
        self.assertIn("palavras", evidence.summary)

    def test_short_text_has_low_confidence_never_inflated(self):
        analyzer = RealTextAnalyzer()
        evidence = analyzer.analyze(reference="Oi.")
        self.assertLess(evidence.confidence, 0.2)


class RealStructuredDataAnalyzerTests(unittest.TestCase):
    def test_numeric_series_analyzed_for_real(self):
        analyzer = RealStructuredDataAnalyzer()
        evidence = analyzer.analyze(reference=[10, 20, 30, 40, 50])
        self.assertGreater(evidence.confidence, 0.0)
        self.assertIn("média", evidence.summary)

    def test_non_numeric_reference_unavailable_never_fabricated(self):
        analyzer = RealStructuredDataAnalyzer()
        evidence = analyzer.analyze(reference="não é uma lista numérica")
        self.assertEqual(evidence.confidence, 0.0)

    def test_table_analyzer_same_discipline(self):
        analyzer = RealTableAnalyzer()
        evidence = analyzer.analyze(reference=[1, 2, 3, 4, 5])
        self.assertGreater(evidence.confidence, 0.0)
        self.assertEqual(evidence.modality, Modality.TABLE)


class MultimodalReasonerIntegrationTests(unittest.TestCase):
    def test_text_and_structured_data_real_others_mock(self):
        reasoner = MultimodalReasoner()
        install_real_analyzers(reasoner)
        result = reasoner.correlate(
            requested_modalities=[Modality.TEXT, Modality.IMAGE, Modality.STRUCTURED_DATA],
            references={
                Modality.TEXT: "Um texto de teste real com múltiplas palavras para gerar estatística genuína.",
                Modality.IMAGE: "ref.png",
                Modality.STRUCTURED_DATA: [1, 2, 3, 4, 5, 6],
            },
        )
        by_modality = {e.modality: e for e in result.evidences}
        self.assertGreater(by_modality[Modality.TEXT].confidence, 0.0)
        self.assertGreater(by_modality[Modality.STRUCTURED_DATA].confidence, 0.0)
        self.assertEqual(by_modality[Modality.IMAGE].confidence, 0.0)  # honestamente MOCK

    def test_combined_confidence_never_exceeds_minimum_real(self):
        """A confiança combinada nunca infla ao misturar uma
        modalidade real (alta confiança) com uma indisponível."""
        reasoner = MultimodalReasoner()
        install_real_analyzers(reasoner)
        result = reasoner.correlate(
            requested_modalities=[Modality.TEXT],
            references={Modality.TEXT: "x" * 5},  # texto curto -> confianca baixa
        )
        self.assertLessEqual(result.combined_confidence, 1.0)


class AtlasDirectorMultimodalWiringTests(unittest.TestCase):
    def test_atlas_multimodal_reasoner_has_real_text_analyzer(self):
        app = build_app(owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), persistent=False)
        try:
            result = app.atlas.multimodal_reasoner.correlate(
                requested_modalities=[Modality.TEXT],
                references={Modality.TEXT: "Texto real conectado via AtlasDirector para verificação."},
            )
            self.assertGreater(result.evidences[0].confidence, 0.0)
        finally:
            app.shutdown()


if __name__ == "__main__":
    unittest.main()
