"""
core.agents.runtime.prisma
==============================
BLOCO FINAL 1 (item 1) — PRISMA: agente REAL de inteligência de
dados. Estatística real (stdlib `statistics`) sobre dados numéricos
fornecidos pelo chamador — nunca infere um "padrão" a partir de
amostra insuficiente (mínimo configurável, padrão 3 pontos para
média/desvio, 2 para correlação).
"""

from __future__ import annotations

import statistics

from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability

_MIN_SAMPLE_FOR_STATS = 3
_MIN_SAMPLE_FOR_CORRELATION = 3


class PrismaAgent(RealAgentContract):
    agent_id = "PRISMA"
    primary_task_types = frozenset({"DESCRIBE_SERIES", "DETECT_OUTLIERS", "CORRELATE_SERIES", "RANK_ITEMS"})
    all_rounder_task_types = frozenset({"DESCRIBE_SERIES"})
    max_input_items = 100_000

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type == "DESCRIBE_SERIES":
            return self._describe_series(payload)
        if task_type == "DETECT_OUTLIERS":
            return self._detect_outliers(payload)
        if task_type == "CORRELATE_SERIES":
            return self._correlate_series(payload)
        if task_type == "RANK_ITEMS":
            return self._rank_items(payload)
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _describe_series(self, payload: dict) -> AgentTaskResult:
        values = payload.get("values")
        if not isinstance(values, list) or not all(isinstance(v, (int, float)) for v in values):
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="DESCRIBE_SERIES", reason="'values' precisa ser uma lista de números",
            )
        if len(values) < _MIN_SAMPLE_FOR_STATS:
            return AgentTaskResult.completed(
                agent_id=self.agent_id, task_type="DESCRIBE_SERIES",
                output={"status": "INSUFFICIENT_DATA", "sample_size": len(values),
                        "minimum_required": _MIN_SAMPLE_FOR_STATS},
                confidence=0.0,
            )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="DESCRIBE_SERIES",
            output={
                "sample_size": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
                "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
                "min": min(values), "max": max(values),
            },
            confidence=1.0,
        )

    def _detect_outliers(self, payload: dict) -> AgentTaskResult:
        values = payload.get("values")
        z_threshold = payload.get("z_threshold", 2.0)
        if not isinstance(values, list) or not all(isinstance(v, (int, float)) for v in values):
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="DETECT_OUTLIERS", reason="'values' precisa ser uma lista de números",
            )
        if len(values) < _MIN_SAMPLE_FOR_STATS:
            return AgentTaskResult.completed(
                agent_id=self.agent_id, task_type="DETECT_OUTLIERS",
                output={"status": "INSUFFICIENT_DATA", "sample_size": len(values)}, confidence=0.0,
            )
        mean = statistics.mean(values)
        stdev = statistics.stdev(values)
        if stdev == 0:
            return AgentTaskResult.completed(
                agent_id=self.agent_id, task_type="DETECT_OUTLIERS",
                output={"outliers": [], "reason": "desvio padrão zero — todos os valores idênticos"}, confidence=1.0,
            )
        outliers = [
            {"index": i, "value": v, "z_score": (v - mean) / stdev}
            for i, v in enumerate(values) if abs((v - mean) / stdev) >= z_threshold
        ]
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="DETECT_OUTLIERS",
            output={"outliers": outliers, "mean": mean, "stdev": stdev, "z_threshold": z_threshold},
            confidence=1.0,
        )

    def _correlate_series(self, payload: dict) -> AgentTaskResult:
        series_a = payload.get("series_a")
        series_b = payload.get("series_b")
        if not isinstance(series_a, list) or not isinstance(series_b, list) or len(series_a) != len(series_b):
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="CORRELATE_SERIES",
                reason="'series_a' e 'series_b' precisam ser listas numéricas do MESMO tamanho",
            )
        if len(series_a) < _MIN_SAMPLE_FOR_CORRELATION:
            return AgentTaskResult.completed(
                agent_id=self.agent_id, task_type="CORRELATE_SERIES",
                output={"status": "INSUFFICIENT_DATA", "sample_size": len(series_a)}, confidence=0.0,
            )
        try:
            correlation = statistics.correlation(series_a, series_b)
        except statistics.StatisticsError as exc:
            return AgentTaskResult.completed(
                agent_id=self.agent_id, task_type="CORRELATE_SERIES",
                output={"status": "UNDEFINED", "reason": str(exc)}, confidence=0.0,
            )
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="CORRELATE_SERIES",
            output={"correlation": correlation, "sample_size": len(series_a)}, confidence=1.0,
        )

    def _rank_items(self, payload: dict) -> AgentTaskResult:
        items = payload.get("items")
        key = payload.get("key")
        if not isinstance(items, list) or not key:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="RANK_ITEMS", reason="'items' (lista de dicts) e 'key' são obrigatórios",
            )
        try:
            ranked = sorted(items, key=lambda it: it.get(key, float("-inf")), reverse=True)
        except Exception as exc:
            return AgentTaskResult.rejected(agent_id=self.agent_id, task_type="RANK_ITEMS", reason=str(exc))
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="RANK_ITEMS", output={"ranked": ranked, "key": key}, confidence=1.0,
        )


def register_prisma_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="PRISMA", capability=Capability.DATA_ANALYSIS, level=CompetencyLevel.ESPECIALISTA,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
