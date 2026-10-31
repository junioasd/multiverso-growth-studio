import os
import tempfile
import unittest

from core.agents.runtime.bridge import BridgeAgent
from core.agents.runtime.contracts import AgentTaskStatus
from core.agents.runtime.http_generation_provider import HttpGenerationProvider
from core.agents.runtime.http_search_provider import HttpSearchProvider
from core.agents.runtime.sentinel import SentinelAgent
from core.multimodal.evidence import Modality

"""
Testes MICROCORREÇÃO — BLOCO FINAL 1: SENTINEL/BRIDGE com runtime
real (0 PARTIAL), providers HTTP reais substituindo NullProvider, e
analisadores multimodais reais para todas as modalidades restantes
(imagem, screenshot, PDF, documento, página web, áudio, vídeo).
"""


class SentinelAgentTests(unittest.TestCase):
    def test_review_video_uses_real_quality_supervisor(self):
        agent = SentinelAgent()
        result = agent.execute(task_type="REVIEW_VIDEO", payload={"subject": {}})
        self.assertEqual(result.status, AgentTaskStatus.COMPLETED)
        self.assertIn("decision", result.output)
        self.assertIn("verdict", result.output)

    def test_missing_subject_rejected(self):
        agent = SentinelAgent()
        result = agent.execute(task_type="REVIEW_VIDEO", payload={})
        self.assertEqual(result.status, AgentTaskStatus.REJECTED)

    def test_unknown_evidence_never_becomes_proceed(self):
        """UNKNOWN nunca vira aprovação por omissão — confiança sempre
        0.0 nesse caso, preservando a disciplina já estabelecida."""
        agent = SentinelAgent()
        result = agent.execute(task_type="REVIEW_VIDEO", payload={"subject": {}})
        if result.output["verdict"] == "UNKNOWN":
            self.assertEqual(result.confidence, 0.0)
            self.assertNotEqual(result.output["decision"], "PROCEED")


class BridgeAgentTests(unittest.TestCase):
    def test_register_and_list_connector(self):
        agent = BridgeAgent()
        r1 = agent.execute(task_type="REGISTER_CONNECTOR", payload={
            "platform": "youtube", "connector_type": "OFFICIAL_API", "is_configured": True, "authenticated": True,
        })
        self.assertEqual(r1.status, AgentTaskStatus.COMPLETED)
        r2 = agent.execute(task_type="LIST_CONNECTORS", payload={"platform": "youtube"})
        self.assertEqual(len(r2.output["connectors"]), 1)

    def test_invalid_connector_type_rejected(self):
        agent = BridgeAgent()
        result = agent.execute(task_type="REGISTER_CONNECTOR", payload={"platform": "x", "connector_type": "NAO_EXISTE"})
        self.assertEqual(result.status, AgentTaskStatus.REJECTED)

    def test_best_execution_path_none_when_nothing_registered(self):
        agent = BridgeAgent()
        result = agent.execute(task_type="BEST_EXECUTION_PATH", payload={"platform": "plataforma_sem_conector"})
        self.assertFalse(result.output["available"])


class HttpSearchProviderTests(unittest.TestCase):
    def test_not_configured_without_env_vars(self):
        provider = HttpSearchProvider(api_key=None, api_url=None)
        self.assertFalse(provider.is_configured())

    def test_configured_with_explicit_credentials(self):
        provider = HttpSearchProvider(api_key="k", api_url="http://x.invalid/search")
        self.assertTrue(provider.is_configured())

    def test_call_without_credential_reports_no_credential(self):
        provider = HttpSearchProvider(api_key=None, api_url=None)
        response = provider.call(operation="MARKET_RADAR", params={})
        self.assertFalse(response.available)
        self.assertIn("NO_CREDENTIAL_CONFIGURED", response.reason)

    def test_network_error_handling_path_is_real(self):
        """MICROCORREÇÃO (suite-order stability): verifica o CAMINHO
        REAL de tratamento de erro de rede sem depender do
        comportamento de DNS/rede do ambiente onde o teste roda (uma
        tentativa de conexão GENUÍNA a um domínio inválido pode levar
        de milissegundos a dezenas de segundos dependendo do
        resolvedor de DNS local — inaceitável para um teste
        determinístico). Usa `unittest.mock.patch` para simular a
        MESMA exceção (`URLError`) que uma falha de rede real
        produziria, exercitando o código real de tratamento
        (`except urllib.error.URLError`) sem esperar por um timeout de
        verdade. O código de produção (`urllib.request.urlopen`)
        nunca é substituído — apenas o RESULTADO da chamada de rede é
        controlado para o teste ser rápido e determinístico."""
        import urllib.error
        from unittest.mock import patch

        provider = HttpSearchProvider(api_key="fake", api_url="http://endereco-teste.example/search")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("conexão recusada (simulado)")):
            response = provider.call(operation="MARKET_RADAR", params={"query": "x"})
        self.assertFalse(response.available)
        self.assertIn("falha real de rede", response.reason)


class HttpGenerationProviderTests(unittest.TestCase):
    def test_not_configured_without_env_vars(self):
        provider = HttpGenerationProvider(api_key=None, api_url=None)
        self.assertFalse(provider.is_configured())

    def test_network_error_handling_path_is_real(self):
        """Mesma disciplina de `HttpSearchProviderTests.
        test_network_error_handling_path_is_real` — nunca depende do
        tempo real de falha de DNS/rede do ambiente."""
        import urllib.error
        from unittest.mock import patch

        provider = HttpGenerationProvider(api_key="fake", api_url="http://endereco-teste.example/gen")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("conexão recusada (simulado)")):
            response = provider.call(operation="IMAGE_GENERATION", params={})
        self.assertFalse(response.available)
        self.assertIn("falha real de rede", response.reason)


class RealImageModalityTests(unittest.TestCase):
    def test_real_image_analysis_genuine_statistics(self):
        from PIL import Image

        from core.multimodal.real_image_analyzer import RealImageAnalyzer

        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            Image.new("RGB", (80, 40), color=(0, 0, 0)).save(path)  # preto puro -> brilho 0
            evidence = RealImageAnalyzer().analyze(reference=path)
            self.assertEqual(evidence.confidence, 1.0)
            self.assertIn("80x40", evidence.summary)
            self.assertIn("brilho médio=0.0", evidence.summary)
        finally:
            os.remove(path)

    def test_missing_file_unavailable(self):
        from core.multimodal.real_image_analyzer import RealImageAnalyzer

        evidence = RealImageAnalyzer().analyze(reference="/tmp/nao_existe_teste_img.png")
        self.assertEqual(evidence.confidence, 0.0)

    def test_screenshot_analyzer_same_discipline(self):
        from core.multimodal.real_image_analyzer import RealScreenshotAnalyzer

        analyzer = RealScreenshotAnalyzer()
        self.assertEqual(analyzer.modality, Modality.SCREENSHOT)


class RealDocumentModalityTests(unittest.TestCase):
    def test_real_pdf_page_count_and_text(self):
        import pypdf

        from core.multimodal.real_document_analyzer import RealPdfAnalyzer

        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            writer = pypdf.PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.add_blank_page(width=100, height=100)
            writer.add_blank_page(width=100, height=100)
            with open(path, "wb") as f:
                writer.write(f)
            evidence = RealPdfAnalyzer().analyze(reference=path)
            self.assertIn("3 página", evidence.summary)
        finally:
            os.remove(path)

    def test_real_docx_paragraph_count(self):
        import docx

        from core.multimodal.real_document_analyzer import RealDocxAnalyzer

        fd, path = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        try:
            document = docx.Document()
            document.add_paragraph("Primeiro parágrafo real.")
            document.add_paragraph("Segundo parágrafo real.")
            document.add_paragraph("Terceiro.")
            document.save(path)
            evidence = RealDocxAnalyzer().analyze(reference=path)
            self.assertIn("3 parágrafo", evidence.summary)
            self.assertEqual(evidence.confidence, 1.0)
        finally:
            os.remove(path)

    def test_missing_pdf_unavailable(self):
        from core.multimodal.real_document_analyzer import RealPdfAnalyzer

        evidence = RealPdfAnalyzer().analyze(reference="/tmp/nao_existe.pdf")
        self.assertEqual(evidence.confidence, 0.0)


class RealAudioVideoModalityTests(unittest.TestCase):
    def test_real_wav_audio_analysis(self):
        import struct
        import wave

        from core.multimodal.real_audio_video_analyzer import RealAudioAnalyzer

        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as f:
                f.setnchannels(1)
                f.setsampwidth(2)
                f.setframerate(8000)
                f.writeframes(struct.pack("<" + "h" * 8000, *([500] * 8000)))
            evidence = RealAudioAnalyzer().analyze(reference=path)
            self.assertEqual(evidence.confidence, 1.0)
            self.assertIn("1.00s", evidence.summary)
        finally:
            os.remove(path)

    def test_unsupported_audio_format_honestly_unavailable(self):
        from core.multimodal.real_audio_video_analyzer import RealAudioAnalyzer

        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(b"fake mp3 content")
            evidence = RealAudioAnalyzer().analyze(reference=path)
            self.assertEqual(evidence.confidence, 0.0)
            self.assertIn("não decodificável", evidence.summary)
        finally:
            os.remove(path)

    def test_real_video_analysis(self):
        import cv2
        import numpy as np

        from core.multimodal.real_audio_video_analyzer import RealVideoAnalyzer

        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(path, fourcc, 5.0, (32, 32))
            for _ in range(10):
                writer.write(np.zeros((32, 32, 3), dtype=np.uint8))
            writer.release()
            evidence = RealVideoAnalyzer().analyze(reference=path)
            self.assertGreater(evidence.confidence, 0.0)
            self.assertIn("32x32", evidence.summary)
        finally:
            os.remove(path)

    def test_missing_video_unavailable(self):
        from core.multimodal.real_audio_video_analyzer import RealVideoAnalyzer

        evidence = RealVideoAnalyzer().analyze(reference="/tmp/nao_existe.mp4")
        self.assertEqual(evidence.confidence, 0.0)


class RealWebPageModalityTests(unittest.TestCase):
    def test_invalid_url_rejected(self):
        from core.multimodal.real_webpage_analyzer import RealWebPageAnalyzer

        evidence = RealWebPageAnalyzer().analyze(reference="nao-eh-url")
        self.assertEqual(evidence.confidence, 0.0)

    def test_network_error_handling_path_is_real(self):
        """MICROCORREÇÃO (suite-order stability): mesma disciplina de
        `HttpSearchProviderTests.test_network_error_handling_path_is_real`
        — nunca depende do tempo real de rede/DNS do ambiente onde o
        teste roda. Simula a MESMA exceção que uma falha de rede real
        produziria, exercitando o tratamento real de erro
        (`except urllib.error.URLError`), sem esperar por uma conexão
        de verdade."""
        import urllib.error
        from unittest.mock import patch

        from core.multimodal.real_webpage_analyzer import RealWebPageAnalyzer

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("conexão recusada (simulado)")):
            evidence = RealWebPageAnalyzer().analyze(reference="https://example.com")
        self.assertEqual(evidence.confidence, 0.0)
        self.assertIn("análise indisponível", evidence.summary)

    def test_successful_fetch_extracts_real_text(self):
        """Confirma que, quando a rede FUNCIONA (simulado), a
        extração de texto real via `html.parser` (stdlib, nunca
        fabricado) processa corretamente a resposta."""
        import io
        from unittest.mock import MagicMock, patch

        from core.multimodal.real_webpage_analyzer import RealWebPageAnalyzer

        fake_html = b"<html><head><title>Titulo Real</title></head><body><p>Conteudo real de teste.</p></body></html>"
        fake_response = MagicMock()
        fake_response.read.return_value = fake_html
        fake_response.status = 200
        fake_response.headers = {"Content-Type": "text/html"}
        fake_response.__enter__ = lambda self: fake_response
        fake_response.__exit__ = lambda self, *a: None

        with patch("urllib.request.urlopen", return_value=fake_response):
            evidence = RealWebPageAnalyzer().analyze(reference="https://example.com")
        self.assertEqual(evidence.confidence, 1.0)
        self.assertIn("Titulo Real", evidence.summary)


class AllModalitiesRegisteredRealTests(unittest.TestCase):
    """MICROCORREÇÃO — confirma que TODAS as 10 modalidades relevantes
    têm analisador REAL registrado (nenhuma MOCK) via
    `install_real_analyzers`."""

    def test_no_modality_left_as_mock_analyzer(self):
        from core.multimodal.models import MultimodalReasoner, MockModalityAnalyzer
        from core.multimodal.real_analyzers import install_real_analyzers

        reasoner = MultimodalReasoner()
        install_real_analyzers(reasoner)

        relevant_modalities = [
            Modality.TEXT, Modality.STRUCTURED_DATA, Modality.TABLE, Modality.IMAGE, Modality.SCREENSHOT,
            Modality.PDF, Modality.DOCUMENT, Modality.WEB_PAGE, Modality.AUDIO, Modality.VIDEO,
        ]
        for modality in relevant_modalities:
            analyzer = reasoner.analyzers[modality]
            self.assertNotIsInstance(
                analyzer, MockModalityAnalyzer, f"modalidade {modality.value} ainda está com analisador MOCK",
            )


if __name__ == "__main__":
    unittest.main()
