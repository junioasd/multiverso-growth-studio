"""
atlas.radar.engine
=====================
ATLAS+RADAR — item 1: motor do Radar Permanente de Oportunidades
Comerciais.

`CommercialOpportunityRadar` não mantém uma lista fixa — cada chamada
de `scan()` consulta TODAS as fontes registradas de novo (nunca
retorna um cache estático como se fosse "a verdade atual"). O registro
de fontes (`atlas.radar.sources.build_default_sources`) é extensível
sem alterar este módulo: uma nova plataforma só precisa de um novo
`OpportunitySource` registrado.

O radar gera um RANKING, nunca uma execução: `rank()` devolve
`RankedOpportunity` com score/confiança/justificativa — publicar,
cadastrar conta, gastar, contratar ou conectar continuam
EXCLUSIVAMENTE em `atlas.director.AtlasDirector.request_critical_action`
(nunca neste módulo).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from atlas.radar.models import (
    ChannelType,
    CommercialOpportunity,
    EvidenceLevel,
    RankedOpportunity,
)
from atlas.radar.quality_gate import OpportunityEvidenceGate
from atlas.radar.sources import OpportunitySource, build_default_sources
from commercial.learning_loop import LearningLoop
from core.quality.gates import Verdict


@dataclass
class TrackedOpportunity:
    opportunity: CommercialOpportunity
    first_seen_at: float
    last_seen_at: float
    scans: int = 1


class CommercialOpportunityRadar:
    def __init__(self, *, sources: list[OpportunitySource] | None = None) -> None:
        self.sources: list[OpportunitySource] = sources if sources is not None else build_default_sources()
        self.evidence_gate = OpportunityEvidenceGate()
        self._tracked: dict[str, TrackedOpportunity] = {}

    # ------------------------------------------------------------------
    # Descoberta
    # ------------------------------------------------------------------

    def scan(
        self, *, channel_types: set[ChannelType] | None = None, niche: str | None = None,
    ) -> list[CommercialOpportunity]:
        opportunities: list[CommercialOpportunity] = []
        for source in self.sources:
            if channel_types is not None and source.channel_type not in channel_types:
                continue
            opportunities.extend(source.discover(niche=niche))

        now = time.time()
        for opp in opportunities:
            tracked = self._tracked.get(opp.opportunity_id)
            if tracked is None:
                self._tracked[opp.opportunity_id] = TrackedOpportunity(
                    opportunity=opp, first_seen_at=now, last_seen_at=now,
                )
            else:
                tracked.opportunity = opp
                tracked.last_seen_at = now
                tracked.scans += 1

        return opportunities

    def tracked_opportunities(self) -> list[TrackedOpportunity]:
        return list(self._tracked.values())

    # ------------------------------------------------------------------
    # Ranking (integra o Quality Gate de evidência — item 5)
    # ------------------------------------------------------------------

    def rank(
        self,
        opportunities: list[CommercialOpportunity],
        *,
        policy_compatible_by_id: dict[str, bool] | None = None,
        risk_level_by_id: dict[str, str] | None = None,
        learning_loop: LearningLoop | None = None,
    ) -> list[RankedOpportunity]:
        policy_compatible_by_id = policy_compatible_by_id or {}
        risk_level_by_id = risk_level_by_id or {}

        ranked: list[RankedOpportunity] = []
        for opp in opportunities:
            gate_subject = {
                "opportunity": opp,
                "policy_compatible": policy_compatible_by_id.get(opp.opportunity_id),
                "risk_level": risk_level_by_id.get(opp.opportunity_id),
                "contradictory_data": self.has_contradictory_signals(opp),
            }
            gate_result = self.evidence_gate.evaluate(gate_subject)

            justification = list(gate_result.reasons)
            score, confidence = self._score(opp, gate_result.verdict)

            if learning_loop is not None:
                pattern = learning_loop.pattern_for(self.learning_scope_key(opp))
                if pattern.observations > 0:
                    justification.append(
                        f"Aprendizado prévio: {pattern.observations} observação(ões), "
                        f"{pattern.worked_count} funcionou(aram), confiança {pattern.confidence} "
                        "(apenas informativo — não altera o veredito do Quality Gate)."
                    )

            ranked.append(RankedOpportunity(
                opportunity=opp, score=score, confidence=confidence, justification=justification,
            ))

        def sort_key(ro: RankedOpportunity):
            return (0, -ro.score) if isinstance(ro.score, float) else (1, 0)

        ranked.sort(key=sort_key)
        return ranked

    @staticmethod
    def has_contradictory_signals(opp: CommercialOpportunity) -> bool:
        cost = opp.signal("cost")
        margin = opp.signal("margin_potential")
        if (
            cost.evidence != EvidenceLevel.UNKNOWN and margin.evidence != EvidenceLevel.UNKNOWN
            and isinstance(cost.value, (int, float)) and isinstance(margin.value, (int, float))
            and margin.value < 0 and cost.value <= 0
        ):
            return True
        return False

    @staticmethod
    def _score(opp: CommercialOpportunity, verdict: Verdict) -> tuple:
        if verdict != Verdict.APPROVED:
            return "UNKNOWN", "LOW"

        numeric_signals = []
        for key in ("margin_potential", "roi", "purchase_intent"):
            s = opp.signal(key)
            if isinstance(s.value, (int, float)) and s.evidence != EvidenceLevel.UNKNOWN:
                numeric_signals.append(s.value)

        if not numeric_signals:
            return "UNKNOWN", "LOW"

        score = sum(numeric_signals) / len(numeric_signals)
        confidence = "MEDIUM" if len(numeric_signals) >= 2 else "LOW"
        return round(score, 4), confidence

    @staticmethod
    def learning_scope_key(opp: CommercialOpportunity) -> str:
        return f"radar:{opp.channel_type.value}:{opp.offer_type.value}"
