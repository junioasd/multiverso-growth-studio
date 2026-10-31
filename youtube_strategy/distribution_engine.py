"""
youtube_strategy.distribution_engine
=========================================
BLOCO FINAL 1 (item 4 — YOUTUBE RECOMMENDATION & DISTRIBUTION ENGINE).
Estende `strategy_engine.py`/`alignment_checker.py` (já existentes,
preservados integralmente) com:

- avaliação REAL de embalagem (título/thumbnail) — regras explícitas
  e auditáveis (mesma disciplina de `core.agents.runtime.juris`),
  nunca uma "pontuação de clique" inventada;
- decisão REAL de formato (Shorts vs. vídeo longo) baseada em
  características do próprio conteúdo (duração, ritmo) — nunca uma
  recomendação genérica sem esses dados;
- planejamento de distribuição integrado ao Learning Loop
  quantitativo (`core.learning.loop.LearningLoop`) — aprendizado REAL
  por canal E por vídeo, nunca compartilhado entre canais sem
  validação estatística e marcação explícita `applies_globally`.

Nunca usa manipulação, spam ou métricas falsas — toda recomendação
aqui é auditável e rastreável à regra ou ao aprendizado validado que a
gerou.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.learning.loop import LearningLoop, Prediction


class ContentFormat(str, Enum):
    SHORT = "SHORT"
    LONG_FORM = "LONG_FORM"


@dataclass
class ContentCharacteristics:
    """Características REAIS do conteúdo já produzido/planejado —
    nunca inferidas sem esses dados."""

    duration_seconds: float | None = None
    has_strong_single_hook: bool | None = None  # um unico gancho central, tipico de Shorts
    requires_deep_explanation: bool | None = None  # topico exige desenvolvimento longo


@dataclass
class FormatDecision:
    recommended_format: ContentFormat | None
    reasoning: list = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class PackagingEvaluation:
    verdict: str  # "OK" | "NEEDS_REVIEW" | "BLOCKED"
    issues: list = field(default_factory=list)


_MISLEADING_MARKERS = ("!!!", "???", "VOCÊ NÃO VAI ACREDITAR", "CHOCANTE", "NUNCA MAIS")


class PackagingEvaluator:
    """Avaliação REAL de título/thumbnail — regras explícitas,
    auditáveis (nunca uma pontuação de CTR inventada). Nunca aprova
    título/thumbnail sem checar contra os marcadores conhecidos de
    clickbait enganoso (Seção 24 — `ProhibitedGrowthTactic.
    MISLEADING_CLICKBAIT`)."""

    def evaluate_title(self, title: str) -> PackagingEvaluation:
        if not title or not title.strip():
            return PackagingEvaluation(verdict="BLOCKED", issues=["título vazio"])
        issues = []
        upper_ratio = sum(1 for c in title if c.isupper()) / max(len(title), 1)
        if upper_ratio > 0.6 and len(title) > 10:
            issues.append("proporção excessiva de maiúsculas (padrão de clickbait)")
        for marker in _MISLEADING_MARKERS:
            if marker.lower() in title.lower():
                issues.append(f"marcador de clickbait enganoso detectado: '{marker}'")
        if len(title) > 100:
            issues.append("título excede 100 caracteres — pode ser truncado na exibição")
        if issues:
            return PackagingEvaluation(verdict="NEEDS_REVIEW", issues=issues)
        return PackagingEvaluation(verdict="OK", issues=[])

    def evaluate_thumbnail_matches_content(self, *, thumbnail_matches_content: bool | None) -> PackagingEvaluation:
        """Nunca avalia a IMAGEM em si (fora do alcance deste
        ambiente sem provider multimodal real) — avalia apenas se o
        chamador (humano ou VERITAS) confirmou correspondência real
        entre thumbnail e conteúdo. `None` é sempre pendência, nunca
        aprovação por omissão."""
        if thumbnail_matches_content is None:
            return PackagingEvaluation(verdict="NEEDS_REVIEW", issues=["correspondência thumbnail↔conteúdo não confirmada"])
        if thumbnail_matches_content is False:
            return PackagingEvaluation(verdict="BLOCKED", issues=["thumbnail não corresponde ao conteúdo real (clickbait enganoso)"])
        return PackagingEvaluation(verdict="OK", issues=[])


class FormatSelector:
    """Decisão REAL de formato (Shorts vs. vídeo longo) — nunca
    recomenda sem características reais do conteúdo."""

    def decide(self, characteristics: ContentCharacteristics) -> FormatDecision:
        reasoning = []
        if characteristics.duration_seconds is None:
            return FormatDecision(recommended_format=None, reasoning=["duration_seconds ausente — sem dado suficiente"], confidence=0.0)

        if characteristics.duration_seconds <= 60:
            reasoning.append(f"duração ({characteristics.duration_seconds:.0f}s) compatível com Shorts")
            if characteristics.requires_deep_explanation:
                reasoning.append("porém o tópico exige explicação profunda — considerar expandir para vídeo longo")
                return FormatDecision(recommended_format=ContentFormat.LONG_FORM, reasoning=reasoning, confidence=0.6)
            return FormatDecision(recommended_format=ContentFormat.SHORT, reasoning=reasoning, confidence=0.9)

        reasoning.append(f"duração ({characteristics.duration_seconds:.0f}s) excede o limite de Shorts")
        if characteristics.has_strong_single_hook and characteristics.duration_seconds <= 180:
            reasoning.append("gancho único forte e duração moderada — poderia ser cortado em Short adicional")
        return FormatDecision(recommended_format=ContentFormat.LONG_FORM, reasoning=reasoning, confidence=0.85)


class DistributionPlanner:
    """Combina os aprendizados VALIDADOS (nunca especulativos) do
    Learning Loop, escopados por canal/vídeo, com as regras de
    embalagem/formato acima — nunca recomenda algo fora do que a
    Seção 24 permite (`YouTubeStrategyEngine.request_growth_action()`
    continua sendo o ponto único de bloqueio de táticas proibidas)."""

    def __init__(self, *, learning_loop: LearningLoop | None = None) -> None:
        self.learning_loop = learning_loop if learning_loop is not None else LearningLoop()
        self.packaging_evaluator = PackagingEvaluator()
        self.format_selector = FormatSelector()

    def plan_distribution(
        self, *, channel_id: str, title: str, characteristics: ContentCharacteristics,
        thumbnail_matches_content: bool | None = None,
    ) -> dict:
        title_eval = self.packaging_evaluator.evaluate_title(title)
        thumb_eval = self.packaging_evaluator.evaluate_thumbnail_matches_content(
            thumbnail_matches_content=thumbnail_matches_content,
        )
        format_decision = self.format_selector.decide(characteristics)
        applicable_learnings = self.learning_loop.learnings_visible_to(scope_channel_id=channel_id)

        blocked = title_eval.verdict == "BLOCKED" or thumb_eval.verdict == "BLOCKED"
        return {
            "channel_id": channel_id,
            "title_evaluation": {"verdict": title_eval.verdict, "issues": title_eval.issues},
            "thumbnail_evaluation": {"verdict": thumb_eval.verdict, "issues": thumb_eval.issues},
            "format_decision": {
                "recommended_format": format_decision.recommended_format.value if format_decision.recommended_format else None,
                "reasoning": format_decision.reasoning, "confidence": format_decision.confidence,
            },
            "applicable_learnings": [
                {"statement": l.statement, "sample_size": l.sample_size} for l in applicable_learnings
            ],
            "ready_to_publish": not blocked and title_eval.verdict != "NEEDS_REVIEW" and thumb_eval.verdict != "NEEDS_REVIEW",
        }

    def record_video_prediction(
        self, *, channel_id: str, video_id: str, metric_name: str, predicted_value: float, context: str,
    ) -> Prediction:
        """Aprendizado por VÍDEO (não só por canal) — `video_id` entra
        no contexto para rastreabilidade; o escopo estatístico
        continua sendo `scope_channel_id` (canal), já que um único
        vídeo nunca gera amostra suficiente sozinho para validar um
        aprendizado (mínimo de observações do Learning Loop)."""
        return self.learning_loop.record_prediction(Prediction.new(
            agent_id="PULSE", metric_name=metric_name, predicted_value=predicted_value,
            context=f"video={video_id}: {context}", scope_channel_id=channel_id,
        ))
