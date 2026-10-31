"""
navigator.executor
======================
NAVIGATOR — Executor Operacional Multicanal Master.

Hierarquia obrigatória:
    ATLAS     = planeja, decide, coordena, supervisiona
    NAVIGATOR = EXECUTA operacionalmente nas plataformas (este módulo)
    BRIDGE    = fornece conexão técnica (nunca executa nada de negócio)

CORREÇÃO (Owner Approval real): uma ação crítica gera UMA
`owner_approval_request` (via `critical_action_key` — identidade
ESTÁVEL, nunca dependente de attempt_id). Uma vez `APPROVED`, a
próxima chamada CONSOME-A — executa uma única vez, vincula
`approval_used`. `REJECTED`/`EXPIRED`/`CANCELLED` nunca autorizam.

CORREÇÃO (microcorreção V3 — item 1: REPLAN + REEXECUTE com NAVIGATOR
real): separa `critical_action_key` (estável — SEM attempt_id, usada
para ações críticas/Owner Approval) de `idempotency_key` de tentativa
(inclui `attempt_id` para ações NÃO críticas — permite retry legítimo
de uma execução `FAILED` com um `attempt_id` novo, mantendo a MESMA
tentativa idempotente se repetida). Retries só avançam quando a ação é
`retry_safe` (lista padrão em `navigator.models.RETRY_SAFE_ACTIONS`)
ou o chamador declara explicitamente `retry_safe_override=True`
(idempotência externa/receipt verificável) — caso contrário, uma nova
tentativa é recusada (`BLOCKED`). Um resultado INCERTO de ação
crítica (exceção durante a consumação) nunca é `FAILED` comum — vira
`NEEDS_VERIFICATION`, nunca repetido automaticamente.

CORREÇÃO (tools_allowed de ponta a ponta): `execute(...,
tools_allowed=[...])` restringe a busca do BRIDGE aos mecanismos
explicitamente permitidos.

CORREÇÃO (BRIDGE -> NAVIGATOR -> EXECUTOR REAL): o mecanismo
selecionado pelo BRIDGE determina o executor de fato chamado; antes de
`SUCCESS`, `selected_mechanism` é verificado contra o `mode`
realmente devolvido.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from atlas.escalation import ApprovalStatus, EscalationDesk
from bridge.hub import BridgeHub, ConnectorType, build_default_bridge
from core.adapters.base import AdapterMode
from core.governance.idempotency import IdempotencyConflict
from core.isolation.isolation import IsolationGuard, ResourceScope
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role
from navigator.adapters import DEFAULT_PLATFORMS
from navigator.execution_log import NavigatorExecutionLog
from navigator.mechanism_executors import MechanismExecutor, build_default_mechanism_executor
from navigator.models import (
    CRITICAL_NAVIGATOR_ACTIONS,
    RETRY_SAFE_ACTIONS,
    ExecutionLogEntry,
    ExecutionResult,
    NavigatorAction,
    compute_attempt_identity,
    compute_execution_identity,
)


class NavigatorHierarchyViolation(Exception):
    pass


class UnknownPlatform(Exception):
    pass


@dataclass
class TemporaryExecutor:
    executor_id: str
    platform: str
    navigator: "NavigatorExecutor"

    def run(
        self, *, action: NavigatorAction, payload: dict, mission_id: str | None = None,
        task_id: str | None = None, channel_id: str | None = None, account_context: str | None = None,
        idempotency_key: str | None = None, tools_allowed: list | None = None,
        attempt_id: str | None = None, execution_id: str | None = None,
        retry_safe_override: bool | None = None,
    ) -> ExecutionLogEntry:
        return self.navigator.execute(
            mission_id=mission_id, task_id=task_id, channel_id=channel_id, platform=self.platform,
            account_context=account_context, action=action, payload=payload, executor_name=self.executor_id,
            idempotency_key=idempotency_key, tools_allowed=tools_allowed, attempt_id=attempt_id,
            execution_id=execution_id, retry_safe_override=retry_safe_override,
        )


@dataclass
class NavigatorExecutor:
    principal: Principal
    escalation_desk: EscalationDesk
    audit: AuditTrail
    db_path: str | None = None
    max_retries: int = 3
    bridge: BridgeHub = None  # type: ignore[assignment]
    log: NavigatorExecutionLog = field(init=False)

    def __post_init__(self) -> None:
        if self.principal.role != Role.NAVIGATOR:
            raise NavigatorHierarchyViolation(
                f"NavigatorExecutor exige um Principal de role NAVIGATOR — recebeu '{self.principal.role.value}'."
            )
        self.log = NavigatorExecutionLog(db_path=self.db_path)
        if self.bridge is None:
            self.bridge = build_default_bridge(platforms=DEFAULT_PLATFORMS)
        self._temporary_executors: dict = {}
        self._mechanism_executors: dict = {}
        self._known_platforms: set = set(DEFAULT_PLATFORMS)

    # ------------------------------------------------------------------
    # Registro de executores por mecanismo
    # ------------------------------------------------------------------

    def register_mechanism_executor(
        self, *, platform: str, mechanism: ConnectorType, executor: MechanismExecutor,
    ) -> None:
        self._mechanism_executors[(platform, mechanism)] = executor
        self._known_platforms.add(platform)

    def _get_mechanism_executor(self, platform: str, mechanism: ConnectorType) -> MechanismExecutor:
        key = (platform, mechanism)
        if key not in self._mechanism_executors:
            self._mechanism_executors[key] = build_default_mechanism_executor(mechanism, provider_name=platform)
        return self._mechanism_executors[key]

    @staticmethod
    def _parse_allowed_mechanisms(tools_allowed: list | None) -> set | None:
        if not tools_allowed:
            return None
        parsed = set()
        for name in tools_allowed:
            try:
                parsed.add(ConnectorType(name))
            except ValueError:
                continue
        return parsed

    # ------------------------------------------------------------------
    # Executores temporários
    # ------------------------------------------------------------------

    def spawn_temporary_executor(self, *, platform: str) -> TemporaryExecutor:
        if platform not in self._known_platforms:
            raise UnknownPlatform(platform)
        executor = TemporaryExecutor(
            executor_id=f"exec_{platform}_{uuid.uuid4().hex[:8]}", platform=platform, navigator=self,
        )
        self._temporary_executors[executor.executor_id] = executor
        return executor

    def temporary_executors(self) -> list:
        return list(self._temporary_executors.values())

    # ------------------------------------------------------------------
    # Execução principal
    # ------------------------------------------------------------------

    def execute(
        self,
        *,
        platform: str,
        action: NavigatorAction,
        payload: dict,
        mission_id: str | None = None,
        task_id: str | None = None,
        channel_id: str | None = None,
        account_context: str | None = None,
        executor_name: str = "NavigatorExecutor",
        idempotency_key: str | None = None,
        tools_allowed: list | None = None,
        attempt_id: str | None = None,
        execution_id: str | None = None,
        retry_safe_override: bool | None = None,
    ) -> ExecutionLogEntry:
        if channel_id is not None:
            IsolationGuard.check(self.principal.scopes, ResourceScope(channel_id=channel_id))

        # `critical_action_key` — identidade ESTÁVEL da ação (nunca
        # inclui attempt_id). Ações críticas SEMPRE usam esta chave —
        # Owner Approval e consumo protegidos contra duplicação
        # independentemente de quantas tentativas/replans levaram até
        # aqui.
        critical_action_key = compute_execution_identity(
            mission_id=mission_id, task_id=task_id, action=action, platform=platform,
            account_context=account_context, channel_id=channel_id, payload=payload,
        )

        if action in CRITICAL_NAVIGATOR_ACTIONS:
            key = idempotency_key or critical_action_key
            fingerprint = critical_action_key
            return self._execute_critical(
                mission_id=mission_id, task_id=task_id, platform=platform, account_context=account_context,
                action=action, payload=payload, executor_name=executor_name, key=key, fingerprint=fingerprint,
                tools_allowed=tools_allowed, attempt_id=attempt_id, execution_id=execution_id,
            )

        # Não crítica: identidade de TENTATIVA — inclui attempt_id
        # quando fornecido, permitindo retry legítimo de um FAILED
        # anterior sem que a idempotência o bloqueie indevidamente.
        attempt_fingerprint = compute_attempt_identity(execution_identity=critical_action_key, attempt_id=attempt_id)
        key = idempotency_key or attempt_fingerprint

        existing = self.log.find_by_idempotency_key(key)
        if existing is not None:
            if existing.fingerprint != attempt_fingerprint:
                raise IdempotencyConflict(
                    f"idempotency_key '{key}' já usada para uma execução com identidade diferente "
                    "(mission/task/action/platform/conta/canal/payload/tentativa) — reutilização incompatível recusada"
                )
            return existing  # MESMA tentativa repetida — idempotente, nunca reexecuta

        if platform not in self._known_platforms:
            entry = ExecutionLogEntry(
                entry_id=ExecutionLogEntry.new_id(), mission_id=mission_id, task_id=task_id, platform=platform,
                account_context=account_context, task_description=f"{action.value} em {platform}",
                action=action.value, result=ExecutionResult.FAILED.value,
                error=f"plataforma desconhecida: {platform}", executor=executor_name,
                idempotency_key=key, fingerprint=attempt_fingerprint, execution_id=execution_id,
                attempt_id=attempt_id,
            )
            return self.log.record(entry)

        # CORREÇÃO (item 1): uma NOVA tentativa (attempt_id fornecido e
        # já existem tentativas anteriores para a MESMA tarefa/ação)
        # só é permitida quando a ação é retry-safe por padrão ou o
        # chamador declarou explicitamente retry_safe_override=True.
        if attempt_id is not None and mission_id is not None and task_id is not None:
            prior_attempts = self.log.by_task(mission_id=mission_id, task_id=task_id, action=action.value)
            is_retry = len(prior_attempts) > 0
            if is_retry and action not in RETRY_SAFE_ACTIONS and not retry_safe_override:
                entry = ExecutionLogEntry(
                    entry_id=ExecutionLogEntry.new_id(), mission_id=mission_id, task_id=task_id, platform=platform,
                    account_context=account_context, task_description=f"{action.value} em {platform}",
                    action=action.value, result=ExecutionResult.BLOCKED.value,
                    error=(
                        f"nova tentativa recusada: ação '{action.value}' não é retry-safe e "
                        "retry_safe_override não foi declarado — risco de efeito colateral duplicado"
                    ),
                    evidence={"prior_attempts": len(prior_attempts), "retry_safe": False},
                    executor=executor_name, idempotency_key=key, fingerprint=attempt_fingerprint,
                    execution_id=execution_id, attempt_id=attempt_id,
                )
                self.audit.record(
                    actor=self.principal.subject, action="navigator_retry_not_authorized", resource=platform,
                    outcome="ERROR", details={"action": action.value, "attempt_id": attempt_id},
                )
                return self.log.record(entry)

        allowed_mechanisms = self._parse_allowed_mechanisms(tools_allowed)
        path = self.bridge.best_available_execution_path(platform=platform, allowed_mechanisms=allowed_mechanisms)
        if path is None:
            return self._record_no_path(
                mission_id=mission_id, task_id=task_id, platform=platform, account_context=account_context,
                action=action, executor_name=executor_name, key=key, fingerprint=attempt_fingerprint,
                tools_allowed=tools_allowed, execution_id=execution_id, attempt_id=attempt_id,
            )

        return self._execute_via_mechanism(
            mission_id=mission_id, task_id=task_id, platform=platform, account_context=account_context,
            action=action, payload=payload, executor_name=executor_name, key=key, fingerprint=attempt_fingerprint,
            path=path, tools_allowed=tools_allowed, execution_id=execution_id, attempt_id=attempt_id,
        )

    def _record_no_path(
        self, *, mission_id, task_id, platform, account_context, action, executor_name, key, fingerprint,
        tools_allowed, execution_id, attempt_id,
    ) -> ExecutionLogEntry:
        all_viable = [c.connector_type.value for c in self.bridge.available_connectors(platform=platform) if c.is_viable()]
        reason = (
            "nenhum caminho de execução disponível dentro de tools_allowed"
            if tools_allowed else "nenhum caminho de execução disponível (BRIDGE sem conector viável)"
        )
        entry = ExecutionLogEntry(
            entry_id=ExecutionLogEntry.new_id(), mission_id=mission_id, task_id=task_id, platform=platform,
            account_context=account_context, task_description=f"{action.value} em {platform}",
            action=action.value, result=ExecutionResult.BLOCKED.value, error=reason,
            evidence={
                "requested_tools": tools_allowed or [], "allowed_tools": tools_allowed or [],
                "selected_tool": None, "rejected_tools": all_viable,
            },
            executor=executor_name, idempotency_key=key, fingerprint=fingerprint,
            execution_id=execution_id, attempt_id=attempt_id,
        )
        self.audit.record(
            actor=self.principal.subject, action="navigator_no_execution_path", resource=platform,
            outcome="ERROR", details={"action": action.value, "tools_allowed": tools_allowed or [], "reason": reason},
        )
        return self.log.record(entry)

    # ------------------------------------------------------------------
    # Ações críticas — consumo de Owner Approval (chave ESTÁVEL)
    # ------------------------------------------------------------------

    def _execute_critical(
        self, *, mission_id, task_id, platform, account_context, action, payload, executor_name, key,
        fingerprint, tools_allowed, attempt_id, execution_id,
    ) -> ExecutionLogEntry:
        existing = self.log.find_by_idempotency_key(key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency_key '{key}' já usada para uma ação crítica com identidade diferente — "
                    "reutilização incompatível recusada"
                )
            # SUCCESS e NEEDS_VERIFICATION são finais — nunca repetidos
            # automaticamente, não importa quantas "tentativas"/replans
            # aconteçam depois.
            if existing.result in (ExecutionResult.SUCCESS.value, ExecutionResult.NEEDS_VERIFICATION.value):
                return existing
            if existing.result != ExecutionResult.ESCALATED.value:
                return existing  # BLOCKED (REJECTED/EXPIRED/CANCELLED) — permanece definitivo
            approval = self.escalation_desk.get(existing.approval_used)
            return self._act_on_approval(
                approval, mission_id=mission_id, task_id=task_id, platform=platform,
                account_context=account_context, action=action, payload=payload, executor_name=executor_name,
                key=key, fingerprint=fingerprint, tools_allowed=tools_allowed, existing_log_entry=existing,
                attempt_id=attempt_id, execution_id=execution_id,
            )

        if platform not in self._known_platforms:
            entry = ExecutionLogEntry(
                entry_id=ExecutionLogEntry.new_id(), mission_id=mission_id, task_id=task_id, platform=platform,
                account_context=account_context, task_description=f"{action.value} em {platform}",
                action=action.value, result=ExecutionResult.FAILED.value,
                error=f"plataforma desconhecida: {platform}", executor=executor_name,
                idempotency_key=key, fingerprint=fingerprint, execution_id=execution_id, attempt_id=attempt_id,
            )
            return self.log.record(entry)

        approval = self.escalation_desk.find_by_idempotency_key(key)
        if approval is None:
            approval = self.escalation_desk.escalate(
                reason=f"NAVIGATOR solicita ação crítica '{action.value}' em {platform}",
                context={
                    "mission_id": mission_id, "task_id": task_id, "action": action.value, "platform": platform,
                    "account_context": account_context or "", "channel_id": None, "payload": payload,
                },
                actor=self.principal.subject, operation=f"navigator_{action.value}", risk="HIGH",
                idempotency_key=key,
            )
        return self._act_on_approval(
            approval, mission_id=mission_id, task_id=task_id, platform=platform,
            account_context=account_context, action=action, payload=payload, executor_name=executor_name,
            key=key, fingerprint=fingerprint, tools_allowed=tools_allowed, existing_log_entry=None,
            attempt_id=attempt_id, execution_id=execution_id,
        )

    def _act_on_approval(
        self, approval, *, mission_id, task_id, platform, account_context, action, payload, executor_name,
        key, fingerprint, tools_allowed, existing_log_entry, attempt_id, execution_id,
    ) -> ExecutionLogEntry:
        def persist(result: str, evidence: dict, error: str | None) -> ExecutionLogEntry:
            entry_id = existing_log_entry.entry_id if existing_log_entry is not None else ExecutionLogEntry.new_id()
            entry = ExecutionLogEntry(
                entry_id=entry_id, mission_id=mission_id, task_id=task_id, platform=platform,
                account_context=account_context, task_description=f"{action.value} em {platform}",
                action=action.value, result=result, evidence=evidence, error=error,
                approval_used=approval.escalation_id, executor=executor_name,
                idempotency_key=key, fingerprint=fingerprint, execution_id=execution_id, attempt_id=attempt_id,
            )
            if existing_log_entry is not None:
                return self.log.update(entry)
            return self.log.record(entry)

        if approval.status == ApprovalStatus.APPROVED:
            # NUNCA reexecuta uma ação já consumida — SUCCESS e
            # NEEDS_VERIFICATION são ambos finais (resultado incerto
            # nunca é repetido automaticamente).
            already = self.log.by_approval(approval.escalation_id)
            final_hit = next(
                (e for e in already if e.result in (ExecutionResult.SUCCESS.value, ExecutionResult.NEEDS_VERIFICATION.value)),
                None,
            )
            if final_hit is not None:
                return final_hit

            allowed_mechanisms = self._parse_allowed_mechanisms(tools_allowed)
            path = self.bridge.best_available_execution_path(platform=platform, allowed_mechanisms=allowed_mechanisms)
            if path is None:
                all_viable = [c.connector_type.value for c in self.bridge.available_connectors(platform=platform) if c.is_viable()]
                entry = persist(
                    ExecutionResult.BLOCKED.value,
                    {"requested_tools": tools_allowed or [], "allowed_tools": tools_allowed or [], "selected_tool": None, "rejected_tools": all_viable},
                    "nenhum caminho de execução disponível dentro de tools_allowed",
                )
                self.audit.record(
                    actor=self.principal.subject, action="navigator_no_execution_path", resource=platform,
                    outcome="ERROR", details={"action": action.value},
                )
                return entry

            mechanism = path.connector_type
            executor = self._get_mechanism_executor(platform, mechanism)
            try:
                from core.operations import kill_switch as _ks  # v1.0.1: KILL SWITCH
                if _ks.block_reason(self.db_path if hasattr(self, 'db_path') else None):
                    return persist(ExecutionResult.BLOCKED.value, {'kill_switch': _ks.BLOCK_CODE}, _ks.block_reason(self.db_path if hasattr(self, 'db_path') else None))
                result = executor.execute(action=action, payload=payload)
            except Exception as exc:
                # CORREÇÃO (item 1, teste adversarial 3): exceção
                # DURANTE a consumação de uma ação crítica é um
                # resultado INCERTO — não sabemos se o efeito externo
                # ocorreu. NUNCA tratado como FAILED comum (que
                # sugeriria "seguro tentar de novo") — vira
                # NEEDS_VERIFICATION, definitivo, nunca repetido
                # automaticamente.
                entry = persist(
                    ExecutionResult.NEEDS_VERIFICATION.value,
                    {"selected_mechanism": mechanism.value, "reason": "exceção durante execução real — resultado incerto"},
                    str(exc),
                )
                self.audit.record(
                    actor=self.principal.subject, action="navigator_needs_verification", resource=platform,
                    outcome="ERROR", details={"approval_id": approval.escalation_id, "action": action.value, "error": str(exc)},
                )
                return entry

            if mechanism != ConnectorType.MOCK_FALLBACK and result.mode == AdapterMode.MOCK:
                entry = persist(
                    ExecutionResult.FAILED.value,
                    {"selected_mechanism": mechanism.value, "actual_executor": type(executor).__name__, "mode": result.mode.value},
                    f"inconsistência: mecanismo selecionado '{mechanism.value}' mas executor respondeu em modo MOCK",
                )
                self.audit.record(
                    actor=self.principal.subject, action="navigator_mechanism_mismatch", resource=platform,
                    outcome="ERROR", details={"selected_mechanism": mechanism.value},
                )
                return entry

            final = ExecutionResult.SUCCESS.value if result.success else ExecutionResult.FAILED.value
            evidence = {
                "selected_mechanism": mechanism.value, "actual_executor": type(executor).__name__,
                "mode": result.mode.value, "platform": platform, "configured": path.is_configured,
                "authenticated": path.authenticated, "permissions": path.permissions_ok,
                "requested_tools": tools_allowed or [], "selected_tool": mechanism.value, **result.data,
            }
            entry = persist(final, evidence, result.error)
            self.audit.record(
                actor=self.principal.subject, action="navigator_consume_approval", resource=platform,
                outcome="SUCCESS", details={"approval_id": approval.escalation_id, "action": action.value, "result": final},
            )
            return entry

        if approval.status == ApprovalStatus.PENDING_OWNER_APPROVAL:
            if existing_log_entry is not None:
                return existing_log_entry
            return persist(ExecutionResult.ESCALATED.value, {}, None)

        # REJECTED / EXPIRED / CANCELLED — NUNCA autoriza execução.
        return persist(
            ExecutionResult.BLOCKED.value, {},
            f"aprovação com status '{approval.status.value}' nunca autoriza execução",
        )

    # ------------------------------------------------------------------
    # Ações não críticas
    # ------------------------------------------------------------------

    def _execute_via_mechanism(
        self, *, mission_id, task_id, platform, account_context, action, payload, executor_name, key,
        fingerprint, path, tools_allowed, execution_id, attempt_id,
    ) -> ExecutionLogEntry:
        mechanism = path.connector_type
        executor = self._get_mechanism_executor(platform, mechanism)
        retry_count = 0
        last_error: str | None = None
        while retry_count <= self.max_retries:
            try:
                from core.operations import kill_switch as _ks  # v1.0.1: KILL SWITCH
                if _ks.block_reason(self.db_path if hasattr(self, 'db_path') else None):
                    return persist(ExecutionResult.BLOCKED.value, {'kill_switch': _ks.BLOCK_CODE}, _ks.block_reason(self.db_path if hasattr(self, 'db_path') else None))
                result = executor.execute(action=action, payload=payload)
            except Exception as exc:
                last_error = str(exc)
                retry_count += 1
                continue

            if mechanism != ConnectorType.MOCK_FALLBACK and result.mode == AdapterMode.MOCK:
                entry = ExecutionLogEntry(
                    entry_id=ExecutionLogEntry.new_id(), mission_id=mission_id, task_id=task_id, platform=platform,
                    account_context=account_context, task_description=f"{action.value} em {platform}",
                    action=action.value, result=ExecutionResult.FAILED.value,
                    error=f"inconsistência: mecanismo selecionado '{mechanism.value}' mas executor respondeu em modo MOCK",
                    evidence={"selected_mechanism": mechanism.value, "actual_executor": type(executor).__name__, "mode": result.mode.value},
                    retry_count=retry_count, executor=executor_name, idempotency_key=key, fingerprint=fingerprint,
                    execution_id=execution_id, attempt_id=attempt_id,
                )
                self.audit.record(
                    actor=self.principal.subject, action="navigator_mechanism_mismatch", resource=platform,
                    outcome="ERROR", details={"selected_mechanism": mechanism.value},
                )
                return self.log.record(entry)

            evidence = {
                "selected_mechanism": mechanism.value, "actual_executor": type(executor).__name__,
                "mode": result.mode.value, "platform": platform, "configured": path.is_configured,
                "authenticated": path.authenticated, "permissions": path.permissions_ok,
                "requested_tools": tools_allowed or [], "selected_tool": mechanism.value, **result.data,
            }
            entry = ExecutionLogEntry(
                entry_id=ExecutionLogEntry.new_id(), mission_id=mission_id, task_id=task_id, platform=platform,
                account_context=account_context, task_description=f"{action.value} em {platform}",
                action=action.value,
                result=ExecutionResult.SUCCESS.value if result.success else ExecutionResult.FAILED.value,
                evidence=evidence, error=result.error,
                retry_count=retry_count, executor=executor_name, idempotency_key=key, fingerprint=fingerprint,
                execution_id=execution_id, attempt_id=attempt_id,
            )
            self.audit.record(
                actor=self.principal.subject, action="navigator_execute", resource=platform,
                outcome="SUCCESS", details={"action": action.value, "result": entry.result},
            )
            return self.log.record(entry)

        entry = ExecutionLogEntry(
            entry_id=ExecutionLogEntry.new_id(), mission_id=mission_id, task_id=task_id, platform=platform,
            account_context=account_context, task_description=f"{action.value} em {platform}",
            action=action.value, result=ExecutionResult.FAILED.value, error=last_error,
            evidence={"selected_mechanism": mechanism.value},
            retry_count=retry_count, executor=executor_name, idempotency_key=key, fingerprint=fingerprint,
            execution_id=execution_id, attempt_id=attempt_id,
        )
        self.audit.record(
            actor=self.principal.subject, action="navigator_execute", resource=platform,
            outcome="ERROR", details={"action": action.value, "error": last_error},
        )
        return self.log.record(entry)
