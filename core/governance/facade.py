"""
core.governance.facade
=========================
MICROCORREÇÃO 2 (auditoria externa — reprovado na primeira rodada).

Correções aplicadas nesta versão:

1. GOVERNANÇA: operações "globais" (sem channel_id/project_id) agora
   exigem, ALÉM da Permission de domínio (MANAGE_JOBS/MANAGE_FINANCE),
   a Permission explícita `MANAGE_GLOBAL_OPERATIONS` — que, por padrão,
   só o Owner possui. Isso corrige o buraco em que qualquer principal
   com MANAGE_FINANCE/MANAGE_JOBS podia executar uma mutação "global"
   sem nenhuma checagem adicional.
2. AUDITORIA: o outcome SUCCESS só é gravado DEPOIS que a operação de
   domínio (delegada ao serviço interno) retorna com sucesso. Validações
   de negócio que falham DEPOIS da autorização (ex: `IdempotencyConflict`,
   `InvalidLeadTransition`, `InvalidEventType`) agora geram um evento de
   auditoria com outcome=ERROR — antes, a fachada registrava SUCCESS
   antes mesmo de chamar o serviço interno, o que era estruturalmente
   incorreto (podia haver SUCCESS auditado para uma operação que na
   verdade falhou).
3. IDENTIDADE VERIFICADA: `Principal` só deve chegar aqui reconstruído
   por `core.security.auth.principal_from_session()` a partir de uma
   sessão HMAC verificada — nunca de um objeto montado livremente a
   partir de dados de requisição não autenticados. A fachada não
   consegue impedir um chamador Python de construir um `Principal`
   arbitrário localmente (isso é uma limitação de linguagem, não algo
   que uma fachada resolve sozinha), mas a camada HTTP
   (`api/server.py`) só constrói `Principal` a partir de sessão
   verificada, nunca de headers/campos soltos do request.

`GovernedFacade` continua sendo o ÚNICO ponto de entrada recomendado
para mutações de CRM/Sales/Finance/Jobs/Learning.
"""

from __future__ import annotations

from typing import Any, Callable

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.multigerenciador import MultiGerenciadorComercial
from commercial.sales_tracking import SalesTracker
from core.governance.replay_guard import ReplayGuard, ReplayRejected
from core.isolation.isolation import IsolationGuard, IsolationViolation, ResourceScope
from core.jobs.queue import JobQueue
from core.models.entities import Job, LeadStatus, SalesEvent, ValueType
from core.security.audit import AuditTrail
from core.security.rbac import AccessDenied, Permission, Principal, require_permission
from core.security.sanitize import sanitize_dict


class GovernanceDenied(Exception):
    """Classe base para todas as recusas aplicadas pela fachada governada."""


class InvalidIdentity(GovernanceDenied):
    pass


class InvalidBinding(GovernanceDenied):
    pass


_PLACEHOLDER_BINDING_VALUES = ("", "UNKNOWN")  # valor explícito mas inválido


class GovernedFacade:
    def __init__(
        self,
        *,
        job_queue: JobQueue,
        multigerenciador: MultiGerenciadorComercial,
        audit: AuditTrail,
        replay_ttl_seconds: float = 300.0,
    ) -> None:
        self.job_queue = job_queue
        self.mgc = multigerenciador
        self.crm: CRM = multigerenciador.crm
        self.sales_tracker: SalesTracker = multigerenciador.sales_tracker
        self.finance: FinanceLedger = multigerenciador.finance
        self.audit = audit
        self.replay_guard = ReplayGuard(replay_ttl_seconds)

    # ------------------------------------------------------------------
    # Núcleo: autoriza, executa, audita — nesta ordem, sempre
    # ------------------------------------------------------------------

    def _execute(
        self,
        principal: Principal | None,
        permission: Permission,
        scope: ResourceScope,
        *,
        action: str,
        resource: str,
        fn: Callable[[], Any],
        require_channel: bool = False,
        require_project: bool = False,
        is_global: bool = False,
        operation_id: str | None = None,
    ) -> Any:
        # --- 1. Identidade ---
        if principal is None:
            self.audit.record(
                actor="unknown", action=action, resource=resource, outcome="DENIED",
                details={"reason": "invalid_identity"},
            )
            raise InvalidIdentity("identidade ausente/inválida — operação recusada (fail-closed)")

        # --- 2. Binding de canal/projeto ---
        # Um valor EXPLÍCITO mas inválido ("" ou "UNKNOWN") é sempre
        # recusado, independentemente de o campo ser obrigatório ou não
        # para esta operação — não existe "UNKNOWN" legítimo para
        # channel_id/project_id vindo de um chamador (None é diferente:
        # significa "operação intencionalmente sem esse escopo").
        if scope.channel_id in _PLACEHOLDER_BINDING_VALUES:
            self.audit.record(
                actor=principal.subject, action=action, resource=resource, outcome="DENIED",
                details={"reason": "invalid_channel_binding"},
            )
            raise InvalidBinding("channel_id inválido/UNKNOWN — operação recusada (fail-closed)")

        if scope.project_id in _PLACEHOLDER_BINDING_VALUES:
            self.audit.record(
                actor=principal.subject, action=action, resource=resource, outcome="DENIED",
                details={"reason": "invalid_project_binding"},
            )
            raise InvalidBinding("project_id inválido/UNKNOWN — operação recusada (fail-closed)")

        if require_channel and scope.channel_id is None:
            self.audit.record(
                actor=principal.subject, action=action, resource=resource, outcome="DENIED",
                details={"reason": "missing_channel_binding"},
            )
            raise InvalidBinding("channel_id ausente — obrigatório para esta operação (fail-closed)")

        if require_project and scope.project_id is None:
            self.audit.record(
                actor=principal.subject, action=action, resource=resource, outcome="DENIED",
                details={"reason": "missing_project_binding"},
            )
            raise InvalidBinding("project_id ausente — obrigatório para esta operação (fail-closed)")

        # --- 3. RBAC (domínio) + isolamento ---
        try:
            require_permission(principal, permission)
            IsolationGuard.check(principal.scopes, scope)
        except (AccessDenied, IsolationViolation) as exc:
            self.audit.record(
                actor=principal.subject, action=action, resource=resource, outcome="DENIED",
                details=sanitize_dict({"reason": str(exc)}),
            )
            raise

        # --- 4. RBAC (operações globais exigem permissão explícita adicional) ---
        if is_global:
            try:
                require_permission(principal, Permission.MANAGE_GLOBAL_OPERATIONS)
            except AccessDenied as exc:
                self.audit.record(
                    actor=principal.subject, action=action, resource=resource, outcome="DENIED",
                    details=sanitize_dict({"reason": f"operação global sem permissão explícita: {exc}"}),
                )
                raise

        # --- 5. Proteção de replay (opcional) ---
        try:
            self.replay_guard.check_and_record(operation_id)
        except ReplayRejected as exc:
            self.audit.record(
                actor=principal.subject, action=action, resource=resource, outcome="DENIED",
                details=sanitize_dict({"reason": str(exc)}),
            )
            raise

        # --- 6. Execução real — SUCCESS só é auditado se isto retornar sem erro ---
        try:
            result = fn()
        except Exception as exc:
            self.audit.record(
                actor=principal.subject, action=action, resource=resource, outcome="ERROR",
                details=sanitize_dict({"reason": str(exc), "error_type": type(exc).__name__}),
            )
            raise

        self.audit.record(actor=principal.subject, action=action, resource=resource, outcome="SUCCESS", details={})
        return result

    # ------------------------------------------------------------------
    # CRM
    # ------------------------------------------------------------------

    def create_lead(
        self,
        principal: Principal | None,
        *,
        channel_id: str,
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        **lead_kwargs,
    ):
        scope = ResourceScope(channel_id=channel_id)
        return self._execute(
            principal, Permission.MANAGE_CRM, scope,
            action="create_lead", resource=f"channel:{channel_id}", require_channel=True,
            operation_id=operation_id,
            fn=lambda: self.crm.create_lead(channel_id, idempotency_key=idempotency_key, **lead_kwargs),
        )

    def transition_lead(
        self,
        principal: Principal | None,
        *,
        channel_id: str,
        lead_id: str,
        new_status: LeadStatus,
        operation_id: str | None = None,
    ):
        scope = ResourceScope(channel_id=channel_id)

        def _do():
            # Defesa em profundidade: mesmo autorizado para o canal
            # alegado, o lead de fato precisa pertencer a esse canal.
            lead = self.crm.get(lead_id)
            IsolationGuard.assert_same_channel(lead.channel_id, channel_id)
            return self.crm.transition(lead_id, new_status)

        return self._execute(
            principal, Permission.MANAGE_CRM, scope,
            action="transition_lead", resource=f"lead:{lead_id}", require_channel=True,
            operation_id=operation_id, fn=_do,
        )

    # ------------------------------------------------------------------
    # Sales
    # ------------------------------------------------------------------

    def record_sales_event(
        self,
        principal: Principal | None,
        *,
        channel_id: str,
        event_type: str,
        value_type: ValueType = ValueType.UNKNOWN,
        amount: float | None = None,
        lead_id: str | None = None,
        offer_id: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        funnel_id: str | None = None,
        platform: str | None = None,
        traffic_source: str | None = None,
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> SalesEvent:
        scope = ResourceScope(channel_id=channel_id)
        return self._execute(
            principal, Permission.MANAGE_SALES, scope,
            action="record_sales_event", resource=f"channel:{channel_id}", require_channel=True,
            operation_id=operation_id,
            fn=lambda: self.sales_tracker.record_event(
                channel_id=channel_id, event_type=event_type, value_type=value_type, amount=amount,
                lead_id=lead_id, offer_id=offer_id, product_id=product_id, campaign_id=campaign_id,
                funnel_id=funnel_id, platform=platform, traffic_source=traffic_source,
                idempotency_key=idempotency_key,
            ),
        )

    # ------------------------------------------------------------------
    # Finance
    # ------------------------------------------------------------------

    def record_finance_entry(
        self,
        principal: Principal | None,
        *,
        entry_type: str,
        amount: float,
        channel_id: str | None = None,
        description: str = "",
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ):
        scope = ResourceScope(channel_id=channel_id)
        is_global = channel_id is None
        return self._execute(
            principal, Permission.MANAGE_FINANCE, scope,
            action="record_finance_entry", resource=f"channel:{channel_id or 'GLOBAL'}",
            require_channel=False, is_global=is_global, operation_id=operation_id,
            fn=lambda: self.finance.record(
                entry_type=entry_type, amount=amount, description=description,
                channel_id=channel_id, idempotency_key=idempotency_key,
            ),
        )

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def enqueue_job(
        self,
        principal: Principal | None,
        *,
        job_type: str,
        idempotency_key: str,
        project_id: str | None = None,
        channel_id: str | None = None,
        payload: dict | None = None,
        max_attempts: int = 3,
        operation_id: str | None = None,
    ) -> Job:
        scope = ResourceScope(channel_id=channel_id, project_id=project_id)
        # MICROCORREÇÃO 2: um job sem NENHUM escopo (nem canal, nem
        # projeto) é uma operação global e exige MANAGE_GLOBAL_OPERATIONS
        # explícita — não basta ter MANAGE_JOBS.
        is_global = channel_id is None and project_id is None
        return self._execute(
            principal, Permission.MANAGE_JOBS, scope,
            action="enqueue_job", resource=f"job_type:{job_type}",
            require_channel=False, require_project=False, is_global=is_global,
            operation_id=operation_id,
            fn=lambda: self.job_queue.enqueue(
                job_type=job_type, idempotency_key=idempotency_key, payload=payload,
                project_id=project_id, channel_id=channel_id, max_attempts=max_attempts,
            ),
        )

    def _job_scope(self, job_id: str) -> tuple[Job, ResourceScope, bool]:
        job = self.job_queue.get(job_id)
        scope = ResourceScope(channel_id=job.channel_id, project_id=job.project_id)
        is_global = job.channel_id is None and job.project_id is None
        return job, scope, is_global

    def start_job(self, principal: Principal | None, *, job_id: str, operation_id: str | None = None) -> Job:
        _, scope, is_global = self._job_scope(job_id)
        return self._execute(
            principal, Permission.MANAGE_JOBS, scope, action="start_job", resource=f"job:{job_id}",
            is_global=is_global, operation_id=operation_id, fn=lambda: self.job_queue.start(job_id),
        )

    def complete_job(
        self, principal: Principal | None, *, job_id: str, result: dict, operation_id: str | None = None
    ) -> Job:
        _, scope, is_global = self._job_scope(job_id)
        return self._execute(
            principal, Permission.MANAGE_JOBS, scope, action="complete_job", resource=f"job:{job_id}",
            is_global=is_global, operation_id=operation_id,
            fn=lambda: self.job_queue.complete(job_id, result),
        )

    def fail_job(
        self, principal: Principal | None, *, job_id: str, error: str, operation_id: str | None = None
    ) -> Job:
        _, scope, is_global = self._job_scope(job_id)
        return self._execute(
            principal, Permission.MANAGE_JOBS, scope, action="fail_job", resource=f"job:{job_id}",
            is_global=is_global, operation_id=operation_id,
            fn=lambda: self.job_queue.fail(job_id, error),
        )

    def cancel_job(self, principal: Principal | None, *, job_id: str, operation_id: str | None = None) -> Job:
        _, scope, is_global = self._job_scope(job_id)
        return self._execute(
            principal, Permission.MANAGE_JOBS, scope, action="cancel_job", resource=f"job:{job_id}",
            is_global=is_global, operation_id=operation_id, fn=lambda: self.job_queue.cancel(job_id),
        )

    # ------------------------------------------------------------------
    # Decisões de escala (leitura sensível — revalida sempre a partir de
    # dados persistidos no momento da chamada, nunca de cache)
    # ------------------------------------------------------------------

    def evaluate_scaling_readiness(self, principal: Principal | None, *, channel_id: str) -> dict:
        scope = ResourceScope(channel_id=channel_id)
        return self._execute(
            principal, Permission.VIEW_DASHBOARD, scope,
            action="evaluate_scaling_readiness", resource=f"channel:{channel_id}", require_channel=True,
            fn=lambda: self.mgc.evaluate_scaling_readiness(channel_id),
        )

    # ------------------------------------------------------------------
    # Painel (MICROCORREÇÃO 3, item 1 — ISOLAMENTO E AUTORIZAÇÃO DO PAINEL)
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_readable_channels(principal: Principal) -> str | set[str]:
        """Retorna "ALL" (escopo coringa "*", só o Owner tem por padrão)
        ou o conjunto exato de canais que a sessão VERIFICADA autoriza.
        Nunca deriva isso de nada que o cliente declare na requisição —
        somente dos escopos gravados no token de sessão assinado."""
        if "*" in principal.scopes:
            return "ALL"
        return {s.split("channel:", 1)[1] for s in principal.scopes if s.startswith("channel:")}

    def get_dashboard_view(
        self, principal: Principal | None, *, requested_channel_id: str | None = None
    ) -> dict:
        """Monta os dados agregados do painel (CRM, vendas, finanças)
        estritamente dentro do escopo da identidade autenticada.

        - `requested_channel_id` informado: só é atendido se esse canal
          estiver dentro do escopo da sessão (senão, `IsolationViolation`).
        - Nenhum canal pedido explicitamente: agrega sobre os canais do
          PRÓPRIO escopo da sessão (nunca "tudo") — a menos que a sessão
          tenha escopo coringa ("*") E também possua
          `VIEW_GLOBAL_DASHBOARD` explicitamente, caso em que agrega
          globalmente.
        """
        if principal is None:
            self.audit.record(
                actor="unknown", action="view_dashboard", resource="dashboard", outcome="DENIED",
                details={"reason": "invalid_identity"},
            )
            raise InvalidIdentity("identidade ausente/inválida — operação recusada (fail-closed)")

        try:
            require_permission(principal, Permission.VIEW_DASHBOARD)
        except AccessDenied as exc:
            self.audit.record(
                actor=principal.subject, action="view_dashboard", resource="dashboard", outcome="DENIED",
                details=sanitize_dict({"reason": str(exc)}),
            )
            raise

        readable = self.resolve_readable_channels(principal)

        if requested_channel_id is not None:
            if readable != "ALL" and requested_channel_id not in readable:
                self.audit.record(
                    actor=principal.subject, action="view_dashboard", resource=f"channel:{requested_channel_id}",
                    outcome="DENIED", details={"reason": "channel_out_of_scope"},
                )
                raise IsolationViolation(
                    f"canal '{requested_channel_id}' fora do escopo da sessão autenticada"
                )
            channels_to_use: set[str] | None = {requested_channel_id}
            resource_label = f"channel:{requested_channel_id}"
        elif readable == "ALL":
            try:
                require_permission(principal, Permission.VIEW_GLOBAL_DASHBOARD)
            except AccessDenied as exc:
                self.audit.record(
                    actor=principal.subject, action="view_dashboard", resource="GLOBAL", outcome="DENIED",
                    details=sanitize_dict({"reason": f"agregado global sem autorização explícita: {exc}"}),
                )
                raise
            channels_to_use = None  # sentinela: agrega literalmente tudo
            resource_label = "GLOBAL"
        else:
            channels_to_use = set(readable)  # pode ser vazio, se a sessão não tem nenhum canal
            resource_label = ",".join(sorted(channels_to_use)) or "NONE"

        sales_summary = self.sales_tracker.summarize(channels_to_use)
        finance_snapshot = self.finance.snapshot(channels_to_use)
        total_leads = (
            len(self.crm.all_leads()) if channels_to_use is None else len(self.crm.by_channels(channels_to_use))
        )

        self.audit.record(
            actor=principal.subject, action="view_dashboard", resource=resource_label, outcome="SUCCESS", details={}
        )
        return {
            "sales_summary": sales_summary,
            "finance_snapshot": finance_snapshot,
            "total_leads": total_leads,
            "channels": channels_to_use,
        }

    # ------------------------------------------------------------------
    # Learning Loop
    # ------------------------------------------------------------------

    def record_learning(
        self,
        principal: Principal | None,
        *,
        scope_key: str,
        outcome: str,
        context: dict,
        limitations: str = "",
        operation_id: str | None = None,
    ):
        # scope_key não é necessariamente um channel_id puro (pode compor
        # canal+produto+formato); exige apenas identidade e permissão
        # válidas — não há binding de canal único a validar
        # estruturalmente. Diferente de finance/jobs, aprendizado é um
        # registro de baixo risco (não move dinheiro nem publica nada),
        # então MANAGE_LEARNING sozinha é suficiente — não exigimos
        # MANAGE_GLOBAL_OPERATIONS aqui (evitaria que o Operator, que
        # recebe MANAGE_LEARNING por padrão, nunca pudesse de fato usá-la).
        scope = ResourceScope()
        return self._execute(
            principal, Permission.MANAGE_LEARNING, scope,
            action="record_learning", resource=scope_key, is_global=False, operation_id=operation_id,
            fn=lambda: self.mgc.learning_loop.record(
                scope_key=scope_key, outcome=outcome, context=context, limitations=limitations
            ),
        )
