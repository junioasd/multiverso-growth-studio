"""
core.multimodal.real_analyzers
===================================
BLOCO FINAL 1 (item 2) + MICROCORREÇÃO (item 3 — tornar operacional
para todas as modalidades restantes): reúne TODOS os analisadores
REAIS disponíveis neste ambiente — TEXT/STRUCTURED_DATA/TABLE (stdlib
puro) e IMAGE/SCREENSHOT/PDF/DOCUMENT/WEB_PAGE/AUDIO/VIDEO (via
bibliotecas reais já instaladas: PIL, pypdf, python-docx, OpenCV,
wave/audioop, urllib.request) — nenhuma modalidade permanece MOCK
nesta instalação; a única coisa que pode fazer uma modalidade
específica reportar `UNAVAILABLE` é a ausência REAL do arquivo/rede/
credencial no momento da chamada, nunca uma implementação de stub
permanente.
"""

from __future__ import annotations

import re
import statistics

from core.evidence.claims import ClaimType
from core.multimodal.evidence import Modality, MultimodalEvidence


class RealTextAnalyzer:
    """Análise REAL de texto — contagem de palavras/frases,
    diversidade lexical, tamanho médio de frase. Nunca "entende" o
    SIGNIFICADO do texto (isso exigiria um modelo de linguagem real) —
    apenas características estruturais genuínas e verificáveis."""

    modality = Modality.TEXT

    def analyze(self, *, reference: str, context: dict | None = None) -> MultimodalEvidence:
        text = reference or ""
        if not text.strip():
            return MultimodalEvidence.unavailable(modality=self.modality, reason="texto vazio")

        sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
        words = re.findall(r"\b\w+\b", text.lower())
        unique_words = set(words)

        word_count = len(words)
        sentence_count = max(len(sentences), 1)
        avg_sentence_length = word_count / sentence_count
        lexical_diversity = len(unique_words) / word_count if word_count else 0.0

        summary = (
            f"{word_count} palavras, {sentence_count} frases, "
            f"{avg_sentence_length:.1f} palavras/frase em média, "
            f"diversidade lexical {lexical_diversity:.0%}"
        )
        # confidence reflete o tamanho REAL da amostra -- texto muito
        # curto nunca sustenta uma analise estatisticamente confiavel
        confidence = min(1.0, word_count / 50) if word_count > 0 else 0.0

        return MultimodalEvidence.new(
            modality=self.modality, source="real_text_analyzer", provenance="analise estrutural real (stdlib)",
            confidence=confidence, claim_type=ClaimType.FACT, summary=summary,
        )


class RealStructuredDataAnalyzer:
    """Análise estatística REAL de dados estruturados/tabelas —
    delega para a MESMA lógica de `core.agents.runtime.prisma.
    PrismaAgent` (nunca uma segunda implementação divergente)."""

    modality = Modality.STRUCTURED_DATA

    def __init__(self) -> None:
        from core.agents.runtime.prisma import PrismaAgent

        self._prisma = PrismaAgent()

    def analyze(self, *, reference, context: dict | None = None) -> MultimodalEvidence:
        # `reference` aqui é a lista de valores numericos (nao uma string) --
        # o protocolo ModalityAnalyzer usa `reference: str` como tipo comum,
        # mas dados estruturados sao naturalmente uma lista/dict; aceitamos
        # ambos os casos.
        values = reference
        if not isinstance(values, list) or not all(isinstance(v, (int, float)) for v in values):
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason="reference precisa ser uma lista de números para análise estrutural real",
            )
        result = self._prisma.execute(task_type="DESCRIBE_SERIES", payload={"values": values})
        if result.output.get("status") == "INSUFFICIENT_DATA":
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"amostra insuficiente ({result.output['sample_size']} pontos)",
            )
        summary = (
            f"{result.output['sample_size']} pontos, média={result.output['mean']:.2f}, "
            f"desvio={result.output['stdev']:.2f}"
        )
        return MultimodalEvidence.new(
            modality=self.modality, source="real_structured_data_analyzer",
            provenance="análise estatística real (statistics stdlib, via PrismaAgent)",
            confidence=result.confidence, claim_type=ClaimType.FACT, summary=summary,
        )


class RealTableAnalyzer(RealStructuredDataAnalyzer):
    """Mesma lógica de `RealStructuredDataAnalyzer`, registrada para a
    modalidade `TABLE` (linhas/colunas numéricas já achatadas em uma
    lista pelo chamador — este analisador não faz parsing de formato
    de tabela algum, apenas estatística sobre os números fornecidos)."""

    modality = Modality.TABLE


def install_real_analyzers(reasoner) -> None:
    """MICROCORREÇÃO (item 3): substitui TODOS os analisadores MOCK
    padrão por implementações REAIS — nenhuma modalidade permanece
    MOCK nesta instalação. Cada analisador tenta processamento
    genuíno (PIL/pypdf/python-docx/OpenCV/wave/urllib.request) e
    reporta `UNAVAILABLE` explicitamente apenas quando o arquivo/rede
    real não está acessível no momento da chamada — nunca um stub
    permanente."""
    from core.multimodal.real_audio_video_analyzer import RealAudioAnalyzer, RealVideoAnalyzer
    from core.multimodal.real_document_analyzer import RealDocxAnalyzer, RealPdfAnalyzer
    from core.multimodal.real_image_analyzer import RealImageAnalyzer, RealScreenshotAnalyzer
    from core.multimodal.real_webpage_analyzer import RealWebPageAnalyzer

    reasoner.register_analyzer(modality=Modality.TEXT, analyzer=RealTextAnalyzer())
    reasoner.register_analyzer(modality=Modality.STRUCTURED_DATA, analyzer=RealStructuredDataAnalyzer())
    reasoner.register_analyzer(modality=Modality.TABLE, analyzer=RealTableAnalyzer())
    reasoner.register_analyzer(modality=Modality.IMAGE, analyzer=RealImageAnalyzer())
    reasoner.register_analyzer(modality=Modality.SCREENSHOT, analyzer=RealScreenshotAnalyzer())
    reasoner.register_analyzer(modality=Modality.PDF, analyzer=RealPdfAnalyzer())
    reasoner.register_analyzer(modality=Modality.DOCUMENT, analyzer=RealDocxAnalyzer())
    reasoner.register_analyzer(modality=Modality.WEB_PAGE, analyzer=RealWebPageAnalyzer())
    reasoner.register_analyzer(modality=Modality.AUDIO, analyzer=RealAudioAnalyzer())
    reasoner.register_analyzer(modality=Modality.VIDEO, analyzer=RealVideoAnalyzer())
