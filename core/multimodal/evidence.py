"""
core.multimodal.evidence
============================
EXPANSÃO — item 6 (Multimodal Evidence Layer).

Toda evidência multimodal carrega EXATAMENTE os campos exigidos: type
(modality), source, timestamp, hash/reference, confidence, provenance,
related mission/task, modality. Nunca guarda conteúdo sensível
desnecessário — só uma referência/hash e um resumo curto, nunca o
conteúdo bruto (imagem, áudio, vídeo, documento) em si.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.evidence.claims import ClaimType


class Modality(str, Enum):
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    SCREENSHOT = "SCREENSHOT"
    DOCUMENT = "DOCUMENT"
    PDF = "PDF"
    TABLE = "TABLE"
    STRUCTURED_DATA = "STRUCTURED_DATA"
    WEB_PAGE = "WEB_PAGE"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"
    LOG = "LOG"
    CODE = "CODE"
    TEST_RESULT = "TEST_RESULT"
    EXTERNAL_RECEIPT = "EXTERNAL_RECEIPT"


@dataclass
class MultimodalEvidence:
    evidence_id: str
    modality: Modality
    source: str
    timestamp: float
    confidence: float  # 0.0..1.0 — 0.0 quando a modalidade está indisponível
    provenance: str
    reference_hash: str | None = None
    related_mission_id: str | None = None
    related_task_id: str | None = None
    claim_type: ClaimType = ClaimType.UNKNOWN
    summary: str = ""  # descrição curta — nunca o conteúdo bruto sensível
    # COMANDO 4/6: texto EXTRAÍDO (literal) — base rastreável para sinais
    # comerciais (cada sinal cita trecho e posição). Nunca vai para logs/resumo.
    content_text: str = ""

    @staticmethod
    def new(
        *, modality: Modality, source: str, provenance: str, confidence: float = 0.0,
        reference_hash: str | None = None, related_mission_id: str | None = None,
        related_task_id: str | None = None, claim_type: ClaimType = ClaimType.UNKNOWN, summary: str = "",
        content_text: str = "",
    ) -> "MultimodalEvidence":
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence deve estar entre 0.0 e 1.0 — recebido {confidence}")
        return MultimodalEvidence(
            evidence_id=f"mmev_{uuid.uuid4().hex[:12]}", modality=modality, source=source,
            timestamp=time.time(), confidence=confidence, provenance=provenance,
            reference_hash=reference_hash, related_mission_id=related_mission_id,
            related_task_id=related_task_id, claim_type=claim_type, summary=summary, content_text=content_text,
        )

    @staticmethod
    def unavailable(*, modality: Modality, reason: str) -> "MultimodalEvidence":
        """Quando uma modalidade não está disponível — NUNCA inventa
        análise; declara indisponibilidade explicitamente, confidence
        sempre 0.0."""
        return MultimodalEvidence.new(
            modality=modality, source="unavailable", provenance=f"MOCK/indisponível — {reason}",
            confidence=0.0, claim_type=ClaimType.UNKNOWN, summary=f"análise indisponível: {reason}",
        )
