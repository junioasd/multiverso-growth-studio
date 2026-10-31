"""
core.quality.video_quality
===========================
Video Quality Gate (Seção 21). Bloqueia vídeo com áudio cortado, duração
errada, narração incompleta, imagem estática excessiva, cena repetida,
cenas irrelevantes, deformação grave, sincronização ruim, legendas erradas,
pronúncia ruim, thumbnail fraca, erro factual, CTA ruim, música inadequada,
copyright incerto, violação de política.

O `subject` esperado é um dict descrevendo o vídeo produzido, por
exemplo (todos os campos são opcionais; ausentes => UNKNOWN):

{
  "audio_complete": bool,
  "duration_expected_seconds": float,
  "duration_actual_seconds": float,
  "narration_complete": bool,
  "static_ratio": float,          # 0-1, fração do vídeo sem movimento/corte
  "repeated_single_image": bool,
  "irrelevant_scenes": bool,
  "deformation_detected": bool,
  "sync_offset_ms": float,
  "captions_error_rate": float,   # 0-1
  "pronunciation_score": float,   # 0-1
  "thumbnail_score": float,       # 0-1
  "fact_check_status": "OK"|"UNKNOWN"|"FAILED",
  "cta_quality_score": float,     # 0-1
  "music_fit_score": float,       # 0-1
  "copyright_status": "CLEARED"|"UNKNOWN"|"AMBIGUOUS"|"INCOMPATIBLE",
  "policy_violation": bool,
}
"""

from __future__ import annotations

from core.quality.gates import GateResult, QualityGate, Verdict

# limites conservadores, ajustáveis conforme aprendizado real (Seção 52)
MAX_STATIC_RATIO = 0.35
MAX_SYNC_OFFSET_MS = 250
MAX_CAPTIONS_ERROR_RATE = 0.05
MIN_PRONUNCIATION_SCORE = 0.75
MIN_THUMBNAIL_SCORE = 0.5
MIN_CTA_SCORE = 0.5
MIN_MUSIC_FIT_SCORE = 0.4
MAX_DURATION_DELTA_RATIO = 0.15  # 15% de tolerância entre esperado x real


class VideoQualityGate(QualityGate):
    name = "video_quality_gate"

    def evaluate(self, subject: dict) -> GateResult:
        reasons: list[str] = []
        criteria: dict[str, str] = {}
        verdict = Verdict.APPROVED

        def worsen(new_verdict: Verdict, reason: str, key: str, status: str) -> None:
            nonlocal verdict
            order = [
                Verdict.BLOCKED, Verdict.REJECTED, Verdict.REWORK_REQUIRED,
                Verdict.REVIEW_REQUIRED, Verdict.UNKNOWN, Verdict.APPROVED,
            ]
            reasons.append(reason)
            criteria[key] = status
            if order.index(new_verdict) < order.index(verdict):
                verdict = new_verdict

        # --- áudio / narração ---
        audio_complete = subject.get("audio_complete")
        if audio_complete is False:
            worsen(Verdict.BLOCKED, "áudio cortado/incompleto", "audio_complete", "FAIL")
        elif audio_complete is None:
            worsen(Verdict.UNKNOWN, "status de áudio desconhecido", "audio_complete", "UNKNOWN")
        else:
            criteria["audio_complete"] = "OK"

        narration_complete = subject.get("narration_complete")
        if narration_complete is False:
            worsen(Verdict.BLOCKED, "narração incompleta", "narration_complete", "FAIL")
        elif narration_complete is None:
            worsen(Verdict.UNKNOWN, "status de narração desconhecido", "narration_complete", "UNKNOWN")
        else:
            criteria["narration_complete"] = "OK"

        # --- duração ---
        expected = subject.get("duration_expected_seconds")
        actual = subject.get("duration_actual_seconds")
        if expected is not None and actual is not None and expected > 0:
            delta_ratio = abs(actual - expected) / expected
            if delta_ratio > MAX_DURATION_DELTA_RATIO:
                worsen(
                    Verdict.REJECTED,
                    f"duração incoerente: esperado={expected}s real={actual}s",
                    "duration", "FAIL",
                )
            else:
                criteria["duration"] = "OK"
        else:
            worsen(Verdict.UNKNOWN, "duração esperada/real não informada", "duration", "UNKNOWN")

        # --- estático / imagem repetida / cenas irrelevantes ---
        static_ratio = subject.get("static_ratio")
        if static_ratio is not None:
            if static_ratio > MAX_STATIC_RATIO:
                worsen(Verdict.REWORK_REQUIRED, "vídeo praticamente estático", "static_ratio", "FAIL")
            else:
                criteria["static_ratio"] = "OK"
        else:
            worsen(Verdict.UNKNOWN, "static_ratio não avaliado", "static_ratio", "UNKNOWN")

        if subject.get("repeated_single_image"):
            worsen(Verdict.REJECTED, "imagem única repetida ao longo do vídeo", "repeated_single_image", "FAIL")

        if subject.get("irrelevant_scenes"):
            worsen(Verdict.REWORK_REQUIRED, "cenas irrelevantes ao roteiro", "irrelevant_scenes", "FAIL")

        if subject.get("deformation_detected"):
            worsen(Verdict.REJECTED, "deformação visual grave detectada", "deformation_detected", "FAIL")

        # --- sincronização / legendas / pronúncia ---
        sync_offset = subject.get("sync_offset_ms")
        if sync_offset is not None:
            if abs(sync_offset) > MAX_SYNC_OFFSET_MS:
                worsen(Verdict.REJECTED, f"sincronização ruim: offset={sync_offset}ms", "sync", "FAIL")
            else:
                criteria["sync"] = "OK"
        else:
            worsen(Verdict.UNKNOWN, "offset de sincronização não avaliado", "sync", "UNKNOWN")

        captions_error_rate = subject.get("captions_error_rate")
        if captions_error_rate is not None:
            if captions_error_rate > MAX_CAPTIONS_ERROR_RATE:
                worsen(Verdict.REWORK_REQUIRED, "taxa de erro de legendas acima do limite", "captions", "FAIL")
            else:
                criteria["captions"] = "OK"

        pronunciation_score = subject.get("pronunciation_score")
        if pronunciation_score is not None:
            if pronunciation_score < MIN_PRONUNCIATION_SCORE:
                worsen(Verdict.REWORK_REQUIRED, "pronúncia abaixo do mínimo aceitável", "pronunciation", "FAIL")
            else:
                criteria["pronunciation"] = "OK"

        # --- thumbnail / cta / música ---
        thumb_score = subject.get("thumbnail_score")
        if thumb_score is not None:
            if thumb_score < MIN_THUMBNAIL_SCORE:
                worsen(Verdict.REWORK_REQUIRED, "thumbnail fraca", "thumbnail", "FAIL")
            else:
                criteria["thumbnail"] = "OK"

        cta_score = subject.get("cta_quality_score")
        if cta_score is not None:
            if cta_score < MIN_CTA_SCORE:
                worsen(Verdict.REWORK_REQUIRED, "CTA de baixa qualidade", "cta", "FAIL")
            else:
                criteria["cta"] = "OK"

        music_fit = subject.get("music_fit_score")
        if music_fit is not None:
            if music_fit < MIN_MUSIC_FIT_SCORE:
                worsen(Verdict.REWORK_REQUIRED, "música inadequada ao contexto", "music_fit", "FAIL")
            else:
                criteria["music_fit"] = "OK"

        # --- fato / copyright / política ---
        fact_status = subject.get("fact_check_status")
        if fact_status == "FAILED":
            worsen(Verdict.REJECTED, "erro factual identificado", "fact_check", "FAIL")
        elif fact_status in (None, "UNKNOWN"):
            worsen(Verdict.UNKNOWN, "fact-check não confirmado", "fact_check", "UNKNOWN")
        else:
            criteria["fact_check"] = "OK"

        copyright_status = subject.get("copyright_status", "UNKNOWN")
        if copyright_status in ("UNKNOWN", "AMBIGUOUS", "INCOMPATIBLE"):
            worsen(Verdict.BLOCKED, f"copyright {copyright_status.lower()}", "copyright", copyright_status)
        else:
            criteria["copyright"] = "OK"

        if subject.get("policy_violation"):
            worsen(Verdict.BLOCKED, "violação de política identificada", "policy", "FAIL")

        return GateResult(verdict=verdict, reasons=reasons, criteria=criteria)
