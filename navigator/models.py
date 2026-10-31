"""
navigator.models
===================
NAVIGATOR — tipos de ação operacional, resultado de execução, e as
identidades usadas para idempotência e rastreabilidade.

CORREÇÃO (microcorreção final — item 1: REPLAN + REEXECUTE com
NAVIGATOR real): separa claramente 6 conceitos antes colapsados em um
só:

- `mission_id` / `task_id` — já existiam: identificam a TAREFA dentro
  de uma missão do ATLAS.
- `execution_id` — agrupa TODAS as tentativas de uma mesma rodada de
  execução de uma tarefa (gerado uma vez por
  `ReasoningEngine._run_task_with_replan`).
- `attempt_id` — identifica UMA tentativa específica dentro de um
  `execution_id` (ex: `exec_abc123_attempt_2`). Tentativas DIFERENTES
  (attempt_id diferente) são execuções GENUINAMENTE distintas — mesma
  tentativa repetida (mesmo attempt_id) é idempotente.
- `idempotency_key` — a chave efetivamente usada para deduplicar: para
  ações NÃO críticas, inclui o `attempt_id` (permite retry legítimo
  com novo attempt_id); para ações CRÍTICAS, é sempre o
  `critical_action_key` (nunca inclui attempt_id — protege contra
  duplicar gasto/publicação/ação irreversível não importa quantas
  "tentativas" levaram até a aprovação).
- `critical_action_key` — identidade ESTÁVEL da ação crítica em si
  (mission_id, task_id, action, platform, conta, canal, payload —
  SEM attempt_id). Usada para a solicitação de Owner Approval e para
  garantir que a ação crítica execute NO MÁXIMO uma vez, mesmo com
  múltiplas tentativas/replans.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.governance.idempotency import stable_fingerprint


class NavigatorAction(str, Enum):
    ACCESS_PLATFORM = "access_platform"
    LOCATE_AREA = "locate_area"
    FILL_FORM = "fill_form"
    CREATE_DRAFT = "create_draft"
    PUBLISH = "publish"
    REPLY = "reply"
    UPDATE_CONTENT = "update_content"
    COLLECT_METRICS = "collect_metrics"
    CHECK_STATUS = "check_status"
    SEND_EVIDENCE = "send_evidence"


# Ações que alteram estado PÚBLICO/externo de forma visível — sempre
# passam pelo fluxo de Owner Approval (nunca executadas sem aprovação
# ATIVA — ver `navigator.executor`).
CRITICAL_NAVIGATOR_ACTIONS = frozenset({
    NavigatorAction.PUBLISH, NavigatorAction.REPLY, NavigatorAction.UPDATE_CONTENT,
})

# CORREÇÃO (item 1): ações classificadas como retry-safe por padrão —
# leituras/checagens de status, sem efeito colateral observável ao
# repetir. Ações NÃO listadas aqui (e não críticas) só podem ser
# retentadas com um NOVO attempt_id quando o chamador declarar
# explicitamente `retry_safe=True` na tarefa (representando proteção
# por idempotência externa/receipt verificável) — nunca por padrão.
RETRY_SAFE_ACTIONS = frozenset({
    NavigatorAction.ACCESS_PLATFORM, NavigatorAction.LOCATE_AREA, NavigatorAction.COLLECT_METRICS,
    NavigatorAction.CHECK_STATUS, NavigatorAction.SEND_EVIDENCE,
})


class ExecutionResult(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ESCALATED = "ESCALATED"
    # CORREÇÃO (item 1, teste adversarial 3): resultado INCERTO de uma
    # ação crítica (ex: exceção levantada DURANTE a execução real, já
    # consumindo a aprovação — não se sabe se o efeito externo
    # ocorreu). NUNCA repetido automaticamente — a mesma
    # `critical_action_key` sempre devolve este mesmo registro até
    # confirmação/evidência externa resolver a incerteza.
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"


def compute_execution_identity(
    *, mission_id: str | None, task_id: str | None, action: NavigatorAction, platform: str,
    account_context: str | None, channel_id: str | None, payload: dict,
) -> str:
    """Identidade ESTÁVEL da ação (SEM attempt_id) — mission_id,
    task_id, action, platform, account/context, channel/página/perfil,
    payload COMPLETO. Usada diretamente como `critical_action_key`
    para ações críticas, e como base para a identidade de tentativa
    (`compute_attempt_identity`) para ações não críticas.
    `stable_fingerprint` já canonicaliza a ordem das chaves de
    `payload` recursivamente — um payload semanticamente igual com
    chaves em ordem diferente produz a MESMA identidade; qualquer
    diferença material produz uma identidade diferente."""
    return stable_fingerprint(
        mission_id, task_id, action.value, platform, account_context, channel_id, payload,
    )


def compute_attempt_identity(*, execution_identity: str, attempt_id: str | None) -> str:
    """CORREÇÃO (item 1): identidade de idempotência da TENTATIVA —
    para ações não críticas. Sem `attempt_id` (comportamento padrão
    preservado), é idêntica à identidade estável da ação (uma única
    execução por tarefa, como antes desta correção). Com
    `attempt_id`, cada tentativa vira uma identidade DISTINTA — a
    MESMA tentativa (mesmo attempt_id) repetida é idempotente; uma
    NOVA tentativa (novo attempt_id) é uma execução genuinamente
    nova, nunca bloqueada pela idempotência da tentativa anterior."""
    if attempt_id is None:
        return execution_identity
    return stable_fingerprint(execution_identity, attempt_id)


@dataclass
class ExecutionLogEntry:
    entry_id: str
    mission_id: str | None
    task_id: str | None
    platform: str
    account_context: str | None
    task_description: str
    action: str
    result: str
    evidence: dict = field(default_factory=dict)
    error: str | None = None
    retry_count: int = 0
    approval_used: str | None = None
    executor: str = "NavigatorExecutor"
    idempotency_key: str | None = None
    fingerprint: str | None = None
    execution_id: str | None = None
    attempt_id: str | None = None
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def new_id() -> str:
        return f"navlog_{uuid.uuid4().hex[:12]}"
