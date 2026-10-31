"""
commercial.omnichannel_intelligence
========================================
BLOCO FINAL 1 (item 5 — OMNICHANNEL CAMPAIGN INTELLIGENCE). Inteligência
única para ORGÂNICO + ADS, mesmo com orçamento de mídia paga = ZERO.

Princípios centrais (nunca violados):
- Mede performance usando as dimensões REAIS já rastreadas por
  `commercial.sales_tracking.SalesTracker` (canal, plataforma,
  produto, campanha, funil, fonte de tráfego) — nunca uma métrica
  paralela inventada.
- Aprende e propaga PRINCÍPIOS (padrões nomeados e descritos —
  "gancho de pergunta nos primeiros 3s", "prova social explícita")
  — NUNCA o criativo verbatim de terceiros. `record_principle()`
  aceita apenas uma descrição textual do PADRÃO observado, nunca uma
  cópia do texto/imagem original.
- Aprendizado bidirecional real: um princípio observado em ADS pode
  ser sugerido para adaptação ORGÂNICA, e vice-versa — sempre como
  SUGESTÃO explícita (nunca aplicado automaticamente), sempre
  validado estatisticamente antes de propagar (reaproveita
  `core.learning.loop.LearningLoop` integralmente — mesma barra de
  validação e isolamento de escopo já testados).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from commercial.sales_tracking import SalesSummary, SalesTracker
from core.learning.loop import LearningLoop


class ChannelType(str, Enum):
    ORGANIC = "ORGANIC"
    PAID_ADS = "PAID_ADS"
    UNKNOWN = "UNKNOWN"


class PrincipleCategory(str, Enum):
    HOOK = "hook"
    ANGLE = "angle"
    OFFER = "offer"
    CREATIVE_STYLE = "creative_style"
    CTA = "cta"
    FORMAT = "format"
    PACING = "pacing"
    PROOF = "proof"
    CONVERSION = "conversion"


# CORREÇÃO: classificação EXPLÍCITA e auditável (nunca "adivinha") —
# marcadores conhecidos de tráfego pago; qualquer coisa fora disso é
# `UNKNOWN`, nunca assumida como orgânica por omissão.
_PAID_MARKERS = ("ads", "cpc", "cpm", "paid", "sponsored", "adwords", "facebook_ads", "google_ads", "tiktok_ads")
_PAID_MARKERS_PT = ("patrocinad", "impulsionad", "anuncio", "anúncio", "trafego_pago", "tráfego_pago", "midia_paga",
                    "mídia_paga", "pago")
_ORGANIC_MARKERS = ("organic", "organico", "orgânico", "seo", "busca_natural")


@dataclass
class CreativePrinciple:
    """Um PADRÃO observado e nomeado — nunca o criativo original em
    si. `description` deve ser uma descrição do PADRÃO (ex: 'pergunta
    retórica nos primeiros 3 segundos'), nunca uma cópia verbatim de
    texto/imagem de terceiros — a responsabilidade de nunca colar
    conteúdo de terceiros aqui é de quem CHAMA esta API; o sistema não
    tem como detectar isso automaticamente, mas o campo em si é
    estruturalmente uma DESCRIÇÃO curta, não um espaço para colar
    criativo completo."""

    principle_id: str
    category: PrincipleCategory
    description: str
    origin_channel_type: ChannelType
    origin_channel_id: str | None
    origin_campaign_id: str | None
    recorded_at: float = field(default_factory=time.time)

    @staticmethod
    def new(
        *, category: PrincipleCategory, description: str, origin_channel_type: ChannelType,
        origin_channel_id: str | None = None, origin_campaign_id: str | None = None,
    ) -> "CreativePrinciple":
        if not description or len(description) > 500:
            raise ValueError(
                "description deve ser uma descrição curta do PADRÃO observado (até 500 caracteres) — "
                "nunca o criativo original completo"
            )
        return CreativePrinciple(
            principle_id=f"principle_{uuid.uuid4().hex[:12]}", category=category, description=description,
            origin_channel_type=origin_channel_type, origin_channel_id=origin_channel_id,
            origin_campaign_id=origin_campaign_id,
        )


@dataclass
class PropagationSuggestion:
    """Uma SUGESTÃO de aplicar um princípio no sentido oposto
    (ads->organico ou organico->ads) — nunca uma aplicação automática.
    Precisa ser explicitamente aprovada por quem consome (Owner/ARGOS/
    SCRIPTOR) antes de virar conteúdo real."""

    suggestion_id: str
    principle_id: str
    principle_description: str
    from_channel_type: ChannelType
    to_channel_type: ChannelType
    rationale: str
    sample_size: int
    direction_accuracy: float


class OmnichannelIntelligence:
    def __init__(self, *, sales_tracker: SalesTracker | None = None, learning_loop: LearningLoop | None = None) -> None:
        self.sales_tracker = sales_tracker
        self.learning_loop = learning_loop if learning_loop is not None else LearningLoop()
        self._principles: dict = {}

    # ------------------------------------------------------------------
    # Classificação e medição — sempre sobre dados REAIS já rastreados
    # ------------------------------------------------------------------

    def classify_traffic_source(self, traffic_source: str | None) -> ChannelType:
        """CORREÇÃO: nunca adivinha — marcadores EXPLÍCITOS de tráfego
        pago; ausência de marcador nunca vira 'orgânico' por padrão
        quando `traffic_source` está vazio (vira `UNKNOWN`, honesto)."""
        if not traffic_source:
            return ChannelType.UNKNOWN
        lowered = traffic_source.lower()
        if any(marker in lowered for marker in _PAID_MARKERS + _PAID_MARKERS_PT):
            return ChannelType.PAID_ADS
        # COMANDO 5/6 (defeito provado): QUALQUER fonte sem marcador pago
        # em inglês virava ORGÂNICO — "instagram_patrocinado",
        # "post_impulsionado", "trafego_pago" (PAGOS) e "email"/"direto"/
        # "indicacao" (sem base) recebiam atribuição inventada. Orgânico
        # agora exige marcador EXPLÍCITO; o resto é UNKNOWN.
        if any(marker in lowered for marker in _ORGANIC_MARKERS):
            return ChannelType.ORGANIC
        return ChannelType.UNKNOWN

    def measure_performance(
        self, *, channel_id=None, product_id=None, campaign_id=None, funnel_id=None, platform=None,
        traffic_source=None,
    ) -> SalesSummary | None:
        """Delega INTEGRALMENTE para `SalesTracker.summarize_filtered()`
        — nunca recalcula métricas paralelas. `None` quando nenhum
        `SalesTracker` real está conectado (honesto, nunca fabrica)."""
        if self.sales_tracker is None:
            return None
        return self.sales_tracker.summarize_filtered(
            channel_id=channel_id, product_id=product_id, campaign_id=campaign_id, funnel_id=funnel_id,
            platform=platform, traffic_source=traffic_source,
        )

    def compare_organic_vs_ads(self, *, channel_id: str | None = None) -> dict:
        """Compara performance real organico vs ads DENTRO do mesmo
        canal — usa os `traffic_source`s REALMENTE registrados (nunca
        uma lista fixa) via `SalesTracker.distinct_values()`."""
        if self.sales_tracker is None:
            return {"status": "UNAVAILABLE", "reason": "nenhum SalesTracker real conectado"}
        sources = self.sales_tracker.distinct_values("traffic_source", channel_id=channel_id)
        organic_sources = [s for s in sources if self.classify_traffic_source(s) == ChannelType.ORGANIC]
        ads_sources = [s for s in sources if self.classify_traffic_source(s) == ChannelType.PAID_ADS]

        def _aggregate(source_list):
            if not source_list:
                return None
            total_revenue = 0.0
            total_sales = 0
            for src in source_list:
                summary = self.sales_tracker.summarize_filtered(channel_id=channel_id, traffic_source=src)
                total_revenue += summary.real_revenue
                total_sales += summary.sales
            return {"sources": source_list, "real_revenue": total_revenue, "sales": total_sales}

        return {
            "channel_id": channel_id, "organic": _aggregate(organic_sources), "ads": _aggregate(ads_sources),
            "unclassified_sources": [s for s in sources if self.classify_traffic_source(s) == ChannelType.UNKNOWN],
        }

    # ------------------------------------------------------------------
    # Princípios e aprendizado bidirecional — nunca cópia de criativo
    # ------------------------------------------------------------------

    def record_principle(self, principle: CreativePrinciple) -> CreativePrinciple:
        self._principles[principle.principle_id] = principle
        return principle

    def suggest_bidirectional_propagation(
        self, *, principle_id: str, metric_name: str, scope_channel_id: str | None = None,
    ) -> PropagationSuggestion | None:
        """CORREÇÃO (item 5 — aprendizado bidirecional real): só
        sugere propagação (ads->organico ou organico->ads) quando o
        Learning Loop já VALIDOU estatisticamente o princípio
        associado à métrica dada (mesma barra de
        `LearningLoop.evaluate_learning()` — nunca uma sugestão
        especulativa). `None` quando o princípio é desconhecido ou o
        aprendizado ainda não foi validado."""
        principle = self._principles.get(principle_id)
        if principle is None:
            return None

        applicable = self.learning_loop.learnings_visible_to(scope_channel_id=scope_channel_id)
        matching_learning = next((l for l in applicable if l.metric_name == metric_name), None)
        if matching_learning is None:
            return None

        target = ChannelType.PAID_ADS if principle.origin_channel_type == ChannelType.ORGANIC else ChannelType.ORGANIC
        return PropagationSuggestion(
            suggestion_id=f"sugg_{uuid.uuid4().hex[:12]}", principle_id=principle_id,
            principle_description=principle.description, from_channel_type=principle.origin_channel_type,
            to_channel_type=target,
            rationale=(
                f"princípio '{principle.description}' observado em {principle.origin_channel_type.value} "
                f"tem aprendizado validado para '{metric_name}' ({matching_learning.sample_size} amostras, "
                f"{matching_learning.direction_accuracy:.0%} de consistência) — candidato a adaptação "
                f"ORIGINAL em {target.value} (nunca cópia direta do criativo)"
            ),
            sample_size=matching_learning.sample_size, direction_accuracy=matching_learning.direction_accuracy,
        )

    def principles_by_category(self, category: PrincipleCategory) -> list:
        return [p for p in self._principles.values() if p.category == category]
