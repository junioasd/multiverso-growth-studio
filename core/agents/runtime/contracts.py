"""
core.agents.runtime.contracts
=================================
BLOCO FINAL 1 (item 1 — 25 AGENTES: runtime real): contrato comum
usado por TODOS os agentes que saem do estado SCAFFOLD. Nenhum agente
"declara sucesso sem evidência real" — todo `AgentTaskResult` carrega
`status` explícito (`COMPLETED`/`UNAVAILABLE`/`FAILED`), as
`evidence_ids`/`source` que sustentam a resposta, e `confidence`
honesta (nunca inventada quando o resultado vem de um cálculo
determinístico sobre dado real — nesse caso, `confidence=1.0` reflete
que não há incerteza estatística, apenas o dado de entrada).

Diferença central em relação a `ScaffoldAgent`: SCAFFOLD nunca executa
(`AgentNotImplemented` sempre). Um agente REAL executa de verdade —
mas quando depende de um provider externo não configurado (rede,
API paga, modelo generativo), retorna `status=UNAVAILABLE` de forma
explícita, nunca fabrica um resultado.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class AgentTaskStatus(str, Enum):
    COMPLETED = "COMPLETED"
    UNAVAILABLE = "UNAVAILABLE"  # dependência externa (provider) não configurada
    FAILED = "FAILED"            # erro genuíno durante a execução real
    REJECTED = "REJECTED"        # a própria tarefa viola uma regra/limite do agente


@dataclass
class AgentTaskResult:
    result_id: str
    agent_id: str
    task_type: str
    status: AgentTaskStatus
    output: dict = field(default_factory=dict)
    evidence_ids: list = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    executed_at: float = field(default_factory=time.time)
    duration_ms: float | None = None

    @staticmethod
    def completed(*, agent_id: str, task_type: str, output: dict, evidence_ids: list | None = None,
                  confidence: float = 1.0, duration_ms: float | None = None) -> "AgentTaskResult":
        return AgentTaskResult(
            result_id=f"res_{uuid.uuid4().hex[:12]}", agent_id=agent_id, task_type=task_type,
            status=AgentTaskStatus.COMPLETED, output=output, evidence_ids=list(evidence_ids or []),
            confidence=confidence, duration_ms=duration_ms,
        )

    @staticmethod
    def unavailable(*, agent_id: str, task_type: str, reason: str) -> "AgentTaskResult":
        """CORREÇÃO (zero mocks apresentados como produção): usado
        sempre que a tarefa dependeria de um provider externo (rede,
        API de terceiros, modelo generativo) que não está configurado
        neste ambiente/instalação — nunca finge sucesso."""
        return AgentTaskResult(
            result_id=f"res_{uuid.uuid4().hex[:12]}", agent_id=agent_id, task_type=task_type,
            status=AgentTaskStatus.UNAVAILABLE, confidence=0.0, reason=reason,
        )

    @staticmethod
    def failed(*, agent_id: str, task_type: str, reason: str) -> "AgentTaskResult":
        return AgentTaskResult(
            result_id=f"res_{uuid.uuid4().hex[:12]}", agent_id=agent_id, task_type=task_type,
            status=AgentTaskStatus.FAILED, confidence=0.0, reason=reason,
        )

    @staticmethod
    def rejected(*, agent_id: str, task_type: str, reason: str) -> "AgentTaskResult":
        return AgentTaskResult(
            result_id=f"res_{uuid.uuid4().hex[:12]}", agent_id=agent_id, task_type=task_type,
            status=AgentTaskStatus.REJECTED, confidence=0.0, reason=reason,
        )


class AgentLimitExceeded(Exception):
    """Levantado quando uma tarefa excede um limite explícito do
    agente (ex: tamanho de entrada, escopo fora do permitido) — nunca
    processado parcialmente sem aviso."""


class RealAgentContract:
    """Base para agentes REAIS (não-SCAFFOLD). Cada subclasse declara:

    - `primary_task_types`/`all_rounder_task_types` (mesma disciplina
      de `ActiveAgentContract` já usada por ATLAS/NAVIGATOR/FORGE);
    - `max_input_items` — limite explícito de escopo por chamada
      (nunca processa uma quantidade ilimitada silenciosamente);
    - `execute_real(task_type, payload)` — a lógica REAL, implementada
      pela subclasse.

    `execute()` (o método público) SEMPRE passa por: checagem de tipo
    de tarefa suportado, checagem de limite, log de auditoria (quando
    `audit` é fornecido), e captura de exceções não tratadas como
    `FAILED` (nunca deixa uma exceção crua escapar como "sucesso
    silencioso" em nenhum caminho)."""

    agent_id: str = "UNSET"
    primary_task_types: set = frozenset()
    all_rounder_task_types: set = frozenset()
    max_input_items: int = 500

    def __init__(self, *, audit=None) -> None:
        self.audit = audit
        self.executions: int = 0
        self.failures: int = 0

    def can_handle_as_primary(self, *, task_type: str) -> bool:
        return task_type in self.primary_task_types

    def can_handle(self, *, task_type: str) -> bool:
        return task_type in self.primary_task_types or task_type in self.all_rounder_task_types

    def execute(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        start = time.time()
        if not self.can_handle(task_type=task_type):
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type=task_type,
                reason=f"'{self.agent_id}' não trata task_type '{task_type}'",
            )
        try:
            self._check_limits(payload)
            result = self.execute_real(task_type=task_type, payload=payload)
        except AgentLimitExceeded as exc:
            result = AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason=str(exc))
        except Exception as exc:  # nunca deixa a exceção crua escapar como sucesso
            self.failures += 1
            result = AgentTaskResult.failed(agent_id=self.agent_id, task_type=task_type, reason=str(exc))
        result.duration_ms = (time.time() - start) * 1000
        self.executions += 1
        if self.audit is not None:
            try:
                self.audit.record(
                    actor=self.agent_id, action=f"EXECUTE_{task_type}", resource=self.agent_id,
                    outcome=result.status.value, details={"result_id": result.result_id, "reason": result.reason},
                )
            except Exception:
                pass  # auditoria nunca mascara o resultado real da execução
        return result

    def _check_limits(self, payload: dict) -> None:
        for value in payload.values():
            if isinstance(value, (list, tuple, set)) and len(value) > self.max_input_items:
                raise AgentLimitExceeded(
                    f"entrada excede o limite de {self.max_input_items} itens por chamada "
                    f"(recebido: {len(value)}) — divida em lotes menores"
                )

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        raise NotImplementedError("subclasses de RealAgentContract implementam execute_real()")
