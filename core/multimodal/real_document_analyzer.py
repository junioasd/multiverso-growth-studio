"""
core.multimodal.real_document_analyzer
===========================================
MICROCORREÇÃO — item 3 (MULTIMODAL: PDF/documento). Extração REAL de
texto/metadados de arquivos PDF (via `pypdf`, já instalado) e DOCX
(via `python-docx`, já instalado) — número de páginas/parágrafos real,
contagem real de caracteres extraídos, metadados reais do arquivo.
Nunca "resume" ou "interpreta" o conteúdo (isso exigiria um modelo de
linguagem real) — apenas extração literal e suas estatísticas.

`reference` é sempre um caminho de arquivo real no disco.
"""

from __future__ import annotations

import hashlib
import os

from core.evidence.claims import ClaimType
from core.multimodal.evidence import Modality, MultimodalEvidence


def _file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class RealPdfAnalyzer:
    """Extração REAL de PDF via `pypdf` — número de páginas real,
    texto real extraído (contagem de caracteres, nunca o texto
    completo em `summary` — resumo curto, nunca conteúdo bruto
    sensível, mesma disciplina de `MultimodalEvidence.summary`)."""

    modality = Modality.PDF

    def analyze(self, *, reference: str, context: dict | None = None) -> MultimodalEvidence:
        if not reference or not os.path.isfile(reference):
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"arquivo PDF não encontrado: '{reference}'",
            )
        try:
            import pypdf
        except ImportError:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason="biblioteca pypdf não disponível neste ambiente",
            )
        try:
            reader = pypdf.PdfReader(reference)
            page_count = len(reader.pages)
            total_chars = 0
            pdf_texts = []
            for page in reader.pages:
                text = page.extract_text() or ""
                total_chars += len(text)
                pdf_texts.append(text)
            is_encrypted = reader.is_encrypted
        except Exception as exc:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"falha ao processar PDF real: {exc}",
            )

        summary = f"{page_count} página(s), {total_chars} caracteres extraídos" + (
            " (criptografado — texto pode estar inacessível)" if is_encrypted else ""
        )
        confidence = 1.0 if (page_count > 0 and not is_encrypted) else (0.3 if page_count > 0 else 0.0)
        return MultimodalEvidence.new(
            modality=self.modality, source=reference, provenance="extração real de texto via pypdf",
            confidence=confidence, reference_hash=_file_hash(reference), claim_type=ClaimType.FACT, summary=summary,
            content_text="\n".join(pdf_texts),
        )


class RealDocxAnalyzer:
    """Extração REAL de DOCX via `python-docx` — contagem real de
    parágrafos/caracteres/tabelas."""

    modality = Modality.DOCUMENT

    def analyze(self, *, reference: str, context: dict | None = None) -> MultimodalEvidence:
        if not reference or not os.path.isfile(reference):
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"arquivo de documento não encontrado: '{reference}'",
            )
        try:
            import docx
        except ImportError:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason="biblioteca python-docx não disponível neste ambiente",
            )
        try:
            document = docx.Document(reference)
            paragraph_count = len(document.paragraphs)
            total_chars = sum(len(p.text) for p in document.paragraphs)
            table_count = len(document.tables)
        except Exception as exc:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"falha ao processar DOCX real: {exc}",
            )

        summary = f"{paragraph_count} parágrafo(s), {total_chars} caracteres, {table_count} tabela(s)"
        return MultimodalEvidence.new(
            modality=self.modality, source=reference, provenance="extração real de texto via python-docx",
            confidence=1.0, reference_hash=_file_hash(reference), claim_type=ClaimType.FACT, summary=summary,
            content_text="\n".join(p.text for p in document.paragraphs),
        )
