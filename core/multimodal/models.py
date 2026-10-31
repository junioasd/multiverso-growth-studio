"""
core.multimodal.models
==========================
EXPANSÃO — item 5 (raciocínio multimodal) + item 24 (abstração de
modelos/providers).

Interfaces SEPARADAS por modalidade (VisionModel, AudioModel,
VideoModel, DocumentModel) — nenhuma capacidade multimodal é acoplada
a um único fornecedor. Sem provider real configurado, as
implementações padrão são MOCK/DETERMINÍSTICAS e NUNCA inventam
análise: sempre declaram indisponibilidade e devolvem confidence=0.0
(nunca simulam capacidade real inexistente).

`MultimodalReasoner` correlaciona evidências de várias modalidades —
a confiança combinada nunca é maior que a mínima confiança real
disponível entre as modalidades correlacionadas (nunca "infla"
confiança combinando incerteza com incerteza).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from core.evidence.claims import ClaimType
from core.multimodal.evidence import Modality, MultimodalEvidence


class ModalityAnalyzer(Protocol):
    modality: Modality

    def analyze(self, *, reference: str, context: dict) -> MultimodalEvidence: ...


@dataclass
class MockModalityAnalyzer:
    """Base MOCK para qualquer modalidade — sem provider real
    conectado, nunca finge análise. Uma integração real (fora deste
    lote) substitui esta classe por uma implementação concreta do
    mesmo `ModalityAnalyzer`, sem alterar `MultimodalReasoner`."""

    modality: Modality

    def analyze(self, *, reference: str, context: dict | None = None) -> MultimodalEvidence:
        return MultimodalEvidence.unavailable(
            modality=self.modality, reason=f"nenhum provider real de {self.modality.value} configurado neste ambiente",
        )


def build_default_analyzers() -> dict:
    """Um `MockModalityAnalyzer` por modalidade — o ponto de integração
    real é substituir a entrada correspondente por uma implementação
    concreta (ex: `reasoner.analyzers[Modality.IMAGE] = RealVisionModel()`)."""
    return {modality: MockModalityAnalyzer(modality=modality) for modality in Modality}


@dataclass
class MultimodalCorrelationResult:
    evidences: list
    missing_modalities: list
    combined_confidence: float
    summary: str


@dataclass
class MultimodalReasoner:
    analyzers: dict = field(default_factory=build_default_analyzers)

    def register_analyzer(self, *, modality: Modality, analyzer: ModalityAnalyzer) -> None:
        """Ponto de integração real — troca o analisador MOCK de uma
        modalidade por uma implementação real, sem alterar
        `correlate()`."""
        self.analyzers[modality] = analyzer

    def correlate(
        self, *, requested_modalities: list, references: dict, context: dict | None = None,
        related_mission_id: str | None = None, related_task_id: str | None = None,
    ) -> MultimodalCorrelationResult:
        """Correlaciona evidências entre modalidades. Modalidade
        solicitada sem referência fornecida, ou sem analisador
        registrado, entra em `missing_modalities` — nunca inventa a
        análise que faltou. `combined_confidence` nunca é maior que a
        MENOR confiança real entre as evidências efetivamente
        coletadas (0.0 se nenhuma modalidade teve análise real)."""
        context = context or {}
        evidences: list = []
        missing: list = []

        for modality in requested_modalities:
            reference = references.get(modality)
            analyzer = self.analyzers.get(modality)
            if reference is None or analyzer is None:
                missing.append(modality)
                continue
            evidence = analyzer.analyze(reference=reference, context=context)
            if related_mission_id is not None:
                evidence.related_mission_id = related_mission_id
            if related_task_id is not None:
                evidence.related_task_id = related_task_id
            evidences.append(evidence)

        real_confidences = [e.confidence for e in evidences if e.confidence > 0.0]
        combined_confidence = min(real_confidences) if real_confidences else 0.0

        summary = (
            f"{len(evidences)} modalidade(s) correlacionada(s) "
            f"({len(real_confidences)} com evidência real); {len(missing)} indisponível(is)"
        )
        return MultimodalCorrelationResult(
            evidences=evidences, missing_modalities=missing, combined_confidence=combined_confidence,
            summary=summary,
        )
