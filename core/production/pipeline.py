"""
core.production.pipeline
==========================
Production Engine — pipeline completo (Seção 8):

IDEIA -> PESQUISA -> FACT CHECK -> PLANEJAMENTO -> ROTEIRO -> STORYBOARD
-> NARRAÇÃO -> CENAS -> VISUAIS -> 2D/3D/VÍDEO IA -> MÚSICA -> EFEITOS
-> EDIÇÃO -> LEGENDAS -> THUMBNAIL -> TÍTULO -> DESCRIÇÃO -> SEO -> CTA
-> QUALITY GATE -> PREVIEW -> PUBLICAÇÃO/AGENDAMENTO AUTORIZADO.

A pipeline nunca publica sozinha: o último estágio é sempre PREVIEW,
aguardando autorização explícita do Owner (Seção 27/72).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.production.copyright_gate import AssetRights, CopyrightGate
from core.production.discovery_engines import SeoEngine, ThumbnailEngine, TitleEngine
from core.production.narration import assert_clean_for_voice, split_raw_script
from core.production.scene_engine import ScenePlanRequest, plan_scenes
from core.production.script_engine import FactClaim, assess_script, fact_check
from core.production.sync_validator import SyncCheckInput, validate_sync
from core.quality.gates import Verdict
from core.quality.video_quality import VideoQualityGate


class PipelineStage(str, Enum):
    IDEA = "IDEA"
    RESEARCH = "RESEARCH"
    FACT_CHECK = "FACT_CHECK"
    PLANNING = "PLANNING"
    SCRIPT = "SCRIPT"
    NARRATION = "NARRATION"
    SCENES = "SCENES"
    VISUALS = "VISUALS"
    MUSIC = "MUSIC"
    EDITING = "EDITING"
    CAPTIONS = "CAPTIONS"
    THUMBNAIL = "THUMBNAIL"
    TITLE = "TITLE"
    SEO = "SEO"
    COPYRIGHT_GATE = "COPYRIGHT_GATE"
    QUALITY_GATE = "QUALITY_GATE"
    PREVIEW = "PREVIEW"
    AWAITING_OWNER_AUTHORIZATION = "AWAITING_OWNER_AUTHORIZATION"


@dataclass
class StageResult:
    stage: PipelineStage
    ok: bool
    details: dict = field(default_factory=dict)
    blocking_reasons: list[str] = field(default_factory=list)


@dataclass
class PipelineRun:
    project_id: str
    channel_id: str
    stage_results: list[StageResult] = field(default_factory=list)
    final_verdict: Verdict | None = None
    blocked: bool = False

    def record(self, result: StageResult) -> None:
        self.stage_results.append(result)
        if not result.ok:
            self.blocked = True


class ProductionPipeline:
    """Orquestra os estágios usando as engines especializadas. Cada
    estágio recebe dados já produzidos (por um worker/adapter externo)
    e aplica validação — a pipeline em si não "inventa" conteúdo."""

    def __init__(self) -> None:
        self.title_engine = TitleEngine()
        self.thumbnail_engine = ThumbnailEngine()
        self.seo_engine = SeoEngine()
        self.copyright_gate = CopyrightGate()
        self.video_quality_gate = VideoQualityGate()

    def run_fact_check(self, run: PipelineRun, claims: list[FactClaim]) -> StageResult:
        report = fact_check(claims)
        ok = report.status.value in ("OK",)
        result = StageResult(
            PipelineStage.FACT_CHECK,
            ok=ok,
            details={"status": report.status.value, "unresolved": report.unresolved_claims},
            blocking_reasons=[] if ok else [f"fact-check: {report.status.value}"],
        )
        run.record(result)
        return result

    def run_script_stage(
        self, run: PipelineRun, raw_script: str, *, has_cta: bool, has_hook: bool
    ) -> StageResult:
        assessment = assess_script(raw_script, has_cta=has_cta, has_hook_in_first_lines=has_hook)
        ok = assessment.score >= 0.6
        result = StageResult(
            PipelineStage.SCRIPT,
            ok=ok,
            details={"score": assessment.score, "issues": assessment.issues},
            blocking_reasons=[] if ok else assessment.issues,
        )
        run.record(result)
        return result

    def run_narration_stage(self, run: PipelineRun, raw_script: str) -> StageResult:
        structured = split_raw_script(raw_script)
        try:
            assert_clean_for_voice(structured.narration_text)
            ok = True
            reasons: list[str] = []
        except Exception as exc:  # NarrationLeakDetected
            ok = False
            reasons = [str(exc)]
        result = StageResult(
            PipelineStage.NARRATION,
            ok=ok,
            details={"narration_text": structured.narration_text},
            blocking_reasons=reasons,
        )
        run.record(result)
        return result

    def run_scene_stage(self, run: PipelineRun, request: ScenePlanRequest) -> StageResult:
        try:
            plan = plan_scenes(request)
            result = StageResult(
                PipelineStage.SCENES,
                ok=True,
                details={"scene_count": plan.scene_count, "avg_interval_seconds": plan.avg_interval_seconds},
            )
        except Exception as exc:  # StaticVideoRejected
            result = StageResult(PipelineStage.SCENES, ok=False, blocking_reasons=[str(exc)])
        run.record(result)
        return result

    def run_sync_stage(self, run: PipelineRun, check_input: SyncCheckInput) -> StageResult:
        sync_result = validate_sync(check_input)
        result = StageResult(
            PipelineStage.EDITING,
            ok=sync_result.ok,
            details={"estimated_narration_seconds": sync_result.estimated_narration_seconds},
            blocking_reasons=sync_result.issues,
        )
        run.record(result)
        return result

    def run_copyright_stage(self, run: PipelineRun, assets: list[AssetRights]) -> StageResult:
        gate_result = self.copyright_gate.evaluate(assets)
        result = StageResult(
            PipelineStage.COPYRIGHT_GATE,
            ok=gate_result.approved,
            details={"blocked_assets": gate_result.blocked_assets},
            blocking_reasons=gate_result.reasons,
        )
        run.record(result)
        return result

    def run_title_thumbnail_seo(
        self,
        run: PipelineRun,
        *,
        title: str,
        description: str,
        tags: list[str],
        promised_content_summary: str,
        search_intent_keywords: list[str],
        thumbnail_matches_content: bool,
        thumbnail_has_focal_point: bool,
        thumbnail_text_legible: bool,
    ) -> StageResult:
        title_assessment = self.title_engine.evaluate(title, promised_content_summary=promised_content_summary)
        thumb_assessment = self.thumbnail_engine.evaluate(
            matches_content=thumbnail_matches_content,
            has_clear_focal_point=thumbnail_has_focal_point,
            text_legible_at_small_size=thumbnail_text_legible,
        )
        seo_assessment = self.seo_engine.evaluate(
            title=title, description=description, tags=tags, search_intent_keywords=search_intent_keywords
        )

        ok = (not title_assessment.misleading) and title_assessment.score >= 0.5 and thumb_assessment.score >= 0.5
        reasons = title_assessment.issues + thumb_assessment.issues + seo_assessment.issues

        result = StageResult(
            PipelineStage.TITLE,
            ok=ok,
            details={
                "title_score": title_assessment.score,
                "thumbnail_score": thumb_assessment.score,
                "seo_score": seo_assessment.score,
            },
            blocking_reasons=[] if ok else reasons,
        )
        run.record(result)
        return result

    def run_quality_gate(self, run: PipelineRun, video_subject: dict) -> StageResult:
        gate_result = self.video_quality_gate.evaluate(video_subject)
        run.final_verdict = gate_result.verdict
        ok = gate_result.verdict == Verdict.APPROVED
        result = StageResult(
            PipelineStage.QUALITY_GATE,
            ok=ok,
            details={"verdict": gate_result.verdict.value, "criteria": gate_result.criteria},
            blocking_reasons=gate_result.reasons,
        )
        run.record(result)
        return result

    def finalize_preview(self, run: PipelineRun) -> StageResult:
        """Estágio final: nunca publica. Sempre aguarda autorização do Owner."""
        if run.blocked:
            result = StageResult(
                PipelineStage.PREVIEW,
                ok=False,
                blocking_reasons=["pipeline bloqueada em estágio anterior; preview não liberado"],
            )
        else:
            result = StageResult(
                PipelineStage.AWAITING_OWNER_AUTHORIZATION,
                ok=True,
                details={"message": "preview pronto; aguardando autorização explícita do Owner para publicar"},
            )
        run.record(result)
        return result
