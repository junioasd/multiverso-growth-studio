"""
core.multimodal.real_audio_video_analyzer
==============================================
MICROCORREÇÃO — item 3 (MULTIMODAL: áudio/vídeo). Análise REAL de
arquivos de áudio (formato WAV, via `wave`/`audioop` da stdlib) e
vídeo (via `opencv-python`, já instalado) — duração real, taxa de
amostragem/quadros real, número de canais/quadros real, estatística
real de amplitude/brilho por amostragem. Nunca "entende" o CONTEÚDO
(fala, música, cena) — isso exigiria um modelo real de áudio/visão via
rede/API, fora do alcance deste ambiente sem credencial — apenas
características de arquivo genuinamente computadas.

Limitação honesta: a extração REAL de áudio via stdlib só cobre WAV
(`wave`/`audioop` não decodificam MP3/AAC/OGG — exigiria uma
biblioteca de codec adicional não instalada neste ambiente). Um
arquivo de áudio em outro formato reporta `UNAVAILABLE` explicitamente
— nunca finge ter processado.
"""

from __future__ import annotations

import hashlib
import os
import wave

from core.evidence.claims import ClaimType
from core.multimodal.evidence import Modality, MultimodalEvidence


def _file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class RealAudioAnalyzer:
    """Análise REAL de áudio WAV via `wave`/`audioop` (stdlib) —
    duração/canais/taxa de amostragem reais, RMS real de amplitude.
    Formatos comprimidos (MP3/AAC/OGG) não são decodificáveis com
    stdlib puro — reportado como `UNAVAILABLE` explicitamente, nunca
    fabricado."""

    modality = Modality.AUDIO
    _SUPPORTED_EXTENSIONS = (".wav", ".wave")

    def analyze(self, *, reference: str, context: dict | None = None) -> MultimodalEvidence:
        if not reference or not os.path.isfile(reference):
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"arquivo de áudio não encontrado: '{reference}'",
            )
        ext = os.path.splitext(reference)[1].lower()
        if ext not in self._SUPPORTED_EXTENSIONS:
            return MultimodalEvidence.unavailable(
                modality=self.modality,
                reason=(
                    f"formato '{ext}' não decodificável com as bibliotecas disponíveis neste ambiente "
                    "(apenas WAV é suportado sem um codec adicional) — nunca finge ter processado"
                ),
            )
        try:
            import audioop  # stdlib, obsoleto mas ainda disponível no Python 3.12

            with wave.open(reference, "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_rate = wav_file.getframerate()
                frame_count = wav_file.getnframes()
                sample_width = wav_file.getsampwidth()
                duration_seconds = frame_count / sample_rate if sample_rate else 0.0
                raw_frames = wav_file.readframes(frame_count)
                rms = audioop.rms(raw_frames, sample_width) if raw_frames else 0
        except Exception as exc:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"falha ao processar áudio WAV real: {exc}",
            )

        summary = (
            f"{duration_seconds:.2f}s, {channels} canal(is), {sample_rate}Hz, RMS de amplitude={rms}"
        )
        return MultimodalEvidence.new(
            modality=self.modality, source=reference, provenance="análise real de forma de onda via wave/audioop (stdlib)",
            confidence=1.0, reference_hash=_file_hash(reference), claim_type=ClaimType.FACT, summary=summary,
        )


class RealVideoAnalyzer:
    """Análise REAL de vídeo via OpenCV (`cv2.VideoCapture`) — duração/
    resolução/fps/número de quadros reais, brilho médio real
    amostrando quadros ao longo do vídeo (nunca todos os quadros —
    amostragem limitada para não processar arquivos grandes
    indefinidamente)."""

    modality = Modality.VIDEO
    _MAX_SAMPLED_FRAMES = 10

    def analyze(self, *, reference: str, context: dict | None = None) -> MultimodalEvidence:
        if not reference or not os.path.isfile(reference):
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"arquivo de vídeo não encontrado: '{reference}'",
            )
        try:
            import cv2
        except ImportError:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason="biblioteca opencv-python não disponível neste ambiente",
            )
        capture = cv2.VideoCapture(reference)
        try:
            if not capture.isOpened():
                return MultimodalEvidence.unavailable(
                    modality=self.modality, reason="OpenCV não conseguiu abrir o arquivo de vídeo (formato não suportado/corrompido)",
                )
            fps = capture.get(cv2.CAP_PROP_FPS)
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            duration_seconds = frame_count / fps if fps else 0.0

            # amostragem REAL de brilho medio -- nunca le o video inteiro
            sampled_brightness = []
            if frame_count > 0:
                step = max(1, frame_count // self._MAX_SAMPLED_FRAMES)
                for i in range(0, frame_count, step):
                    capture.set(cv2.CAP_PROP_POS_FRAMES, i)
                    ok, frame = capture.read()
                    if ok:
                        sampled_brightness.append(float(frame.mean()))
                    if len(sampled_brightness) >= self._MAX_SAMPLED_FRAMES:
                        break
        except Exception as exc:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"falha ao processar vídeo real: {exc}",
            )
        finally:
            capture.release()

        if frame_count <= 0:
            return MultimodalEvidence.unavailable(modality=self.modality, reason="vídeo sem quadros legíveis")

        avg_brightness = sum(sampled_brightness) / len(sampled_brightness) if sampled_brightness else None
        summary = (
            f"{duration_seconds:.2f}s, {width}x{height}px, {fps:.1f}fps, {frame_count} quadros"
            + (f", brilho médio amostrado={avg_brightness:.1f}/255 ({len(sampled_brightness)} quadros)" if avg_brightness is not None else "")
        )
        return MultimodalEvidence.new(
            modality=self.modality, source=reference, provenance="análise real de quadros via OpenCV (amostragem)",
            confidence=1.0 if sampled_brightness else 0.5, reference_hash=_file_hash(reference),
            claim_type=ClaimType.FACT, summary=summary,
        )
