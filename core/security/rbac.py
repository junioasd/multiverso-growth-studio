"""
core.security.rbac
===================
RBAC (Role-Based Access Control) e Princípio de Menor Privilégio
(Seções 58 e 60 do Comando Mestre).

Nenhuma IA/agente é Owner. Competência != autonomia != autoridade (Seção 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    OWNER = "OWNER"
    # ATLAS — Diretor Operacional do Growth Studio (ver pacote `atlas/`).
    # Abaixo do Owner na hierarquia, acima de Operator/Agent — coordena a
    # operação diária mas NUNCA tem autoridade de Owner: sem
    # MANAGE_CREDENTIALS, MANAGE_GLOBAL_OPERATIONS, VIEW_GLOBAL_DASHBOARD,
    # APPROVE_PUBLICATION ou PUBLISH_CONTENT (ver `_ROLE_PERMISSIONS`).
    ATLAS = "ATLAS"
    # NAVIGATOR — Executor Operacional Multicanal (ver pacote `navigator/`).
    # Subordinado ao ATLAS — executa nas plataformas externas, nunca
    # decide/planeja/aprova. Menor privilégio possível: só o essencial
    # para executar tarefas operacionais e ler catálogo.
    NAVIGATOR = "NAVIGATOR"
    # FORGE — Engenharia, Manutenção e Reparo (ver pacote `forge/`).
    # Diagnostica e propõe correções; nunca aplica nada crítico sem
    # autorização explícita (ver `forge.engineer.ForgeEngineer.apply_fix`).
    FORGE = "FORGE"
    OPERATOR = "OPERATOR"      # agente/automação com permissões limitadas
    VIEWER = "VIEWER"          # somente leitura (dashboards, relatórios)
    AGENT = "AGENT"            # IA/worker de produção, escopo mínimo por job


class Permission(str, Enum):
    MANAGE_CHANNELS = "manage_channels"
    MANAGE_PROJECTS = "manage_projects"
    APPROVE_PUBLICATION = "approve_publication"
    PUBLISH_CONTENT = "publish_content"
    MANAGE_PRODUCTS = "manage_products"
    MANAGE_FUNNELS = "manage_funnels"
    VIEW_DASHBOARD = "view_dashboard"
    MANAGE_CREDENTIALS = "manage_credentials"
    MANAGE_FINANCE = "manage_finance"
    RUN_PRODUCTION_JOB = "run_production_job"
    READ_CATALOG = "read_catalog"
    # Adicionadas na microcorreção de hardening: cobrem as interfaces
    # governadas de CRM/Sales/Jobs/Learning que antes eram mutáveis sem
    # checagem de autorização.
    MANAGE_CRM = "manage_crm"
    MANAGE_SALES = "manage_sales"
    MANAGE_JOBS = "manage_jobs"
    MANAGE_LEARNING = "manage_learning"
    # MICROCORREÇÃO 2: operações SEM canal/projeto (globais — ex: um
    # lançamento financeiro sem canal, ou um job de manutenção do
    # sistema) exigem esta permissão ADICIONAL, além da permissão de
    # domínio (MANAGE_FINANCE/MANAGE_JOBS/etc). Por padrão, só o Owner
    # a possui — operações globais nunca são banais o suficiente para
    # ficarem no conjunto padrão do Operator.
    MANAGE_GLOBAL_OPERATIONS = "manage_global_operations"
    # MICROCORREÇÃO 3 (item 1): ver o agregado de TODOS os canais no
    # painel (não apenas os canais no escopo da própria sessão) exige
    # esta permissão explícita, além de VIEW_DASHBOARD. Por padrão, só o
    # Owner a possui.
    VIEW_GLOBAL_DASHBOARD = "view_global_dashboard"


# Menor privilégio: cada role só recebe o que precisa.
_ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.OWNER: set(Permission),  # Owner tem tudo, mas é o único humano autorizado
    # ATLAS: coordena CRM/Sales/Jobs/Learning/Produtos/Funis/Projetos e lê
    # o dashboard (sempre por canal — nunca o agregado global, que exige
    # VIEW_GLOBAL_DASHBOARD, restrita ao Owner). Deliberadamente SEM
    # MANAGE_CREDENTIALS, MANAGE_FINANCE, MANAGE_GLOBAL_OPERATIONS,
    # VIEW_GLOBAL_DASHBOARD, APPROVE_PUBLICATION ou PUBLISH_CONTENT —
    # ATLAS nunca publica, gasta, contrata ou altera credenciais (ver
    # `atlas.director.AtlasDirector.request_critical_action`, que SEMPRE
    # escalona essas ações ao Owner em vez de tentar executá-las).
    Role.ATLAS: {
        Permission.MANAGE_PROJECTS,
        Permission.MANAGE_PRODUCTS,
        Permission.MANAGE_FUNNELS,
        Permission.VIEW_DASHBOARD,
        Permission.RUN_PRODUCTION_JOB,
        Permission.READ_CATALOG,
        Permission.MANAGE_CRM,
        Permission.MANAGE_SALES,
        Permission.MANAGE_JOBS,
        Permission.MANAGE_LEARNING,
    },
    Role.OPERATOR: {
        Permission.MANAGE_PROJECTS,
        Permission.MANAGE_PRODUCTS,
        Permission.MANAGE_FUNNELS,
        Permission.VIEW_DASHBOARD,
        Permission.RUN_PRODUCTION_JOB,
        Permission.READ_CATALOG,
        Permission.MANAGE_CRM,
        Permission.MANAGE_SALES,
        Permission.MANAGE_JOBS,
        Permission.MANAGE_LEARNING,
    },
    Role.AGENT: {
        Permission.RUN_PRODUCTION_JOB,
        Permission.READ_CATALOG,
    },
    # NAVIGATOR: executa operacionalmente, nunca decide — mesmo escopo
    # mínimo do AGENT. Nenhuma ação crítica (publicar, gastar,
    # contratar, alterar credencial, conectar conta) é alcançável por
    # aqui de qualquer forma (ver `navigator.executor.NavigatorExecutor`,
    # que SEMPRE escala essas ações para o Owner via ATLAS).
    Role.NAVIGATOR: {
        Permission.RUN_PRODUCTION_JOB,
        Permission.READ_CATALOG,
    },
    # FORGE: diagnostica e lê para investigar — sem nenhuma permissão de
    # mutação de negócio. Aplicar uma correção exige autorização
    # explícita própria (ver `forge.engineer.ForgeEngineer.apply_fix`),
    # nunca apenas esta role.
    Role.FORGE: {
        Permission.VIEW_DASHBOARD,
        Permission.READ_CATALOG,
    },
    Role.VIEWER: {
        Permission.VIEW_DASHBOARD,
    },
}


class AccessDenied(Exception):
    pass


@dataclass
class Principal:
    """Identidade autenticada usada nas checagens de autorização."""
    subject: str
    role: Role
    scopes: set[str] = field(default_factory=set)  # ex: {"channel:abc123"}


def has_permission(principal: Principal, permission: Permission) -> bool:
    return permission in _ROLE_PERMISSIONS.get(principal.role, set())


def require_permission(principal: Principal, permission: Permission) -> None:
    if not has_permission(principal, permission):
        raise AccessDenied(
            f"role '{principal.role.value}' não possui permissão '{permission.value}'"
        )


def require_owner(principal: Principal) -> None:
    if principal.role != Role.OWNER:
        raise AccessDenied("ação restrita ao Owner")
