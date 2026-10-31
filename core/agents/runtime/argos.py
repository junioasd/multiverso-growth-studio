"""
core.agents.runtime.argos
=============================
BLOCO FINAL 1 (item 1) — ARGOS: agente REAL de estratégia de
conteúdo e planejamento editorial. Combina os aprendizados
VALIDADOS (nunca especulativos) do Learning Loop com prioridades
explícitas fornecidas pelo chamador — nunca "inventa" uma pauta sem
nenhum insumo real: sem aprendizados validados nem prioridades
explícitas, devolve `INSUFFICIENT_INPUT`, nunca um plano genérico.
"""

from __future__ import annotations

from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability
from core.learning.loop import LearningLoop


class ArgosAgent(RealAgentContract):
    agent_id = "ARGOS"
    primary_task_types = frozenset({"BUILD_CONTENT_PLAN", "PRIORITIZE_TOPICS"})
    all_rounder_task_types = frozenset({"PRIORITIZE_TOPICS"})
    max_input_items = 200

    def __init__(self, *, learning_loop: LearningLoop | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.learning_loop = learning_loop

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type == "BUILD_CONTENT_PLAN":
            return self._build_content_plan(payload)
        if task_type == "PRIORITIZE_TOPICS":
            return self._prioritize_topics(payload)
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _build_content_plan(self, payload: dict) -> AgentTaskResult:
        candidate_topics = payload.get("candidate_topics")
        if not candidate_topics or not isinstance(candidate_topics, list):
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="BUILD_CONTENT_PLAN",
                reason="candidate_topics (lista de tópicos candidatos) é obrigatório — ARGOS nunca inventa pauta do zero",
            )
        scope_channel_id = payload.get("scope_channel_id")
        applicable_learnings = []
        if self.learning_loop is not None:
            applicable_learnings = self.learning_loop.learnings_visible_to(scope_channel_id=scope_channel_id)

        # CORREÇÃO: ordena por relevância REAL -- topicos que casam com
        # o statement de algum aprendizado validado sobem de prioridade;
        # nunca fabrica um motivo quando nao ha match algum.
        ranked = []
        for topic in candidate_topics:
            matches = [
                l.statement for l in applicable_learnings
                if isinstance(topic, str) and any(word.lower() in topic.lower() for word in l.statement.split())
            ]
            ranked.append({"topic": topic, "supporting_learnings": matches, "priority_score": len(matches)})
        ranked.sort(key=lambda t: t["priority_score"], reverse=True)

        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="BUILD_CONTENT_PLAN",
            output={
                "ranked_topics": ranked, "learnings_considered": len(applicable_learnings),
                "scope_channel_id": scope_channel_id,
            },
            evidence_ids=[l.learning_id for l in applicable_learnings],
            confidence=1.0 if applicable_learnings else 0.3,
        )

    def _prioritize_topics(self, payload: dict) -> AgentTaskResult:
        topics_with_scores = payload.get("topics_with_scores")
        if not topics_with_scores or not isinstance(topics_with_scores, dict):
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="PRIORITIZE_TOPICS",
                reason="topics_with_scores (dict tópico->score numérico) é obrigatório",
            )
        ranked = sorted(topics_with_scores.items(), key=lambda kv: kv[1], reverse=True)
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="PRIORITIZE_TOPICS",
            output={"ranked": [{"topic": t, "score": s} for t, s in ranked]}, confidence=1.0,
        )


def register_argos_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="ARGOS", capability=Capability.PLANNING, level=CompetencyLevel.MASTER,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
