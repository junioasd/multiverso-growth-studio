"""
core.agents.runtime.echo
============================
BLOCO FINAL 1 (item 1) — ECHO: agente REAL de aprendizado contínuo.
Wrapper fino sobre `core.learning.loop.LearningLoop` — nunca
reimplementa a lógica de validação (a mesma barra estatística e
isolamento de escopo já testados em `tests/test_learning_loop.py`
se aplicam aqui integralmente).
"""

from __future__ import annotations

from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability
from core.learning.loop import LearningLoop, Prediction, ValidationStatus


class EchoAgent(RealAgentContract):
    agent_id = "ECHO"
    primary_task_types = frozenset({
        "RECORD_PREDICTION", "RECORD_OUTCOME", "EVALUATE_LEARNING", "PROMOTE_LEARNING", "GET_VISIBLE_LEARNINGS",
    })
    all_rounder_task_types = frozenset({"EVALUATE_LEARNING"})
    max_input_items = 1_000

    def __init__(self, *, learning_loop: LearningLoop | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.learning_loop = learning_loop if learning_loop is not None else LearningLoop()

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type == "RECORD_PREDICTION":
            return self._record_prediction(payload)
        if task_type == "RECORD_OUTCOME":
            return self._record_outcome(payload)
        if task_type == "EVALUATE_LEARNING":
            return self._evaluate_learning(payload)
        if task_type == "PROMOTE_LEARNING":
            return self._promote_learning(payload)
        if task_type == "GET_VISIBLE_LEARNINGS":
            return self._get_visible_learnings(payload)
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _record_prediction(self, payload: dict) -> AgentTaskResult:
        required = ("predicting_agent_id", "metric_name", "predicted_value", "context")
        missing = [k for k in required if k not in payload]
        if missing:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="RECORD_PREDICTION", reason=f"campos obrigatórios ausentes: {missing}",
            )
        prediction = self.learning_loop.record_prediction(Prediction.new(
            agent_id=payload["predicting_agent_id"], metric_name=payload["metric_name"],
            predicted_value=payload["predicted_value"], context=payload["context"],
            scope_channel_id=payload.get("scope_channel_id"), scope_project_id=payload.get("scope_project_id"),
            evidence_ids=payload.get("evidence_ids"),
        ))
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="RECORD_PREDICTION",
            output={"prediction_id": prediction.prediction_id}, evidence_ids=[prediction.prediction_id],
            confidence=1.0,
        )

    def _record_outcome(self, payload: dict) -> AgentTaskResult:
        if "prediction_id" not in payload or "actual_value" not in payload:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="RECORD_OUTCOME",
                reason="prediction_id e actual_value são obrigatórios",
            )
        try:
            record = self.learning_loop.record_outcome(
                prediction_id=payload["prediction_id"], actual_value=payload["actual_value"],
            )
        except KeyError as exc:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type="RECORD_OUTCOME", reason=str(exc))
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="RECORD_OUTCOME",
            output={
                "record_id": record.record_id, "error": record.error, "direction_correct": record.direction_correct,
            },
            evidence_ids=[record.record_id, record.prediction_id], confidence=1.0,
        )

    def _evaluate_learning(self, payload: dict) -> AgentTaskResult:
        if "metric_name" not in payload:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="EVALUATE_LEARNING", reason="metric_name é obrigatório",
            )
        status = self.learning_loop.evaluate_learning(
            metric_name=payload["metric_name"], scope_channel_id=payload.get("scope_channel_id"),
            scope_project_id=payload.get("scope_project_id"),
        )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="EVALUATE_LEARNING", output={"status": status.value},
            confidence=1.0 if status == ValidationStatus.VALIDATED else 0.5,
        )

    def _promote_learning(self, payload: dict) -> AgentTaskResult:
        required = ("metric_name", "statement", "validated_by")
        missing = [k for k in required if k not in payload]
        if missing:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="PROMOTE_LEARNING", reason=f"campos obrigatórios ausentes: {missing}",
            )
        try:
            learning = self.learning_loop.promote_validated_learning(
                metric_name=payload["metric_name"], statement=payload["statement"],
                validated_by=payload["validated_by"], scope_channel_id=payload.get("scope_channel_id"),
                scope_project_id=payload.get("scope_project_id"), applies_globally=payload.get("applies_globally", False),
            )
        except ValueError as exc:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type="PROMOTE_LEARNING", reason=str(exc))
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="PROMOTE_LEARNING",
            output={
                "learning_id": learning.learning_id, "sample_size": learning.sample_size,
                "direction_accuracy": learning.direction_accuracy,
            },
            evidence_ids=[learning.learning_id], confidence=learning.direction_accuracy,
        )

    def _get_visible_learnings(self, payload: dict) -> AgentTaskResult:
        learnings = self.learning_loop.learnings_visible_to(
            scope_channel_id=payload.get("scope_channel_id"), scope_project_id=payload.get("scope_project_id"),
        )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="GET_VISIBLE_LEARNINGS",
            output={"learnings": [
                {"learning_id": l.learning_id, "metric_name": l.metric_name, "statement": l.statement,
                 "sample_size": l.sample_size, "applies_globally": l.applies_globally}
                for l in learnings
            ]},
            evidence_ids=[l.learning_id for l in learnings], confidence=1.0,
        )


def register_echo_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="ECHO", capability=Capability.DATA_ANALYSIS, level=CompetencyLevel.MASTER,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
    capability_registry.register(
        agent="ECHO", capability=Capability.REASONING, level=CompetencyLevel.MASTER,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
