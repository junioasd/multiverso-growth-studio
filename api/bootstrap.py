"""
api.bootstrap
==============
Monta a aplicação MULTIVERSO GROWTH STUDIO wireando os módulos
centrais. Ponto único de composição usado por testes, CLI e servidor.

MICROCORREÇÃO 2 (auditoria):
- PERSISTÊNCIA: todos os serviços mutáveis (CRM, SalesTracker,
  FinanceLedger, JobQueue, AuditTrail) agora são inicializados com
  `db_path=SETTINGS.db_path` por padrão — o app real sempre persiste em
  SQLite, sobrevivendo a reinício. Testes unitários leves continuam
  instanciando os serviços diretamente sem `db_path` (modo em memória),
  o que não passa por aqui.
- ACESSO OWNER: a inicialização padrão agora lê `OWNER_EMAIL`/
  `OWNER_PASSWORD_HASH` de `SETTINGS` (variáveis de ambiente
  documentadas em `.env.example`) quando os parâmetros não são passados
  explicitamente — antes, `build_app()` sempre caía em um e-mail de
  exemplo sem senha, tornando login impossível mesmo com `.env` real
  configurado.

ATLAS V1:
- `GrowthStudioApp.atlas` expõe o `atlas.director.AtlasDirector` já
  wireado com a MESMA `GovernedFacade` do resto do app. O Principal do
  ATLAS (`role=Role.ATLAS`, escopo coringa "*") é fixado em código —
  ATLAS nunca escolhe seu próprio escopo ou papel. Ver `atlas/director.py`
  para a hierarquia e os limites de autoridade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atlas.director import AtlasDirector
from commercial.multigerenciador import MultiGerenciadorComercial
from core.config import SETTINGS
from core.governance.facade import GovernedFacade
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.production.pipeline import ProductionPipeline
from core.security.audit import AuditTrail
from core.security.auth import OwnerAuthService, create_canonical_auth_service
from core.security.rbac import Principal, Role
from commercial.omnichannel_intelligence import OmnichannelIntelligence
from youtube_strategy.alignment_checker import YouTubeAlignmentChecker
from youtube_strategy.change_radar import YouTubeChangeRadar
from youtube_strategy.distribution_engine import DistributionPlanner
from youtube_strategy.strategy_engine import YouTubeStrategyEngine
from atlas.gateway_worker import AtlasGatewayWorker


@dataclass
class GrowthStudioApp:
    auth: OwnerAuthService
    audit: AuditTrail
    facade: GovernedFacade
    job_queue: JobQueue
    production_pipeline: ProductionPipeline
    multigerenciador: MultiGerenciadorComercial
    atlas: AtlasDirector
    youtube_strategy: YouTubeStrategyEngine
    youtube_alignment: YouTubeAlignmentChecker
    youtube_change_radar: YouTubeChangeRadar
    youtube_distribution: DistributionPlanner
    omnichannel_intelligence: OmnichannelIntelligence
    db_path: str | None = None
    channels: dict = field(default_factory=dict)
    projects: dict = field(default_factory=dict)
    # CORREÇÃO (item 5 — ApplicationGovernanceContext integrado ao
    # runtime): `None` até `bootstrap_governance_context()` ser
    # chamado explicitamente com credenciais reais do Owner — nunca
    # bootstrapado automaticamente (build_app só recebe o HASH da
    # senha, nunca a senha em texto puro, então não pode logar
    # sozinho).
    governance_context: object = None

    def bootstrap_governance_context(self, *, email: str, password: str, bootstrap_reason: str):
        """CORREÇÃO (itens 1/5/9 — round 9): ÚNICO caminho para montar
        `self.governance_context` — exige login REAL (e-mail+senha)
        contra `self.auth` (o `OwnerAuthService` CANÔNICO desta app,
        criado por `create_canonical_auth_service()`). Levanta
        `AuthError` se as credenciais forem inválidas, ou
        `GovernanceContextError` se a autoridade raiz já tiver sido
        bootstrapada nesta app (`ALREADY_BOOTSTRAPPED`)."""
        from core.evidence.governance_context import ApplicationGovernanceContext

        if self.governance_context is not None:
            from core.evidence.governance_context import GovernanceContextError

            raise GovernanceContextError(
                "ALREADY_BOOTSTRAPPED: esta aplicação já possui um governance_context — bootstrap não pode "
                "ser repetido",
                error_code="ALREADY_BOOTSTRAPPED",
            )
        session_token = self.auth.login(email, password)
        self.governance_context = ApplicationGovernanceContext.bootstrap_for_app(
            auth_service=self.auth, session_token=session_token, bootstrap_reason=bootstrap_reason,
            db_path=self.db_path,
        )
        return self.governance_context

    def restore_governance_context(self, *, email: str, password: str):
        """CORREÇÃO (round 9 — continuidade real após restart): caminho
        dedicado para RESTAURAR `self.governance_context` a partir de
        estado já persistido em `self.db_path` (quando a app é
        reconstruída após um restart real do processo — ex: um novo
        `build_app()` apontando para o mesmo banco). Exige a MESMA
        disciplina de autenticação de `bootstrap_governance_context()`
        — restaurar após restart nunca é um bypass silencioso, o Owner
        precisa se autenticar de novo. `NOT_BOOTSTRAPPED` quando não há
        nada persistido (nesse caso, use `bootstrap_governance_context()`
        para o primeiro bootstrap)."""
        from core.evidence.governance_context import ApplicationGovernanceContext, GovernanceContextError

        if self.governance_context is not None:
            raise GovernanceContextError(
                "ALREADY_BOOTSTRAPPED: esta aplicação já possui um governance_context em memória",
                error_code="ALREADY_BOOTSTRAPPED",
            )
        session_token = self.auth.login(email, password)
        self.governance_context = ApplicationGovernanceContext.restore_for_app(
            auth_service=self.auth, session_token=session_token, db_path=self.db_path,
        )
        return self.governance_context

    def shutdown(self, *, timeout: float = 10.0) -> None:
        """Encerra de forma limpa qualquer worker de fundo iniciado —
        `atlas.radar_worker` (RADAR DE OPORTUNIDADES) e
        `atlas.radar_web_worker` (RADAR WEB COMERCIAL, correção item
        3). Idempotente e seguro de chamar mesmo se nada foi iniciado.
        Sempre chamar no shutdown do runtime (ver `main.py cmd_serve`)
        para nunca deixar thread órfã.

        MICROCORREÇÃO (shutdown residual — falha real reproduzida por
        auditoria independente): antes, se `stop_radar_worker()`
        levantasse (thread genuinamente presa além do timeout), o
        `stop_radar_web_worker()` seguinte NUNCA seria tentado — o
        segundo worker ficaria órfão mesmo que ele próprio tivesse
        conseguido parar a tempo. Agora AMBOS os `stop()` são sempre
        tentados, independentemente de o primeiro ter levantado —
        nunca deixa o segundo worker sem sequer uma tentativa de
        parada por causa de uma falha no primeiro. Se qualquer um (ou
        os dois) exceder o timeout, a falha é relevantada ao final —
        nunca escondida, nunca um falso sucesso silencioso."""
        from atlas.radar.worker import WorkerShutdownTimeout as _AtlasWorkerShutdownTimeout
        from radar_web.worker import WorkerShutdownTimeout as _WebWorkerShutdownTimeout

        errors: list = []
        atlas_gateway_worker = getattr(
            self,
            "atlas_gateway_worker",
            None,
        )

        if atlas_gateway_worker is not None:
            try:
                atlas_gateway_worker.stop(timeout=timeout)
            except Exception as exc:
                errors.append(exc)


        try:
            self.atlas.stop_radar_worker(timeout=timeout)
        except _AtlasWorkerShutdownTimeout as exc:
            errors.append(exc)
        try:
            self.atlas.stop_radar_web_worker(timeout=timeout)
        except _WebWorkerShutdownTimeout as exc:
            errors.append(exc)
        if errors:
            raise errors[0] if len(errors) == 1 else ExceptionGroup(
                "shutdown incompleto — um ou mais workers não encerraram a tempo", errors,
            )


def build_app(
    *,
    owner_email: str | None = None,
    owner_password_hash: str | None = None,
    init_database: bool = True,
    persistent: bool = True,
    db_path: str | None = None,
    start_radar_worker: bool = False,
    start_radar_web_worker: bool = False,
) -> GrowthStudioApp:
    """Monta a aplicação.

    `persistent=True` (padrão) conecta CRM/Sales/Finance/Jobs/Audit a
    SQLite (`db_path` ou `SETTINGS.db_path`) — estado sobrevive a
    reinício. `persistent=False` mantém tudo em memória (útil para
    testes que não querem tocar disco).

    `start_radar_worker=False` (padrão) — construir o app NUNCA inicia
    threads de fundo sozinho (evita surpresas em testes/scripts que só
    querem montar o app). O runtime que efetivamente SERVE a aplicação
    (`main.py cmd_serve`) passa `start_radar_worker=True` explicitamente
    — aí sim o worker do RADAR DE OPORTUNIDADES
    (`atlas.radar.worker.RadarWorker`) começa a rodar automaticamente
    junto com o processo, sem exigir nenhuma chamada manual a
    `run_scheduled_radar_scan()`.

    CORREÇÃO (item 1 — RADAR WEB COMERCIAL: autostart real):
    `start_radar_web_worker=False` (padrão) — parâmetro SEPARADO e
    INDEPENDENTE do `start_radar_worker` acima. Quando `True`, inicia
    `atlas.radar_web_worker` (`radar_web.worker.RadarWebWorker`) —
    scheduler/worker PRÓPRIOS do RADAR WEB COMERCIAL, nunca confundidos
    com o RADAR DE OPORTUNIDADES. O agendamento (`enabled`/
    `interval_seconds`) precisa ser configurado separadamente via
    `atlas.radar_web_scheduler.enable(...)` — este parâmetro só decide
    se a THREAD do worker roda; o worker em si só executa um ciclo
    quando o scheduler estiver habilitado (`run_if_due()` é um no-op
    enquanto `enabled=False`).

    Use `GrowthStudioApp.shutdown()` para encerrar ambos os workers de
    forma limpa.
    """
    resolved_db_path = db_path or SETTINGS.db_path
    effective_db_path = resolved_db_path if persistent else None

    if init_database and persistent:
        db_module.init_db(resolved_db_path)

    # CORREÇÃO (item 1/2/3/5 — round 9: OwnerAuthService canônico do
    # runtime): criado pela fábrica canônica — é isto que
    # `ApplicationGovernanceContext.bootstrap_for_app()` exige para
    # aceitar como fonte de confiança. Nenhum `OwnerAuthService`
    # criado fora daqui (ex: por um chamador externo) é aceito.
    auth = create_canonical_auth_service(
        owner_email=owner_email or SETTINGS.owner_email,
        stored_password_hash=owner_password_hash or (SETTINGS.owner_password_hash or None),
        db_path=effective_db_path,
    )
    audit = AuditTrail(db_path=effective_db_path)
    job_queue = JobQueue(db_path=effective_db_path)
    multigerenciador = MultiGerenciadorComercial(db_path=effective_db_path)
    facade = GovernedFacade(job_queue=job_queue, multigerenciador=multigerenciador, audit=audit)

    # ATLAS — Diretor Operacional. Escopo coringa "*" para poder
    # coordenar qualquer canal do Growth Studio (Seção "ATLAS supervisiona
    # as demais IAs/agentes/motores"), mas SEM VIEW_GLOBAL_DASHBOARD nem
    # MANAGE_GLOBAL_OPERATIONS — o agregado GLOBAL do painel e operações
    # sem canal continuam exclusivos do Owner (ver core/security/rbac.py).
    atlas_principal = Principal(subject="atlas@system", role=Role.ATLAS, scopes={"*"})
    atlas = AtlasDirector(facade=facade, principal=atlas_principal, db_path=effective_db_path)

    if start_radar_worker:
        atlas.start_radar_worker()
    if start_radar_web_worker:
        atlas.start_radar_web_worker()

    app = GrowthStudioApp(
        auth=auth,
        audit=audit,
        facade=facade,
        job_queue=job_queue,
        production_pipeline=ProductionPipeline(),
        multigerenciador=multigerenciador,
        atlas=atlas,
        youtube_strategy=YouTubeStrategyEngine(),
        youtube_alignment=YouTubeAlignmentChecker(),
        youtube_change_radar=YouTubeChangeRadar(),
        # BLOCO FINAL 1 (item 4): compartilha o MESMO Learning Loop
        # quantitativo do ATLAS (`atlas.quantitative_learning_loop`) —
        # aprendizados registrados por ECHO/ARGOS ficam visíveis aqui
        # também (mesmo canal), nunca um sistema de aprendizado
        # paralelo desconectado.
        youtube_distribution=DistributionPlanner(learning_loop=atlas.quantitative_learning_loop),
        # BLOCO FINAL 1 (item 5): compartilha o MESMO SalesTracker
        # (`facade.sales_tracker`) e o MESMO Learning Loop quantitativo
        # do ATLAS — nunca um sistema de medição/aprendizado paralelo.
        omnichannel_intelligence=OmnichannelIntelligence(
            sales_tracker=facade.sales_tracker, learning_loop=atlas.quantitative_learning_loop,
        ),
        db_path=effective_db_path,
    )

    app.atlas_gateway_worker = AtlasGatewayWorker(
        job_queue=job_queue,
        atlas=atlas,
    )

    return app
