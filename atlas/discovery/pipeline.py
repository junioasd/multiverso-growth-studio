"""
atlas.discovery.pipeline
============================
ATLAS PIPELINE 1 — INTELIGÊNCIA E DESCOBERTA — orquestrador que
conecta o motor de descoberta (`atlas.discovery.engine`) ao motor de
execução JÁ EXISTENTE e já validado (`atlas.orchestration.graph_executor.
GraphExecutor`, da ATLAS EXPANSION 1) — nenhum scheduler/motor de
execução paralelo foi criado aqui.

Cada fonte registrada vira UMA `PlanTask` independente (grupo
paralelo real, via `AdvancedPlan.parallel_groups()` — já existente);
uma tarefa final de ranking DEPENDE de todas as tarefas de descoberta
terem terminado. Toda a segurança de execução — exclusão por lease,
retry, TTL, heartbeat, recuperação de crash real, Quality Gate, Owner
Approval — é herdada do `GraphExecutor` sem NENHUMA lógica nova aqui.

As oportunidades descobertas NUNCA viajam pelo lease do
`GraphExecutor` (que persiste apenas o `outcome` — um resumo,
JSON-serializável, pequeno) — elas vivem exclusivamente no
`OpportunityStore` persistente, atualizado como efeito colateral
direto de cada `task_runner`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from atlas.advanced_planning import AdvancedPlanner, PlanTask
from atlas.discovery.engine import ContentDiscoveryEngine
from atlas.discovery.opportunity_store import OpportunityStore
from atlas.discovery.sources import DiscoverySource, build_default_sources
from atlas.escalation import EscalationDesk
from atlas.orchestration.graph_executor import GraphExecutor
from core.coordination.mission import MissionTracker
from core.jobs.queue import JobQueue

__all__ = ["DiscoveryPipeline", "DiscoveryPipelineReport"]

_DISCOVER_TASK_PREFIX = "discover:"
_RANK_TASK_ID = "rank_opportunities"


@dataclass
class DiscoveryPipelineReport:
    mission_id: str
    scanned_sources: list[str] = field(default_factory=list)
    unavailable_sources: list[str] = field(default_factory=list)
    discovered_count: int = 0
    updated_count: int = 0
    ranked_count: int = 0
    ready_for_pipeline2_count: int = 0
    low_evidence_count: int = 0
    stale_count: int = 0
    task_statuses: dict = field(default_factory=dict)  # task_id -> status externo do GraphExecutor
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
    # independente): `exec_technical_success` (a execução TÉCNICA do
    # plano terminou sem falha — vem de `PlanExecutionReport.success`,
    # já existente no `GraphExecutor`) é um conceito DISTINTO de
    # `data_status` (o resultado OPERACIONAL da descoberta em si —
    # houve dado genuíno e utilizável, ou não). Uma execução pode ser
    # tecnicamente bem-sucedida (nenhuma tarefa falhou) e ainda assim
    # não ter encontrado NENHUM dado real — isso NUNCA é "success"
    # operacional, mesmo que seja "success" técnico.
    exec_technical_success: bool = False
    data_status: str = "NO_DATA"  # DATA_FOUND | PARTIAL | NO_DATA | ALL_SOURCES_UNAVAILABLE | DEGRADED

    @property
    def prioritized_queue(self) -> list[dict]:
        """Atalho — delega ao `OpportunityStore` usado nesta
        execução. Preenchido por `DiscoveryPipeline.run()` após a
        conclusão."""
        return self._queue

    _queue: list = field(default_factory=list, repr=False)


class DiscoveryPipeline:
    """Orquestra um ciclo completo de descoberta + ranking, usando o
    `GraphExecutor` como harness de execução. Uma instância é leve —
    pode ser recriada a cada chamada (mesmo padrão de
    `GraphExecutor`/`MissionTracker`/`JobQueue`)."""

    def __init__(
        self, *, mission_tracker: MissionTracker, job_queue: JobQueue,
        escalation_desk: EscalationDesk | None = None, db_path: str | None = None,
        sources: list[DiscoverySource] | None = None, gate_in_progress_ttl_seconds: float = 60.0,
        max_workers: int = 4, audit=None, broker=None, allow_test_sources: bool = False,
    ) -> None:
        self.db_path = db_path
        self.audit = audit
        self.store = OpportunityStore(db_path=db_path)
        self.sources: list[DiscoverySource] = sources if sources is not None else build_default_sources()
        # `broker` — repassado explicitamente ao engine; sem um, cada
        # fonte recebe um `NotImplementedTransport` padrão (sempre
        # levanta, honestamente). `allow_test_sources=False` por
        # padrão (modo operacional seguro) — repassado explicitamente,
        # nunca ligado por engano; ver
        # `atlas.discovery.provenance`/`engine`.
        self.engine = ContentDiscoveryEngine(
            sources=self.sources, store=self.store, broker=broker, allow_test_sources=allow_test_sources,
        )
        self.executor = GraphExecutor(
            mission_tracker=mission_tracker, job_queue=job_queue, escalation_desk=escalation_desk,
            db_path=db_path, max_workers=max_workers, gate_in_progress_ttl_seconds=gate_in_progress_ttl_seconds,
        )

    def build_plan(self):
        planner = AdvancedPlanner()
        tasks = []
        discover_task_ids = []
        for source in self.sources:
            task_id = f"{_DISCOVER_TASK_PREFIX}{source.source_name}"
            discover_task_ids.append(task_id)
            tasks.append({
                "task_id": task_id, "description": f"Descobrir oportunidades de conteúdo em {source.source_name}",
                "max_retries": 2,
            })
        tasks.append({
            "task_id": _RANK_TASK_ID, "description": "Ranquear oportunidades descobertas com Quality Gate de evidência",
            "depends_on": list(discover_task_ids), "max_retries": 1,
            "quality_gate": "content_discovery_ranking_structural_gate",
        })
        return planner.build_plan(objective="ATLAS PIPELINE 1 — descoberta e ranking de oportunidades de conteúdo", tasks=tasks)

    def _task_runner(self, task: PlanTask, *, niche: str | None) -> dict:
        if task.task_id == _RANK_TASK_ID:
            return self._run_ranking()
        source_name = task.task_id[len(_DISCOVER_TASK_PREFIX):]
        source = next((s for s in self.sources if s.source_name == source_name), None)
        if source is None:
            # nunca deveria acontecer (o plano é construído a partir
            # de `self.sources`) — fail-closed explícito em vez de
            # um KeyError opaco.
            raise RuntimeError(f"fonte '{source_name}' não encontrada no registro desta instância")
        report = self.engine.scan_source(source, niche=niche)
        return report.to_dict()

    def _run_ranking(self) -> dict:
        ranked = self.engine.rank_all()
        unranked_count = sum(1 for opp in self.store.all() if self.store.ranking_for(opp.identity_key) is None)
        return {
            "ranked_count": len(ranked),
            "ready_for_pipeline2_count": sum(1 for r in ranked if r.status == "READY_FOR_PIPELINE2"),
            "low_evidence_count": sum(1 for r in ranked if r.status == "LOW_EVIDENCE"),
            "stale_count": sum(1 for r in ranked if r.status == "STALE"),
            "unranked_count": unranked_count,
        }

    @staticmethod
    def _ranking_quality_gate(task: PlanTask, outcome: dict) -> bool:
        """Gate ESTRUTURAL (não de julgamento de negócio): a tarefa de
        ranking só é aprovada se TODA oportunidade na fila recebeu
        algum ranking (mesmo que `LOW_EVIDENCE`) — nunca aprova uma
        execução que deixou itens sem processar. Evidência
        insuficiente NAS oportunidades em si nunca reprova esta
        tarefa (isso é decidido item a item pelo
        `ContentOpportunityEvidenceGate`, já aplicado dentro de
        `rank_all()` — nunca aqui de novo)."""
        return outcome.get("unranked_count", 1) == 0

    def _compute_data_status(self, report: DiscoveryPipelineReport) -> str:
        """MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
        independente): distingue sucesso TÉCNICO da execução (o plano
        terminou sem falha) de sucesso OPERACIONAL da descoberta
        (encontrou dado genuíno e utilizável). Zero fontes registradas,
        todas as fontes indisponíveis, apenas fontes MOCK/sem
        credencial (nenhuma descoberta real), ou nenhuma evidência que
        chegou a `READY_FOR_PIPELINE2` — NUNCA são reportados como
        sucesso simples; cada caso recebe um rótulo operacional
        HONESTO e distinto, nunca "success" genérico mascarando
        ausência de dado."""
        total_sources = len(self.sources)
        if total_sources == 0:
            return "NO_DATA"
        if len(report.unavailable_sources) >= total_sources:
            return "ALL_SOURCES_UNAVAILABLE"
        if report.discovered_count == 0 and report.updated_count == 0:
            return "NO_DATA"
        if report.ready_for_pipeline2_count == 0:
            return "DEGRADED"
        if report.unavailable_sources:
            return "PARTIAL"
        return "DATA_FOUND"

    def run(
        self, *, niche: str | None = None, mission_id: str | None = None, actor: str = "atlas_pipeline1",
        timeout_per_task: float = 30.0, requires_owner_approval: Callable[[PlanTask], bool] | None = None,
        approved_escalation_ids: dict | None = None,
    ) -> DiscoveryPipelineReport:
        """Executa um ciclo completo. `mission_id` estável entre
        chamadas permite RETOMADA real (mesmo mecanismo já validado do
        `GraphExecutor` — tarefas já `VERIFIED` são puladas, nunca
        reexecutadas; tarefas travadas em estado intermediário por um
        crash real são recuperadas via TTL+heartbeat+reclaim, sem
        NENHUMA lógica nova aqui)."""
        plan = self.build_plan()
        resolved_mission_id = mission_id or f"discovery_{plan.plan_id}"

        if self.audit is not None:
            self.audit.record(
                actor=actor, action="discovery_pipeline_started", resource=resolved_mission_id,
                outcome="started", details={"niche": niche, "sources": [s.source_name for s in self.sources]},
            )

        exec_report = self.executor.execute_plan(
            plan, task_runner=lambda task: self._task_runner(task, niche=niche), actor=actor,
            mission_id=resolved_mission_id, timeout_per_task=timeout_per_task,
            requires_owner_approval=requires_owner_approval, approved_escalation_ids=approved_escalation_ids,
            quality_gate_checker=self._ranking_quality_gate,
        )

        report = DiscoveryPipelineReport(mission_id=resolved_mission_id)
        for result in exec_report.task_results:
            task_id = result.task_id
            report.task_statuses[task_id] = result.status
            if task_id.startswith(_DISCOVER_TASK_PREFIX) and isinstance(result.result, dict):
                report.scanned_sources.extend(result.result.get("scanned_sources", []))
                report.unavailable_sources.extend(result.result.get("unavailable_sources", []))
                report.discovered_count += result.result.get("discovered_count", 0)
                report.updated_count += result.result.get("updated_count", 0)
            elif task_id == _RANK_TASK_ID and isinstance(result.result, dict):
                report.ranked_count = result.result.get("ranked_count", 0)
                report.ready_for_pipeline2_count = result.result.get("ready_for_pipeline2_count", 0)
                report.low_evidence_count = result.result.get("low_evidence_count", 0)
                report.stale_count = result.result.get("stale_count", 0)

        report.finished_at = time.time()
        report.exec_technical_success = exec_report.success
        report.data_status = self._compute_data_status(report)
        report._queue = self.store.prioritized_queue()

        if self.audit is not None:
            self.audit.record(
                actor=actor, action="discovery_pipeline_finished", resource=resolved_mission_id,
                # MICROCORREÇÃO CRÍTICA (falha real encontrada por
                # auditoria independente): o `outcome` da auditoria
                # NUNCA é "success" genérico — é sempre o status
                # OPERACIONAL real (`data_status`), que reflete
                # honestamente se houve dado genuíno e utilizável, não
                # apenas se a execução técnica terminou sem erro.
                outcome=report.data_status,
                details={
                    "exec_technical_success": report.exec_technical_success,
                    "data_status": report.data_status,
                    "discovered_count": report.discovered_count, "updated_count": report.updated_count,
                    "unavailable_sources": report.unavailable_sources,
                    "ready_for_pipeline2_count": report.ready_for_pipeline2_count,
                    "low_evidence_count": report.low_evidence_count,
                },
            )

        return report
