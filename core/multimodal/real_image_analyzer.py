"""
core.multimodal.real_image_analyzer
========================================
MICROCORREÇÃO — item 3 (MULTIMODAL: imagem/screenshot). Análise REAL
de arquivos de imagem usando PIL/Pillow (já instalado neste ambiente)
— dimensões, formato, modo de cor, brilho médio real (via histograma),
hash de conteúdo real. Nunca "interpreta" o CONTEÚDO SEMÂNTICO da
imagem (isso exigiria um modelo de visão real via rede/API, fora do
alcance deste ambiente sem credencial) — apenas características de
pixel genuinamente computadas e verificáveis.

`reference` é sempre um CAMINHO de arquivo real no disco — nunca uma
URL (isso exigiria rede) nem uma descrição textual alegada pelo
chamador.
"""

from __future__ import annotations

import hashlib
import os

from core.evidence.claims import ClaimType
from core.multimodal.evidence import Modality, MultimodalEvidence


class RealImageAnalyzer:
    """Análise REAL de imagem via PIL — nunca inventa metadados;
    arquivo inexistente/corrompido sempre vira `unavailable()`, nunca
    um resultado parcial apresentado como completo."""

    modality = Modality.IMAGE

    def analyze(self, *, reference: str, context: dict | None = None) -> MultimodalEvidence:
        if not reference or not os.path.isfile(reference):
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"arquivo de imagem não encontrado: '{reference}'",
            )
        try:
            from PIL import Image, ImageStat
        except ImportError:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason="biblioteca PIL/Pillow não disponível neste ambiente",
            )
        try:
            with Image.open(reference) as img:
                img.load()
                width, height = img.size
                fmt = img.format or "desconhecido"
                mode = img.mode
                # estatistica REAL de brilho -- converte para escala de cinza
                # e calcula media real via ImageStat (nunca estimado)
                gray = img.convert("L")
                stat = ImageStat.Stat(gray)
                mean_brightness = stat.mean[0]  # 0-255, real
                stddev_brightness = stat.stddev[0]
        except Exception as exc:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"falha ao abrir/processar imagem real: {exc}",
            )

        with open(reference, "rb") as f:
            content_hash = hashlib.sha256(f.read()).hexdigest()

        summary = (
            f"{width}x{height}px, formato={fmt}, modo={mode}, "
            f"brilho médio={mean_brightness:.1f}/255 (desvio={stddev_brightness:.1f})"
        )
        return MultimodalEvidence.new(
            modality=self.modality, source=reference, provenance="análise real de pixel via PIL/Pillow",
            confidence=1.0, reference_hash=content_hash, claim_type=ClaimType.FACT, summary=summary,
        )


class RealScreenshotAnalyzer(RealImageAnalyzer):
    """Mesma análise real de pixel — screenshots são arquivos de
    imagem como quaisquer outros; a distinção de modalidade é
    semântica (origem = captura de tela), não técnica."""

    modality = Modality.SCREENSHOT
