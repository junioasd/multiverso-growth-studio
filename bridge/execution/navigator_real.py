"""
bridge.execution.navigator_real
====================================
BLOCO FINAL 2 (item 2 — NAVIGATOR REAL) + MICROCORREÇÃO BF2 (itens 1
e 3) + MICROCORREÇÃO (ATOMIC ANTI-REPLAY — falha real confirmada por
auditoria independente usando um servidor HTTP local real: duas
execuções concorrentes com a MESMA escalation_id + MESMA
idempotency_key dispararam 2 requisições HTTP reais). Orquestrador
REAL de execução — aplica a ordem de prioridade OFICIAL: API oficial
> conector/MCP > navegador automatizado > Computer Use/RPA >
MOCK_FALLBACK.

MICROCORREÇÃO — item 1 (criticidade nunca controlada pelo chamador):
a criticidade EFETIVA de toda ação é sempre resolvida por
`bridge.execution.policy_registry.PolicyActionRegistry`.

MICROCORREÇÃO — ANTI-REPLAY ATÔMICO: a causa raiz da corrida anterior
era um padrão "checar depois agir" em dois passos separados (checar
consumo sob lock, LIBERAR o lock, despachar, só então marcar
consumido) — duas threads passavam pela checagem ANTES de qualquer
uma marcar consumo. Corrigido com um CLAIM atômico via `INSERT` numa
tabela com `escalation_id` como `PRIMARY KEY`
(`navigator_execution_claims`): SQLite garante que, entre múltiplas
transações concorrentes tentando inserir a MESMA chave primária
(mesmo processo com múltiplas threads, OU processos diferentes
compartilhando o mesmo banco), exatamente UMA consegue — as demais
recebem `IntegrityError` de forma determinística. Só quem GANHA o
claim despacha a ação real; os demais:
- com a MESMA `idempotency_key`: aguardam (polling limitado, nunca
  infinito) o resultado da execução vencedora e devolvem o MESMO
  resultado — nunca despacham de novo;
- com `idempotency_key` DIFERENTE (ou ausente): são rejeitados
  IMEDIATAMENTE, sem esperar, e NUNCA despacham.

Após sucesso, o claim vira `CONSUMED` (persistido). Após falha, o
claim é LIBERADO (linha deletada) — uma tentativa que falhou nunca
"queima" a aprovação, permitindo nova tentativa genuína.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from atlas.escalation import ApprovalStatus, EscalationDesk
from bridge.execution.browser_executor import BrowserExecutor
from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.execution.policy_registry import ActionCriticality, PolicyActionRegistry
from bridge.hub import BridgeHub, ConnectorType
from core.governance.idempotency import stable_fingerprint

__all__ = [
    "ActionCriticality", "ApprovalContextMismatch", "NavigatorActionResult", "NavigatorReal",
    "ReplayRejected",
]

_MAX_WAIT_FOR_CONCURRENT_EXECUTION_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.02
# MICROCORREÇÃO (reconciliação segura após crash real): um claim
# CLAIMED muito mais velho que qualquer execução real levaria (todas
# as operações reais deste sistema completam em segundos, nunca
# minutos) é tratado como ABANDONADO (processo que morreu no meio da
# execução, sem exceção Python para acionar `_release_claim`) — pode
# ser reivindicado de novo. Nunca reconcilia um claim jovem (só
# porque uma execução real ainda está genuinamente em andamento).
_STALE_CLAIM_THRESHOLD_SECONDS = 120.0


class ApprovalContextMismatch(Exception):
    """MICROCORREÇÃO (item 1 — Owner Approval Binding): uma
    `approved_escalation_id` reutilizada para um contexto diferente
    do originalmente aprovado."""


class ReplayRejected(Exception):
    """MICROCORREÇÃO (Anti-Replay): uma escalação já `CONSUMED`
    reutilizada sem uma `idempotency_key` correspondente."""


@dataclass
class NavigatorActionResult:
    success: bool
    platform: str
    action: str
    mechanism_used: str | None = None
    is_mock_fallback: bool = False
    data: dict = field(default_factory=dict)
    reason: str = ""
    escalation_id: str | None = None
    executed_at: float = field(default_factory=time.time)
    replayed_from_cache: bool = False
    # COMANDO 3/6: NOT_SENT | REJECTED | TRANSIENT | UNKNOWN | ACCEPTED |
    # EXECUTED | CONFIRMED (ver `platform_adapters.PlatformActionResult`).
    delivery_state: str = ""


class NavigatorReal:
    def __init__(
        self, *, bridge_hub: BridgeHub, escalation_desk: EscalationDesk,
        adapters_by_platform: dict | None = None, browser_executor: BrowserExecutor | None = None,
        policy_registry: PolicyActionRegistry | None = None, db_path: str | None = None, audit=None,
    ) -> None:
        self.bridge_hub = bridge_hub
        self.escalation_desk = escalation_desk
        # COMANDO 6/6: validade de uma aprovação a partir da DECISÃO do Owner.
        self.approval_max_age_seconds = 24 * 3600.0
        self.adapters_by_platform: dict = adapters_by_platform or {}
        self.browser_executor = browser_executor if browser_executor is not None else BrowserExecutor()
        self.policy_registry = policy_registry if policy_registry is not None else PolicyActionRegistry()
        self.audit = audit
        self.db_path = db_path
        # MICROCORREÇÃO (anti-replay atômico, modo em memória — usado
        # apenas quando `db_path` não é fornecido): protegido por um
        # único lock, o check-and-set do claim é atômico dentro do
        # MESMO processo (não cobre múltiplos processos — para isso,
        # `db_path` é necessário, já que o claim atômico entre
        # processos depende da constraint de chave primária do
        # SQLite).
        self._memory_lock = threading.Lock()
        self._memory_claims: dict = {}
        if db_path:
            from core.models import db as db_module

            db_module.init_db(db_path)

    def register_adapter(self, *, platform: str, adapter: PlatformApiAdapter) -> None:
        self.adapters_by_platform[platform] = adapter

    def execute_action(
        self, *, platform: str, action: str, criticality: ActionCriticality, params: dict, actor: str,
        adapter_method: str | None = None, url: str | None = None, idempotency_key: str | None = None,
        approved_escalation_id: str | None = None,
    ) -> NavigatorActionResult:
        # v1.0.1: KILL SWITCH GLOBAL — lido a cada chamada, ANTES de claim,
        # aprovação ou despacho (retry/fallback/navegador/API não contornam).
        from core.operations import kill_switch as _ks

        _blocked = _ks.block_reason(self.db_path)
        if _blocked:
            return NavigatorActionResult(success=False, platform=platform, action=action, delivery_state="NOT_SENT",
                                         reason=_blocked, escalation_id=approved_escalation_id)
        effective_criticality = self.policy_registry.effective_criticality(
            action=action, caller_requested=criticality,
        )

        escalation_id = approved_escalation_id
        holding_claim = False
        if effective_criticality == ActionCriticality.CRITICAL:
            approval_check = self._ensure_owner_approval(
                platform=platform, action=action, params=params, actor=actor,
                idempotency_key=idempotency_key, approved_escalation_id=approved_escalation_id,
            )
            if approval_check is not None:
                self._audit_action(result=approval_check, actor=actor, criticality=effective_criticality)
                return approval_check
            # `None` significa: GANHAMOS o claim (fresco ou reclamado
            # após uma falha anterior) -- somos responsáveis por
            # finalizar (consumir/liberar) após o dispatch.
            escalation_id = approved_escalation_id
            holding_claim = True

        try:
            result = self._dispatch(
                platform=platform, action=action, params=params, adapter_method=adapter_method, url=url,
            )
            result.escalation_id = escalation_id
        except Exception:
            # MICROCORREÇÃO (segurança do claim): mesmo uma exceção
            # inesperada durante o dispatch NUNCA deixa o claim preso
            # em CLAIMED para sempre -- sempre libera antes de propagar.
            if holding_claim:
                self._release_claim(escalation_id)
            raise

        if holding_claim:
            # COMANDO 3/6 (defeito provado com servidor HTTP real): antes,
            # QUALQUER falha liberava o claim — um timeout DEPOIS de o
            # serviço executar liberava a aprovação e o retry publicava de
            # novo. Agora só se libera quando é CERTO que nada foi executado.
            if result.success or result.delivery_state == "ACCEPTED":
                self._mark_consumed(escalation_id=escalation_id, idempotency_key=idempotency_key, result=result)
            elif result.delivery_state == "UNKNOWN":
                self._mark_unknown_outcome(escalation_id=escalation_id, idempotency_key=idempotency_key)
            else:
                self._release_claim(escalation_id)

        self._audit_action(result=result, actor=actor, criticality=effective_criticality)
        return result

    def _ensure_owner_approval(
        self, *, platform: str, action: str, params: dict, actor: str, idempotency_key: str | None,
        approved_escalation_id: str | None,
    ) -> NavigatorActionResult | None:
        current_fingerprint = self._binding_fingerprint(platform=platform, action=action, params=params, actor=actor)

        if approved_escalation_id:
            item = self.escalation_desk.get(approved_escalation_id)
            if item.status != ApprovalStatus.APPROVED:
                return NavigatorActionResult(
                    success=False, platform=platform, action=action,
                    reason=f"escalação '{approved_escalation_id}' não está APPROVED (status atual: {item.status.value})",
                    escalation_id=approved_escalation_id,
                )
            # COMANDO 6/6 (lacuna provada): `expires_at` existia mas NUNCA
            # era verificado, e escalações nasciam sem prazo — uma aprovação
            # antiga autorizava a ação indefinidamente. Agora: prazo explícito
            # (`expires_at`) OU idade máxima da DECISÃO do Owner.
            now_ts = time.time()
            decided_at = getattr(item, "decided_at", None) or item.updated_at
            if (item.expires_at is not None and now_ts >= item.expires_at) or \
                    (decided_at is not None and now_ts - decided_at > self.approval_max_age_seconds):
                return NavigatorActionResult(
                    success=False, platform=platform, action=action, delivery_state="NOT_SENT",
                    reason=(f"aprovação '{approved_escalation_id}' EXPIRADA — ações expiradas nunca usam aprovação "
                            f"antiga; solicitar nova aprovação do Owner"),
                    escalation_id=approved_escalation_id,
                )
            stored_fingerprint = item.context.get("binding_fingerprint")
            if stored_fingerprint != current_fingerprint:
                return NavigatorActionResult(
                    success=False, platform=platform, action=action,
                    reason=(
                        f"escalação '{approved_escalation_id}' foi aprovada para um contexto DIFERENTE "
                        f"(operação/plataforma/ação/parâmetros/ator) — reuso rejeitado, nunca um bypass de aprovação"
                    ),
                    escalation_id=approved_escalation_id,
                )

            # MICROCORREÇÃO (anti-replay ATÔMICO): tenta reivindicar o
            # direito EXCLUSIVO de executar esta escalação.
            return self._resolve_claim(
                escalation_id=approved_escalation_id, idempotency_key=idempotency_key, platform=platform,
                action=action, deadline=time.time() + _MAX_WAIT_FOR_CONCURRENT_EXECUTION_SECONDS,
            )

        item = self.escalation_desk.escalate(
            reason=f"Ação crítica '{action}' em '{platform}' requer aprovação explícita do Owner",
            context={
                "platform": platform, "action": action, "params": params, "actor": actor,
                "binding_fingerprint": current_fingerprint,
            },
            actor=actor, operation=f"navigator_{action.lower()}", risk="HIGH",
            idempotency_key=idempotency_key,
        )
        return NavigatorActionResult(
            success=False, platform=platform, action=action,
            reason=f"ação crítica bloqueada aguardando aprovação do Owner (escalation_id={item.escalation_id})",
            escalation_id=item.escalation_id,
        )

    def _resolve_claim(
        self, *, escalation_id: str, idempotency_key: str | None, platform: str, action: str, deadline: float,
    ) -> NavigatorActionResult | None:
        """MICROCORREÇÃO (anti-replay ATÔMICO): loop de resolução —
        tenta reivindicar o claim; se outra execução já está em
        andamento com a MESMA idempotency_key, aguarda (polling
        limitado por `deadline`, nunca infinito) até ela resolver.
        Devolve `None` quando NÓS ganhamos o claim (siga para
        dispatch); devolve um `NavigatorActionResult` quando a
        resposta já está decidida (cache ou rejeição)."""
        while True:
            outcome, payload = self._try_claim(escalation_id=escalation_id, idempotency_key=idempotency_key)

            if outcome == "WON":
                return None
            if outcome == "CONSUMED_MATCH":
                return self._cached_result(payload)
            if outcome == "CONSUMED_MISMATCH":
                return NavigatorActionResult(
                    success=False, platform=platform, action=action,
                    reason=(
                        f"escalação '{escalation_id}' já foi CONSUMED (executada com sucesso anteriormente) — "
                        f"reuso sem a idempotency_key original é sempre rejeitado como replay, nunca duplica o "
                        f"efeito externo"
                    ),
                    escalation_id=escalation_id,
                )
            if outcome == "UNKNOWN_OUTCOME":
                return NavigatorActionResult(
                    success=False, platform=platform, action=action, delivery_state="UNKNOWN",
                    reason=(
                        f"escalação '{escalation_id}' tem resultado externo DESCONHECIDO (a ação pode ter sido "
                        f"executada) — reenvio recusado; o Owner deve reconciliar via reconcile_unknown_outcome()"
                    ),
                    escalation_id=escalation_id,
                )
            if outcome == "IN_FLIGHT_MISMATCH":
                return NavigatorActionResult(
                    success=False, platform=platform, action=action,
                    reason=(
                        f"escalação '{escalation_id}' está sendo executada concorrentemente com uma "
                        f"idempotency_key diferente — rejeitado imediatamente, nunca aguarda nem duplica"
                    ),
                    escalation_id=escalation_id,
                )
            # IN_FLIGHT_MATCH -- outra execução concorrente com a MESMA
            # idempotency_key está em andamento; aguarda o resultado
            # dela em vez de despachar de novo.
            if time.time() >= deadline:
                return NavigatorActionResult(
                    success=False, platform=platform, action=action,
                    reason=(
                        f"tempo esgotado aguardando a execução concorrente da escalação '{escalation_id}' "
                        f"terminar — nenhum efeito externo duplicado, mas a execução original não concluiu a tempo"
                    ),
                    escalation_id=escalation_id,
                )
            time.sleep(_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _binding_fingerprint(*, platform: str, action: str, params: dict, actor: str) -> str:
        return stable_fingerprint(platform, action, params, actor)

    @staticmethod
    def _cached_result(payload: dict) -> NavigatorActionResult:
        return NavigatorActionResult(
            success=payload["result_success"], platform=payload["result_platform"],
            action=payload["result_action"], mechanism_used=payload["result_mechanism_used"],
            is_mock_fallback=payload["result_is_mock_fallback"], data=payload["result_data"],
            reason=payload["result_reason"], escalation_id=payload["escalation_id"], replayed_from_cache=True,
        )

    # ------------------------------------------------------------------
    # Claim atômico — dispatcher para modo persistido (SQLite) ou em
    # memória (thread-safe, um único processo)
    # ------------------------------------------------------------------

    def _try_claim(self, *, escalation_id: str, idempotency_key: str | None) -> tuple:
        if self.db_path:
            return self._try_claim_db(escalation_id=escalation_id, idempotency_key=idempotency_key)
        return self._try_claim_memory(escalation_id=escalation_id, idempotency_key=idempotency_key)

    def _try_claim_db(self, *, escalation_id: str, idempotency_key: str | None) -> tuple:
        from core.models import db as db_module

        # CORREÇÃO (atomicidade real entre threads E processos): a
        # tentativa de INSERT é a operação atômica -- SQLite serializa
        # escritores; entre múltiplas transações concorrentes tentando
        # inserir a MESMA chave primária, exatamente uma commita com
        # sucesso, as demais recebem IntegrityError de forma
        # determinística (nunca as duas "vencem").
        try:
            with db_module.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO navigator_execution_claims (escalation_id, status, idempotency_key, claimed_at) "
                    "VALUES (?, 'CLAIMED', ?, ?)",
                    (escalation_id, idempotency_key, time.time()),
                )
            return "WON", None
        except sqlite3.IntegrityError:
            pass  # já existe uma linha -- lê o estado atual abaixo

        with db_module.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM navigator_execution_claims WHERE escalation_id = ?", (escalation_id,),
            ).fetchone()
        if row is None:
            # janela rara: a linha foi liberada entre o IntegrityError
            # e esta leitura -- tenta reivindicar de novo (recursão
            # limitada pela natureza do próprio estado, nunca infinita
            # na prática).
            return self._try_claim_db(escalation_id=escalation_id, idempotency_key=idempotency_key)

        # MICROCORREÇÃO (reconciliação segura após crash real): claim
        # CLAIMED (nunca chegou a CONSUMED) muito mais velho que
        # qualquer execução real levaria é tratado como abandonado —
        # um processo que morreu no meio da execução (sem chance de
        # rodar `_release_claim` via except/finally) nunca pode travar
        # a aprovação para sempre. DELETE condicional na própria
        # cláusula WHERE (claimed_at antigo) torna a reconciliação
        # atômica também: se outra transação já reconciliou/reclamou
        # primeiro, este DELETE simplesmente não afeta nenhuma linha.
        if row["status"] == "CLAIMED" and (time.time() - row["claimed_at"]) > _STALE_CLAIM_THRESHOLD_SECONDS:
            # COMANDO 3/6 (defeito provado com crash real): o processo que
            # morreu PODE ter enviado a requisição. Apagar o claim e
            # reexecutar duplicava a ação. O claim vira UNKNOWN_OUTCOME e
            # só a reconciliação do Owner o libera (nunca trava para sempre).
            with db_module.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE navigator_execution_claims SET status = 'UNKNOWN_OUTCOME' WHERE escalation_id = ? "
                    "AND status = 'CLAIMED' AND claimed_at = ?",
                    (escalation_id, row["claimed_at"]),
                )
            return self._try_claim_db(escalation_id=escalation_id, idempotency_key=idempotency_key)
        if row["status"] == "UNKNOWN_OUTCOME":
            return "UNKNOWN_OUTCOME", None

        payload = {
            "escalation_id": escalation_id, "result_success": bool(row["result_success"]) if row["result_success"] is not None else False,
            "result_data": json.loads(row["result_data"]) if row["result_data"] else {},
            "result_reason": row["result_reason"] or "", "result_mechanism_used": row["result_mechanism_used"],
            "result_is_mock_fallback": bool(row["result_is_mock_fallback"]) if row["result_is_mock_fallback"] is not None else False,
            "result_action": row["result_action"] or "", "result_platform": row["result_platform"] or "",
        }
        if row["status"] == "CONSUMED":
            if idempotency_key is not None and row["idempotency_key"] == idempotency_key:
                return "CONSUMED_MATCH", payload
            return "CONSUMED_MISMATCH", None
        # CLAIMED -- em andamento por outra execução
        if idempotency_key is not None and row["idempotency_key"] == idempotency_key:
            return "IN_FLIGHT_MATCH", None
        return "IN_FLIGHT_MISMATCH", None

    def _try_claim_memory(self, *, escalation_id: str, idempotency_key: str | None) -> tuple:
        with self._memory_lock:
            state = self._memory_claims.get(escalation_id)
            if state is None:
                self._memory_claims[escalation_id] = {
                    "status": "CLAIMED", "idempotency_key": idempotency_key, "claimed_at": time.time(),
                }
                return "WON", None
            if state["status"] == "CONSUMED":
                if idempotency_key is not None and state["idempotency_key"] == idempotency_key:
                    return "CONSUMED_MATCH", state["result"]
                return "CONSUMED_MISMATCH", None
            if state["status"] == "UNKNOWN_OUTCOME":
                return "UNKNOWN_OUTCOME", None
            if state["status"] == "CLAIMED" and (time.time() - state["claimed_at"]) > _STALE_CLAIM_THRESHOLD_SECONDS:
                state["status"] = "UNKNOWN_OUTCOME"
                return "UNKNOWN_OUTCOME", None
            if idempotency_key is not None and state["idempotency_key"] == idempotency_key:
                return "IN_FLIGHT_MATCH", None
            return "IN_FLIGHT_MISMATCH", None

    def _mark_consumed(self, *, escalation_id: str, idempotency_key: str | None, result: NavigatorActionResult) -> None:
        payload = {
            "escalation_id": escalation_id, "result_success": result.success, "result_platform": result.platform,
            "result_action": result.action, "result_mechanism_used": result.mechanism_used,
            "result_is_mock_fallback": result.is_mock_fallback, "result_data": result.data,
            "result_reason": result.reason,
        }
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE navigator_execution_claims SET status='CONSUMED', consumed_at=?, result_success=?, "
                    "result_data=?, result_reason=?, result_mechanism_used=?, result_is_mock_fallback=?, "
                    "result_action=?, result_platform=? WHERE escalation_id=?",
                    (
                        time.time(), int(result.success), json.dumps(result.data), result.reason,
                        result.mechanism_used, int(result.is_mock_fallback), result.action, result.platform,
                        escalation_id,
                    ),
                )
        else:
            with self._memory_lock:
                self._memory_claims[escalation_id] = {
                    "status": "CONSUMED", "idempotency_key": idempotency_key, "result": payload,
                }

    def _mark_unknown_outcome(self, *, escalation_id: str, idempotency_key: str | None) -> None:
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE navigator_execution_claims SET status = 'UNKNOWN_OUTCOME' WHERE escalation_id = ? "
                    "AND status = 'CLAIMED'", (escalation_id,),
                )
        else:
            with self._memory_lock:
                state = self._memory_claims.get(escalation_id)
                if state is not None and state["status"] == "CLAIMED":
                    state["status"] = "UNKNOWN_OUTCOME"

    def reconcile_unknown_outcome(self, *, escalation_id: str, executed: bool, evidence: str, resolved_by) -> str:
        """COMANDO 3/6 — reconciliação de resultado externo DESCONHECIDO.
        Só o Owner decide, com evidência declarada (ex.: verificação na
        plataforma). `executed=True` -> CONSUMED (a aprovação não autoriza
        repetir); `executed=False` -> libera UMA nova tentativa."""
        from core.security.rbac import Role

        if getattr(resolved_by, "role", None) != Role.OWNER:
            raise PermissionError("apenas o Owner reconcilia um resultado externo desconhecido")
        if not evidence:
            raise ValueError("reconciliação exige evidência declarada")
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                if executed:
                    cur = conn.execute(
                        "UPDATE navigator_execution_claims SET status='CONSUMED', consumed_at=?, result_success=1, "
                        "result_reason=? WHERE escalation_id=? AND status='UNKNOWN_OUTCOME'",
                        (time.time(), f"CONFIRMADO por reconciliação do Owner: {evidence}", escalation_id),
                    )
                else:
                    cur = conn.execute(
                        "DELETE FROM navigator_execution_claims WHERE escalation_id=? AND status='UNKNOWN_OUTCOME'",
                        (escalation_id,),
                    )
                changed = cur.rowcount
        else:
            with self._memory_lock:
                state = self._memory_claims.get(escalation_id)
                changed = int(state is not None and state["status"] == "UNKNOWN_OUTCOME")
                if changed and executed:
                    self._memory_claims[escalation_id] = {
                        "status": "CONSUMED", "idempotency_key": state["idempotency_key"],
                        "result": {"escalation_id": escalation_id, "result_success": True, "result_platform": "",
                                   "result_action": "", "result_mechanism_used": None,
                                   "result_is_mock_fallback": False, "result_data": {},
                                   "result_reason": f"CONFIRMADO por reconciliação do Owner: {evidence}"},
                    }
                elif changed:
                    del self._memory_claims[escalation_id]
        if not changed:
            raise ValueError(f"escalação '{escalation_id}' não está em UNKNOWN_OUTCOME")
        outcome = "CONFIRMED_EXECUTED" if executed else "RELEASED_NOT_EXECUTED"
        if self.audit is not None:
            self.audit.record(
                actor=getattr(resolved_by, "subject", "owner"), action="navigator_reconcile_unknown_outcome",
                resource=escalation_id, outcome="SUCCESS", details={"outcome": outcome, "evidence": evidence},
            )
        return outcome

    def register_webhook(self, *, platform: str, url: str, vault=None, secret_reference_id: str | None = None,
                         timeout_seconds: float = 10.0) -> None:
        """COMANDO 3/6 — conector WEBHOOK do Access & Execution Hub. O
        segredo de assinatura HMAC fica no `TokenVault` (referência), nunca
        no registro/código/log; é lido só no momento do envio."""
        if not hasattr(self, "_webhooks"):
            self._webhooks = {}
        self._webhooks[platform] = {"url": url, "vault": vault, "secret_ref": secret_reference_id,
                                    "timeout": timeout_seconds}

    def _dispatch_webhook(self, *, platform: str, action: str, params: dict) -> NavigatorActionResult:
        from bridge.execution.webhooks import RetryPolicy, WebhookDispatcher

        cfg = getattr(self, "_webhooks", {}).get(platform)
        if cfg is None:
            return NavigatorActionResult(
                success=False, platform=platform, action=action, mechanism_used=ConnectorType.WEBHOOK.value,
                delivery_state="NOT_SENT", reason=f"nenhum endpoint de webhook registrado para '{platform}'",
            )
        secret = cfg["vault"].retrieve_token(cfg["secret_ref"]) if cfg["vault"] and cfg["secret_ref"] else None
        # uma tentativa por chamada: a repetição é decidida pelo claim do
        # NavigatorReal / OperationRunner, que conhecem o estado de entrega.
        dispatcher = WebhookDispatcher(retry_policy=RetryPolicy(max_attempts=1), timeout_seconds=cfg["timeout"])
        r = dispatcher.deliver(url=cfg["url"], payload={"action": action, "params": params}, secret=secret)
        return NavigatorActionResult(
            success=r.success, platform=platform, action=action, mechanism_used=ConnectorType.WEBHOOK.value,
            data={"delivery_id": r.delivery_id, "status_code": r.status_code}, reason=r.reason,
            delivery_state=r.delivery or ("EXECUTED" if r.success else "NOT_SENT"),
        )

    def _release_claim(self, escalation_id: str) -> None:
        """MICROCORREÇÃO (falha nunca deixa o claim preso): remove a
        linha/entrada CLAIMED — libera para uma nova tentativa
        genuína. Uma execução que FALHOU nunca "queima" a aprovação."""
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                conn.execute(
                    "DELETE FROM navigator_execution_claims WHERE escalation_id = ? AND status = 'CLAIMED'",
                    (escalation_id,),
                )
        else:
            with self._memory_lock:
                state = self._memory_claims.get(escalation_id)
                if state is not None and state["status"] == "CLAIMED":
                    del self._memory_claims[escalation_id]

    def _dispatch(
        self, *, platform: str, action: str, params: dict, adapter_method: str | None, url: str | None,
    ) -> NavigatorActionResult:
        best = self.bridge_hub.best_available_execution_path(platform=platform)
        if best is None:
            return NavigatorActionResult(
                success=False, platform=platform, action=action,
                reason=f"nenhum mecanismo de execução viável para '{platform}' — nenhum conector/API configurado",
            )

        if best.connector_type == ConnectorType.OFFICIAL_API:
            return self._dispatch_official_api(platform=platform, action=action, params=params, adapter_method=adapter_method)
        if best.connector_type == ConnectorType.LOCAL_EXECUTION:
            return self._dispatch_local_execution(
                platform=platform,
                action=action,
                params=params,
                adapter_method=adapter_method,
            )
        if best.connector_type == ConnectorType.BROWSER_AUTOMATION:
            return self._dispatch_browser(platform=platform, action=action, url=url)
        if best.connector_type == ConnectorType.WEBHOOK:
            return self._dispatch_webhook(platform=platform, action=action, params=params)
        if best.connector_type == ConnectorType.MOCK_FALLBACK:
            return NavigatorActionResult(
                success=False, platform=platform, action=action, mechanism_used=best.connector_type.value,
                is_mock_fallback=True,
                reason=f"apenas MOCK_FALLBACK disponível para '{platform}' — nenhuma execução real ocorreu",
            )
        return NavigatorActionResult(
            success=False, platform=platform, action=action, mechanism_used=best.connector_type.value,
            reason=f"mecanismo '{best.connector_type.value}' reconhecido mas sem dispatcher implementado neste Navigator",
        )

    def _dispatch_local_execution(
        self, *, platform: str, action: str, params: dict,
        adapter_method: str | None,
    ) -> NavigatorActionResult:
        adapter = self.adapters_by_platform.get(platform)

        if adapter is None:
            return NavigatorActionResult(
                success=False,
                platform=platform,
                action=action,
                mechanism_used=ConnectorType.LOCAL_EXECUTION.value,
                reason=f"LOCAL_EXECUTION viavel no BridgeHub, mas sem adapter registrado para '{platform}'",
            )

        if not adapter_method or not hasattr(adapter, adapter_method):
            return NavigatorActionResult(
                success=False,
                platform=platform,
                action=action,
                mechanism_used=ConnectorType.LOCAL_EXECUTION.value,
                reason=f"adapter_method '{adapter_method}' nao existe no adapter de '{platform}'",
            )

        method = getattr(adapter, adapter_method)
        platform_result = method(**params)

        return NavigatorActionResult(
            success=platform_result.success,
            platform=platform,
            action=action,
            mechanism_used=ConnectorType.LOCAL_EXECUTION.value,
            data=platform_result.data,
            reason=platform_result.reason,
            delivery_state=getattr(platform_result, "delivery", None)
            or ("EXECUTED" if platform_result.success else "NOT_SENT"),
        )

    def _dispatch_official_api(
        self, *, platform: str, action: str, params: dict, adapter_method: str | None,
    ) -> NavigatorActionResult:
        adapter = self.adapters_by_platform.get(platform)
        if adapter is None:
            return NavigatorActionResult(
                success=False, platform=platform, action=action, mechanism_used=ConnectorType.OFFICIAL_API.value,
                reason=f"API oficial marcada como viável em BridgeHub, mas nenhum adapter real registrado para '{platform}'",
            )
        if not adapter_method or not hasattr(adapter, adapter_method):
            return NavigatorActionResult(
                success=False, platform=platform, action=action, mechanism_used=ConnectorType.OFFICIAL_API.value,
                reason=f"adapter_method '{adapter_method}' não existe no adapter de '{platform}'",
            )
        method = getattr(adapter, adapter_method)
        platform_result = method(**params)
        return NavigatorActionResult(
            success=platform_result.success, platform=platform, action=action,
            mechanism_used=ConnectorType.OFFICIAL_API.value, data=platform_result.data, reason=platform_result.reason,
            delivery_state=getattr(platform_result, "delivery", None)
            or ("EXECUTED" if platform_result.success else "NOT_SENT"),
        )

    def _dispatch_browser(self, *, platform: str, action: str, url: str | None) -> NavigatorActionResult:
        if not url:
            return NavigatorActionResult(
                success=False, platform=platform, action=action, mechanism_used=ConnectorType.BROWSER_AUTOMATION.value,
                reason="navegador automatizado escolhido como mecanismo, mas nenhuma URL foi fornecida",
            )
        # CORREÇÃO PÓS-AUDITORIA (bloqueador B — reproduzido): o executor
        # de navegador só NAVEGA e EXTRAI; `action`/`params` eram ignorados
        # e "página aberta" virava sucesso de PUBLISH/SEND/UPDATE, consumindo
        # a aprovação crítica. Navegação só prova LEITURA. Ação mutável (tudo
        # que o PolicyActionRegistry não classifica como ROUTINE/leitura) é
        # recusada ANTES de qualquer efeito: NOT_SENT libera a aprovação.
        if self.policy_registry.effective_criticality(
                action=action, caller_requested=ActionCriticality.ROUTINE) != ActionCriticality.ROUTINE:
            return NavigatorActionResult(
                success=False, platform=platform, action=action, mechanism_used=ConnectorType.BROWSER_AUTOMATION.value,
                delivery_state="NOT_SENT",
                reason=(f"'{action}' é ação MUTÁVEL: o navegador automatizado só navega/extrai e não tem executor "
                        f"catalogado que execute e confirme essa mutação — nada foi executado"),
            )
        browser_result = self.browser_executor.navigate_and_extract(url=url)
        return NavigatorActionResult(
            success=browser_result.success, platform=platform, action=action,
            mechanism_used=ConnectorType.BROWSER_AUTOMATION.value,
            data={"final_url": browser_result.final_url, "page_title": browser_result.page_title},
            reason=browser_result.reason,
            delivery_state="EXECUTED" if browser_result.success else "NOT_SENT",
        )

    def _audit_action(self, *, result: NavigatorActionResult, actor: str, criticality: ActionCriticality) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(
                actor=actor, action=f"NAVIGATOR_{result.action}", resource=result.platform,
                outcome="SUCCESS" if result.success else "FAILED",
                details={
                    "mechanism_used": result.mechanism_used, "is_mock_fallback": result.is_mock_fallback,
                    "criticality": criticality.value, "reason": result.reason, "escalation_id": result.escalation_id,
                    "replayed_from_cache": result.replayed_from_cache,
                },
            )
        except Exception:
            pass
