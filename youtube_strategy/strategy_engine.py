"""
youtube_strategy.strategy_engine
===================================
YouTube Strategy Engine (Seção 22-25). Objetivo: trabalhar a favor dos
sistemas de IA/recomendação do YouTube de forma LEGÍTIMA — nunca
enganar o algoritmo.

Seção 24 proíbe explicitamente: fake views/likes/comments, bot farms,
fake subscribers, spam, engagement manipulation, account farming,
clickbait enganoso, metadata stuffing, mass duplicate content, policy
bypass. Este módulo não implementa nenhuma dessas práticas — ele existe
justamente para detectá-las e bloqueá-las antes da publicação.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProhibitedGrowthTactic(str, Enum):
    FAKE_VIEWS = "fake_views"
    FAKE_LIKES = "fake_likes"
    FAKE_COMMENTS = "fake_comments"
    BOT_FARMS = "bot_farms"
    FAKE_SUBSCRIBERS = "fake_subscribers"
    SPAM = "spam"
    ENGAGEMENT_MANIPULATION = "engagement_manipulation"
    ACCOUNT_FARMING = "account_farming"
    MISLEADING_CLICKBAIT = "misleading_clickbait"
    METADATA_STUFFING = "metadata_stuffing"
    MASS_DUPLICATE_CONTENT = "mass_duplicate_content"
    POLICY_BYPASS = "policy_bypass"


class ProhibitedTacticRequested(Exception):
    pass


@dataclass
class LegitimateSignals:
    """Sinais reais que o motor deve otimizar organicamente — nunca
    manipulados artificialmente (Seção 22)."""

    retention_rate: float | None = None      # 0-1
    watch_time_seconds: float | None = None
    ctr: float | None = None                  # 0-1
    satisfaction_score: float | None = None     # 0-1 (thumbs up/down, surveys)
    search_intent_match: float | None = None      # 0-1
    audience_relevance: float | None = None          # 0-1
    channel_history_score: float | None = None         # 0-1


@dataclass
class StrategyRecommendation:
    focus_areas: list[str]
    reasoning: list[str] = field(default_factory=list)


class YouTubeStrategyEngine:
    def request_growth_action(self, tactic: str) -> None:
        """Qualquer tentativa de solicitar uma tática proibida é
        recusada aqui — ponto único de bloqueio (Seção 24/45)."""
        if tactic in {t.value for t in ProhibitedGrowthTactic}:
            raise ProhibitedTacticRequested(
                f"tática de crescimento proibida solicitada: '{tactic}'. "
                "O sistema só trabalha com distribuição orgânica legítima."
            )

    def recommend_focus(self, signals: LegitimateSignals) -> StrategyRecommendation:
        focus_areas: list[str] = []
        reasoning: list[str] = []

        if signals.retention_rate is not None and signals.retention_rate < 0.4:
            focus_areas.append("retenção")
            reasoning.append(f"retenção baixa ({signals.retention_rate:.0%}): revisar ritmo/hook/roteiro")

        if signals.ctr is not None and signals.ctr < 0.02:
            focus_areas.append("thumbnail_e_titulo")
            reasoning.append(f"CTR baixo ({signals.ctr:.1%}): revisar thumbnail/título (sem clickbait enganoso)")

        if signals.search_intent_match is not None and signals.search_intent_match < 0.5:
            focus_areas.append("seo_e_intencao_de_busca")
            reasoning.append("baixa correspondência com intenção de busca: revisar SEO/tópico")

        if signals.satisfaction_score is not None and signals.satisfaction_score < 0.5:
            focus_areas.append("qualidade_de_conteudo")
            reasoning.append("satisfação baixa: sinal de que o conteúdo não entrega a promessa")

        if not focus_areas:
            focus_areas.append("manter_estrategia_atual")
            reasoning.append("sinais dentro do esperado ou insuficientes para mudança de rumo")

        return StrategyRecommendation(focus_areas=focus_areas, reasoning=reasoning)
