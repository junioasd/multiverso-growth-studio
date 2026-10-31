"""
atlas.agentic
===============
COMANDO 4/6 — ATLAS AGENTIC-FIRST e ALL-ROUNDER.

Faltava o LAÇO de orquestração: a seleção de agente por competência já
existia (`AtlasDirector.resolve_agent_for_task`, contratos
`RealAgentContract`), mas nenhum código do ATLAS executava o agente
escolhido, validava o retorno, rejeitava resultado sem evidência,
redelegava, limitava tentativas/tempo ou impedia laços. Este módulo só
COMPÕE o registro e os contratos existentes — nenhum agente novo.

objetivo → decomposição → delegação (por competência) → execução isolada
→ verificação → (rejeição + redelegação) → integração → decisão.

Regras:
- ordem de delegação: especialista (domínio primário) → ALL-ROUNDER;
- cada tentativa recebe CÓPIA isolada do payload (tarefas não se contaminam);
- aceito só COMPLETED com evidência (do agente OU das entradas declaradas),
  confiança em (min, 1], chaves de saída exigidas presentes;
- saída que tenta exercer autoridade (aprovar, executar ação crítica,
  decidir pelo Owner) é REJEITADA — agente não ultrapassa o Owner;
- mesmo agente nunca é repetido na mesma coordenação; sub-delegação
  circular ou profunda demais é bloqueada (`LOOP_BLOCKED`);
- orçamento explícito de tentativas e de tempo (`BUDGET_EXCEEDED`);
- a conclusão de um agente é INFERÊNCIA (nunca FATO): o fato está na
  evidência referenciada;
- toda tentativa é auditada.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field

from core.agents.runtime.contracts import AgentTaskStatus
from core.evidence.claims import ClaimType
from core.governance.idempotency import stable_fingerprint

_AUTHORITY_KEYS = ("approve", "approval", "owner_decision", "execute_critical", "grant_permission",
                   "resolved_by", "override_owner", "spend_authorization")


@dataclass
class AttemptRecord:
    agent_id: str
    tier: str
    status: str
    accepted: bool
    reason: str
    result_id: str | None = None
    duration_ms: float | None = None


@dataclass
class CoordinationResult:
    coordination_id: str
    objective: str
    task_type: str
    status: str                      # ACCEPTED | ESCALATED | BUDGET_EXCEEDED | LOOP_BLOCKED
    agent_id: str | None = None
    output: dict = field(default_factory=dict)
    claim_type: ClaimType = ClaimType.UNKNOWN
    evidence_refs: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    reason: str = ""
    sub_results: list = field(default_factory=list)


class AgenticCoordinator:
    def __init__(self, *, registry, audit=None, actor: str = "ATLAS", max_attempts: int = 3,
                 time_budget_seconds: float = 30.0, max_depth: int = 2, min_confidence: float = 0.3) -> None:
        self.registry, self.audit, self.actor = registry, audit, actor
        self.max_attempts, self.time_budget_seconds = max_attempts, time_budget_seconds
        self.max_depth, self.min_confidence = max_depth, min_confidence

    # ------------------------------------------------------------ seleção
    def candidates(self, task_type: str, *, preferred_agent: str | None = None) -> list:
        """[(agent_id, tier)] na ordem de competência: especialista
        preferido → demais especialistas → ALL-ROUNDERS."""
        regs = list(self.registry.all())
        primary = [r.agent_id for r in regs
                   if task_type in (getattr(r.contract, "primary_task_types", None) or ())]
        allr = [r.agent_id for r in regs if r.agent_id not in primary
                and task_type in (getattr(r.contract, "all_rounder_task_types", None) or ())]
        if preferred_agent in primary:
            primary.remove(preferred_agent)
            primary.insert(0, preferred_agent)
        return [(a, "SPECIALIST") for a in primary] + [(a, "ALL_ROUNDER") for a in allr]

    # ---------------------------------------------------------- validação
    def _validate(self, result, *, evidence_refs, required_output_keys) -> tuple:
        if result.status != AgentTaskStatus.COMPLETED:
            return False, f"{result.status.value}: {result.reason or 'sem motivo declarado'}"
        output = result.output or {}
        leaked = sorted(k for k in _flatten_keys(output) if k.lower() in _AUTHORITY_KEYS)
        if leaked:
            return False, f"saída tenta exercer autoridade reservada ao Owner/NAVIGATOR ({', '.join(leaked)})"
        inner = str(output.get("status", "")).upper()
        if inner in ("INSUFFICIENT_DATA", "UNKNOWN", "UNAVAILABLE", "FAILED"):
            return False, f"agente declarou '{inner}' dentro de um COMPLETED — não é conclusão utilizável"
        if not (list(result.evidence_ids) or list(evidence_refs)):
            return False, "resultado SEM evidência (nem do agente, nem das entradas declaradas)"
        if not (self.min_confidence < float(result.confidence) <= 1.0):
            return False, f"confiança {result.confidence} fora de ({self.min_confidence}, 1]"
        missing = [k for k in required_output_keys if k not in output or output[k] in (None, "")]
        if missing:
            return False, f"saída incompleta: faltam {missing}"
        return True, "aceito"

    # ---------------------------------------------------------- delegação
    def delegate(self, *, objective: str, task_type: str, payload: dict, evidence_refs: list | tuple = (),
                 preferred_agent: str | None = None, required_output_keys: list | tuple = (),
                 _chain: tuple = (), _started: float | None = None) -> CoordinationResult:
        started = time.monotonic() if _started is None else _started
        cid = f"coord_{uuid.uuid4().hex[:12]}"
        res = CoordinationResult(coordination_id=cid, objective=objective, task_type=task_type, status="ESCALATED")
        key = (task_type, stable_fingerprint("agentic", task_type, payload))
        if key in _chain:
            res.status, res.reason = "LOOP_BLOCKED", "sub-delegação circular detectada (mesma tarefa já na cadeia)"
            return self._audit(res)
        if len(_chain) >= self.max_depth:
            res.status, res.reason = "LOOP_BLOCKED", f"profundidade máxima de delegação ({self.max_depth}) atingida"
            return self._audit(res)
        candidates = self.candidates(task_type, preferred_agent=preferred_agent)
        if not candidates:
            res.reason = f"nenhum agente competente para '{task_type}' — escalar ao Owner"
            return self._audit(res)
        for agent_id, tier in candidates:
            if len(res.attempts) >= self.max_attempts:
                res.status, res.reason = "BUDGET_EXCEEDED", f"orçamento de {self.max_attempts} tentativas esgotado"
                return self._audit(res)
            if time.monotonic() - started > self.time_budget_seconds:
                res.status, res.reason = "BUDGET_EXCEEDED", f"orçamento de tempo ({self.time_budget_seconds}s) esgotado"
                return self._audit(res)
            contract = self.registry.get(agent_id).contract
            result = contract.execute(task_type=task_type, payload=copy.deepcopy(payload))   # isolamento
            ok, why = self._validate(result, evidence_refs=evidence_refs, required_output_keys=required_output_keys)
            res.attempts.append(AttemptRecord(agent_id=agent_id, tier=tier, status=result.status.value, accepted=ok,
                                              reason=why, result_id=result.result_id, duration_ms=result.duration_ms))
            self._audit_attempt(cid, agent_id, tier, ok, why)
            if not ok:
                continue                                   # redelega ao próximo competente
            output = dict(result.output or {})
            sub = output.pop("delegate_request", None)
            if isinstance(sub, dict):
                child = self.delegate(objective=f"{objective} > {sub.get('task_type')}",
                                      task_type=sub.get("task_type", ""), payload=sub.get("payload") or {},
                                      evidence_refs=evidence_refs, _chain=_chain + (key,), _started=started)
                res.sub_results.append(child)
                if child.status != "ACCEPTED":
                    res.attempts[-1].accepted = False
                    res.attempts[-1].reason = f"sub-delegação falhou: {child.status} — {child.reason}"
                    continue
            res.status, res.agent_id, res.output = "ACCEPTED", agent_id, output
            res.claim_type = ClaimType.INFERENCE     # conclusão de agente nunca é FATO
            res.evidence_refs = list(result.evidence_ids) + list(evidence_refs)
            res.reason = f"aceito de {agent_id} ({tier}) após {len(res.attempts)} tentativa(s)"
            return self._audit(res)
        res.reason = ("nenhum agente produziu resultado aceitável — " +
                      "; ".join(f"{a.agent_id}: {a.reason}" for a in res.attempts))
        return self._audit(res)

    # ---------------------------------------------------------- ALL-ROUNDER
    def integrate(self, *, objective: str, perspectives: dict) -> dict:
        """Combina perspectivas (mercado, público, finanças, risco...) SEM
        superficialidade: cada uma é delegada ao especialista competente,
        isolada; o que faltar fica explícito — nada é preenchido."""
        results = {name: self.delegate(objective=f"{objective} [{name}]", **spec)
                   for name, spec in perspectives.items()}
        accepted = {n: r for n, r in results.items() if r.status == "ACCEPTED"}
        return {
            "objective": objective, "claim_type": ClaimType.INFERENCE.value,
            "perspectives": {n: {"agent": r.agent_id, "output": r.output, "evidence": r.evidence_refs}
                             for n, r in accepted.items()},
            "missing_perspectives": {n: f"{r.status}: {r.reason}" for n, r in results.items() if n not in accepted},
            "complete": len(accepted) == len(results),
            "coordination_ids": {n: r.coordination_id for n, r in results.items()},
        }

    # ------------------------------------------------------------ auditoria
    def _audit_attempt(self, cid, agent_id, tier, ok, why):
        if self.audit is not None:
            self.audit.record(actor=self.actor, action="atlas_agentic_attempt", resource=cid,
                              outcome="ACCEPTED" if ok else "REJECTED",
                              details={"agent": agent_id, "tier": tier, "reason": why})

    def _audit(self, res: CoordinationResult) -> CoordinationResult:
        if self.audit is not None:
            self.audit.record(actor=self.actor, action="atlas_agentic_coordination", resource=res.coordination_id,
                              outcome=res.status, details={"task_type": res.task_type, "agent": res.agent_id,
                                                           "attempts": len(res.attempts), "reason": res.reason})
        return res


def _flatten_keys(obj) -> list:
    keys = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(str(k))
            keys.extend(_flatten_keys(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            keys.extend(_flatten_keys(v))
    return keys
