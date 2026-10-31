"""
core.models.db
==============
Inicialização do banco (SQLite local — Seção 62 Cost Control: usar
recursos locais/stdlib durante desenvolvimento). Isolamento estrutural
de Projeto/Canal é reforçado por chaves estrangeiras + checagem em
código (core.isolation).

MICROCORREÇÃO 5 (auditoria — reprovado na 4ª rodada, item 1 —
MIGRAÇÃO ATÔMICA E RECUPERÁVEL):

Falha original: a migração V3→V4 fazia RENAME + CREATE + cópia linha-a-
linha + DROP como passos SEPARADOS (não atômicos). Uma falha no meio
podia deixar a tabela nova incompleta; na reinicialização seguinte, o
código via que `idempotency_records` já existia com o schema novo e
concluía (erradamente) que a migração estava completa — permitindo
duplicação, já que os registros de idempotência da tabela antiga nunca
chegaram a ser copiados.

Corrigido: a migração inteira (rename, create, cópia+validação, drop)
roda dentro de UMA ÚNICA transação SQLite explícita
(`get_autocommit_connection` + `BEGIN IMMEDIATE` manual — nunca
`executescript`, que força commit implícito e quebra a atomicidade).
SQLite tem DDL totalmente transacional (RENAME/CREATE/DROP TABLE podem
ser revertidos por ROLLBACK), então uma falha em QUALQUER ponto reverte
TUDO — a tabela antiga nunca chega a sumir, com sua idempotência
intacta. Nenhuma linha que falhe ao converter ou colida é descartada
silenciosamente: qualquer problema aborta a transação inteira
(`MigrationBlocked`), preservando o banco anterior utilizável. Se uma
tabela de staging de uma tentativa INTERROMPIDA (de uma versão de
código anterior a esta correção) for encontrada, o código tenta
recuperação segura re-copiando a partir dela (fonte de verdade ainda
íntegra); se o formato da staging for inesperado, a inicialização é
bloqueada com um diagnóstico sanitizado em vez de prosseguir às cegas.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_DB_LOCKS: dict[str, threading.RLock] = {}
_DB_LOCKS_GUARD = threading.Lock()


def _db_lock(db_path: str) -> threading.RLock:
    """Serializa transações locais por arquivo sem alongar busy_timeout."""
    key = os.path.abspath(db_path)
    with _DB_LOCKS_GUARD:
        lock = _DB_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DB_LOCKS[key] = lock
        return lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    niche TEXT,
    subniche TEXT,
    audience TEXT,
    language TEXT,
    region TEXT,
    content_style TEXT,
    voice_profile TEXT,
    visual_identity TEXT,
    strategy TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    goal TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    budget REAL,
    created_at REAL NOT NULL
    -- Nota: sem FK para channels(channel_id) nesta fase — ver histórico
    -- em docs/SECURITY.md.
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    project_id TEXT,
    channel_id TEXT,
    job_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    -- idempotency_key NÃO é UNIQUE aqui — a unicidade real é garantida
    -- pela PRIMARY KEY composta em `idempotency_records`.
    idempotency_key TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    payload TEXT,
    result TEXT,
    error TEXT,
    -- MICROCORREÇÃO 5 (item 3): coluna de versão para controle de
    -- concorrência otimista — toda transição condiciona seu UPDATE a
    -- `version = <valor lido>`, detectando e recusando atualizações
    -- baseadas em estado obsoleto entre instâncias.
    version INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    niche TEXT,
    price REAL,
    commission_pct REAL,
    reputation_score REAL,
    refund_rate REAL,
    metadata TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
    offer_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    funnel_id TEXT,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS funnels (
    funnel_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    funnel_type TEXT NOT NULL,
    config TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    source TEXT,
    campaign TEXT,
    content_id TEXT,
    offer_id TEXT,
    product_id TEXT,
    status TEXT NOT NULL DEFAULT 'NEW',
    consent_state TEXT NOT NULL DEFAULT 'UNKNOWN',
    conversion_state TEXT NOT NULL DEFAULT 'UNKNOWN',
    -- MICROCORREÇÃO 5 (item 3): ver nota em `jobs.version` acima.
    version INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_events (
    event_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    lead_id TEXT,
    offer_id TEXT,
    event_type TEXT NOT NULL,
    value_type TEXT NOT NULL,
    amount REAL,
    currency TEXT DEFAULT 'BRL',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS finance_entries (
    entry_id TEXT PRIMARY KEY,
    channel_id TEXT,
    entry_type TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'BRL',
    description TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_gate_results (
    result_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reasons TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revoked_sessions (
    jti TEXT PRIMARY KEY,
    revoked_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

-- ATLAS+RADAR: solicitações críticas que exigem decisão do Owner —
-- persistidas para sobreviver a reinício/restart/nova sessão. Nunca
-- consideradas aprovadas apenas porque o processo reiniciou (status
-- default é sempre PENDING_OWNER_APPROVAL). `idempotency_key` com
-- índice único parcial garante que a mesma solicitação nunca é
-- executada duas vezes por retry/restart/replay.
CREATE TABLE IF NOT EXISTS owner_approval_requests (
    request_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    origin TEXT NOT NULL,
    reason TEXT NOT NULL,
    risk TEXT NOT NULL DEFAULT 'UNKNOWN',
    evidence TEXT NOT NULL DEFAULT '{}',
    context TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING_OWNER_APPROVAL',
    required_authority TEXT NOT NULL DEFAULT 'OWNER',
    decision TEXT,
    decided_by TEXT,
    decided_at REAL,
    idempotency_key TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_approval_idempotency
    ON owner_approval_requests(idempotency_key) WHERE idempotency_key IS NOT NULL;

-- ATLAS+RADAR (correção: execução recorrente do Radar): estado do
-- agendamento — uma linha por agendador ("default"). A concorrência de
-- EXECUÇÃO em si é garantida pela reserva atômica já existente em
-- `core/jobs/queue.py` (JobQueue.start), não por esta tabela — esta
-- tabela só guarda status/intervalo/últimas execuções para exibição e
-- para decidir SE uma execução está devida.
CREATE TABLE IF NOT EXISTS radar_schedule (
    schedule_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    interval_seconds REAL NOT NULL DEFAULT 3600,
    last_run_at REAL,
    last_run_status TEXT NOT NULL DEFAULT 'NEVER_RUN',
    next_run_at REAL,
    updated_at REAL NOT NULL
);

-- EXPANSÃO (coordenação multiagente): missões formais delegadas entre
-- agentes (ATLAS -> NAVIGATOR/FORGE/especialistas). Chave composta
-- (mission_id, task_id) — uma missão pode ter várias tarefas.
CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    objective TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '{}',
    permissions TEXT NOT NULL DEFAULT '[]',
    tools_allowed TEXT NOT NULL DEFAULT '[]',
    restrictions TEXT NOT NULL DEFAULT '[]',
    success_criteria TEXT NOT NULL DEFAULT '[]',
    deadline REAL,
    dependencies TEXT NOT NULL DEFAULT '[]',
    evidence_expected TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'QUEUED',
    result TEXT,
    error TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (mission_id, task_id)
);

-- EXPANSÃO (NAVIGATOR): trilha de execução operacional multicanal —
-- cada linha é UMA ação executada numa plataforma externa (real ou
-- MOCK) em nome de uma missão.
CREATE TABLE IF NOT EXISTS navigator_execution_log (
    entry_id TEXT PRIMARY KEY,
    mission_id TEXT,
    task_id TEXT,
    platform TEXT NOT NULL,
    account_context TEXT,
    task_description TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    approval_used TEXT,
    executor TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- EXPANSÃO (FORGE): registro do pipeline DETECTAR..LIBERAR de cada
-- diagnóstico/correção técnica.
CREATE TABLE IF NOT EXISTS forge_diagnostics (
    diagnostic_id TEXT PRIMARY KEY,
    issue_summary TEXT NOT NULL,
    root_cause TEXT,
    proposed_fix TEXT,
    risk_level TEXT NOT NULL DEFAULT 'UNKNOWN',
    status TEXT NOT NULL DEFAULT 'DETECTED',
    evidence TEXT NOT NULL DEFAULT '{}',
    blocked_operation TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- EXPANSÃO (Biblioteca Mestre): conhecimento validado e versionado,
-- consultável por qualquer agente antes de tarefas relevantes.
CREATE TABLE IF NOT EXISTS master_library_entries (
    entry_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    area TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL,
    responsible_agent TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'LOW',
    validity TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
    version INTEGER NOT NULL DEFAULT 1,
    supersedes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- EXPANSÃO (RADAR WEB COMERCIAL — Visão Evolutiva, itens 11-14/18):
-- histórico longitudinal VERSIONADO de estratégias/funis observados.
-- NUNCA sobrescrito — cada revisão anual adiciona uma NOVA linha
-- (nova versão), preservando integralmente o histórico anterior.
CREATE TABLE IF NOT EXISTS strategy_history (
    entry_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    year INTEGER NOT NULL,
    status TEXT NOT NULL,
    niches TEXT NOT NULL DEFAULT '[]',
    channels TEXT NOT NULL DEFAULT '[]',
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    score REAL,
    confidence REAL NOT NULL DEFAULT 0.0,
    changes TEXT NOT NULL DEFAULT '',
    outcome TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

-- CORREÇÃO (item 3 — RADAR WEB COMERCIAL: execução contínua real).
-- Tabela DEDICADA e SEPARADA de `radar_schedule` (usada pelo RADAR DE
-- OPORTUNIDADES já existente) — responsabilidades e estados distintos,
-- mesmo padrão de agendamento (nunca compartilha a mesma linha de
-- estado). Campos extras exigidos: last_run_error, last_run_duration,
-- last_run_discoveries.
CREATE TABLE IF NOT EXISTS radar_web_schedule (
    schedule_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    interval_seconds REAL NOT NULL DEFAULT 3600,
    last_run_at REAL,
    last_run_status TEXT NOT NULL DEFAULT 'NEVER_RUN',
    last_run_error TEXT,
    last_run_duration REAL,
    last_run_discoveries INTEGER NOT NULL DEFAULT 0,
    next_run_at REAL,
    updated_at REAL NOT NULL
);

-- CORREÇÃO (item 10 — round 9: persistência da root authority): uma
-- única linha, `singleton_guard='ROOT'` como chave primária — garante
-- que só existe UM registro de bootstrap raiz por banco de dados
-- (por instalação/tenant). Sobrevive a restart: uma nova
-- `AuthorityRegistry(db_path=...)` construída depois de um restart do
-- processo consulta esta tabela e recusa (`ALREADY_BOOTSTRAPPED`)
-- qualquer segunda tentativa de bootstrap, mesmo vindo de uma
-- instância Python totalmente nova.
CREATE TABLE IF NOT EXISTS authority_root_bootstrap (
    singleton_guard TEXT PRIMARY KEY DEFAULT 'ROOT',
    owner_id TEXT NOT NULL,
    root_grant_id TEXT NOT NULL,
    auth_service_id TEXT NOT NULL,
    application_id TEXT NOT NULL DEFAULT 'default',
    tenant_id TEXT NOT NULL DEFAULT 'default',
    bootstrapped_at REAL NOT NULL,
    bootstrap_reason TEXT NOT NULL,
    canonical_registry_id TEXT NOT NULL DEFAULT ''
);

-- CORREÇÃO (round 9 — continuidade real após restart): antes, só o
-- FATO do bootstrap era persistido — os grants em si (permissões,
-- decisões de revogação) viviam apenas em memória e desapareciam a
-- cada restart, deixando o sistema "bootstrapado" mas sem nenhum
-- grant utilizável. Agora TODO grant emitido/revogado é persistido
-- aqui — uma nova `AuthorityRegistry(db_path=...)` restaura o estado
-- completo (inclusive grants já revogados, que continuam revogados).
CREATE TABLE IF NOT EXISTS authority_grants (
    grant_id TEXT PRIMARY KEY,
    subject_actor_id TEXT NOT NULL,
    authority_level TEXT NOT NULL,
    issued_by TEXT NOT NULL,
    scopes TEXT NOT NULL,
    permissions TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL,
    approval_id TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    integrity_reference TEXT NOT NULL,
    revoked_at REAL,
    revocation_reason TEXT,
    canonical_registry_id TEXT,
    issued_by_principal TEXT,
    issued_by_grant TEXT,
    audit_id TEXT
);

-- CORREÇÃO (round 9): registros de verifier/source confiáveis também
-- precisam sobreviver a restart — são "decisões críticas" de
-- governança tanto quanto os grants que as autorizaram.
CREATE TABLE IF NOT EXISTS authority_registered_verifiers (
    verifier_id TEXT PRIMARY KEY,
    verifier_type TEXT NOT NULL,
    authorized INTEGER NOT NULL,
    capabilities TEXT NOT NULL,
    verification_methods TEXT NOT NULL,
    health TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    scope TEXT NOT NULL,
    registered_by TEXT,
    authority_grant_id TEXT,
    registration_reason TEXT,
    approval_id TEXT,
    audit_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS authority_registered_sources (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    registered_by TEXT,
    authority_grant_id TEXT,
    requested_trust_class TEXT NOT NULL,
    requested_verification_status TEXT NOT NULL,
    trust_class TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    health TEXT NOT NULL,
    verification_method TEXT,
    observed_at REAL NOT NULL,
    last_checked REAL NOT NULL,
    scope TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    integrity_reference TEXT,
    approval_id TEXT,
    audit_id TEXT
);

-- BLOCO FINAL 1 (item 6 — LEARNING LOOP): registra previsões reais,
-- outcomes reais, e aprendizados validados — sobrevive a restart,
-- nunca contamina outros canais/projetos silenciosamente.
CREATE TABLE IF NOT EXISTS learning_predictions (
    prediction_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    predicted_value REAL NOT NULL,
    scope_channel_id TEXT,
    scope_project_id TEXT,
    context TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    evidence_ids TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS learning_records (
    record_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    predicted_value REAL NOT NULL,
    actual_value REAL NOT NULL,
    scope_channel_id TEXT,
    scope_project_id TEXT,
    compared_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_validated (
    learning_id TEXT PRIMARY KEY,
    metric_name TEXT NOT NULL,
    scope_channel_id TEXT,
    scope_project_id TEXT,
    sample_size INTEGER NOT NULL,
    direction_accuracy REAL NOT NULL,
    mean_absolute_percentage_error REAL,
    statement TEXT NOT NULL,
    applies_globally INTEGER NOT NULL DEFAULT 0,
    validated_by TEXT,
    validated_at REAL NOT NULL
);

-- BLOCO FINAL 2 (item 1 — cofre seguro de tokens/segredos): valores
-- SEMPRE cifrados (Fernet/AES) antes de persistir — a coluna
-- encrypted_value nunca contém texto puro.
CREATE TABLE IF NOT EXISTS vault_tokens (
    reference_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    token_type TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '',
    expires_at REAL,
    created_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    encrypted_value BLOB NOT NULL,
    key_mode TEXT NOT NULL DEFAULT 'EPHEMERAL'
);

-- BLOCO FINAL 2 (item 7 — observabilidade estruturada): cada campo é
-- uma coluna própria, nunca texto livre — permite consultas reais.
CREATE TABLE IF NOT EXISTS execution_observability (
    entry_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_or_channel TEXT,
    executed_at REAL NOT NULL,
    success INTEGER NOT NULL,
    mechanism_used TEXT,
    evidence_ref TEXT,
    error_reason TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    escalation_id TEXT,
    cost_amount REAL,
    cost_currency TEXT
);

-- MICROCORREÇÃO BF2 (item 3 — Owner Approval Anti-Replay): registra
-- cada escalação CRÍTICA já CONSUMIDA (executada com sucesso) — uma
-- escalação aprovada só pode disparar o efeito externo real UMA VEZ;
-- retries subsequentes com a MESMA idempotency_key devolvem o
-- resultado cacheado aqui, nunca re-executam.
CREATE TABLE IF NOT EXISTS navigator_consumed_escalations (
    escalation_id TEXT PRIMARY KEY,
    idempotency_key TEXT,
    result_success INTEGER NOT NULL,
    result_data TEXT NOT NULL DEFAULT '{}',
    result_reason TEXT NOT NULL DEFAULT '',
    result_mechanism_used TEXT,
    result_is_mock_fallback INTEGER NOT NULL DEFAULT 0,
    result_action TEXT NOT NULL,
    result_platform TEXT NOT NULL,
    consumed_at REAL NOT NULL
);

-- MICROCORREÇÃO BF2 (item 2 — Spend Approval Single-Use): vínculo
-- PERMANENTE escalation_id -> approval_id — uma escalação aprovada
-- para R$X gera EXATAMENTE um SpendApproval, nunca mais de um, mesmo
-- sob restart/concorrência (escalation_id é UNIQUE).
CREATE TABLE IF NOT EXISTS spend_approvals (
    approval_id TEXT PRIMARY KEY,
    escalation_id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    campaign_reference TEXT NOT NULL,
    approved_amount REAL NOT NULL,
    currency TEXT NOT NULL,
    spent_so_far REAL NOT NULL DEFAULT 0.0,
    approved_at REAL NOT NULL
);

-- MICROCORREÇÃO (ATOMIC ANTI-REPLAY — falha real confirmada por
-- auditoria independente com servidor HTTP real): registro de CLAIM
-- atômico via `INSERT` com `PRIMARY KEY` — só uma transação consegue
-- inserir a linha para um dado `escalation_id`; qualquer segunda
-- tentativa concorrente (mesmo processo ou processo diferente,
-- compartilhando o mesmo banco) falha com violação de chave
-- primária, nunca ambas "vencem". `status` transiciona
-- CLAIMED -> CONSUMED (sucesso) ou a linha é DELETADA (falha, libera
-- para nova tentativa).
CREATE TABLE IF NOT EXISTS navigator_execution_claims (
    escalation_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    idempotency_key TEXT,
    claimed_at REAL NOT NULL,
    consumed_at REAL,
    result_success INTEGER,
    result_data TEXT,
    result_reason TEXT,
    result_mechanism_used TEXT,
    result_is_mock_fallback INTEGER,
    result_action TEXT,
    result_platform TEXT
);

-- BLOCO FINAL 3 (fechamento da V1 — persistência restante do
-- ClaimRegistry) + MICROCORREÇÃO BF3 (isolamento multi-tenant real —
-- falha real reproduzida por auditoria independente: appB/tenantB
-- conseguia `restore_for_app()` sobre o MESMO `db_path` de
-- appA/tenantA e resolver claims de A). Escopo por
-- application_id/tenant_id/governance_context_id — a MESMA
-- disciplina de `authority_root_bootstrap`/`authority_grants` (que já
-- têm application_id/tenant_id) — nunca um claim de um contexto é
-- visível para outro, mesmo compartilhando o mesmo arquivo SQLite.
-- MICROCORREÇÃO BF3 MULTITENANT (identidade persistente escopada —
-- falha real reproduzida por auditoria independente: `claim_id` como
-- PRIMARY KEY GLOBAL, combinado com `INSERT OR REPLACE`, permitia que
-- appB/tenantB registrasse o MESMO `claim_id` de appA/tenantA e
-- SUBSTITUÍSSE o registro de A). A identidade real de um claim
-- persistido é o par completo (application_id, tenant_id,
-- governance_context_id, claim_id) — nunca claim_id sozinho. O MESMO
-- claim_id pode existir em escopos diferentes sem colidir.
CREATE TABLE IF NOT EXISTS evidence_claim_registry (
    claim_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    statement TEXT NOT NULL,
    related_metric_names TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_requirements TEXT NOT NULL,
    created_at REAL NOT NULL,
    application_id TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    governance_context_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (application_id, tenant_id, governance_context_id, claim_id)
);

-- MICROCORREÇÃO (ATLAS EXPANSION 1 — proteção multi-instância real —
-- falha real reproduzida por auditoria independente): a proteção
-- contra execução concorrente de uma MESMA `(mission_id, task_id)`
-- em `GraphExecutor` era local à instância Python (`_inflight`) —
-- duas instâncias diferentes (dois processos, ou até dois objetos
-- `GraphExecutor` diferentes) não tinham como saber uma da outra, e
-- após um restart (nova instância, `_inflight` vazio na memória), um
-- timeout cujo desfecho nunca fora confirmado (`TIMED_OUT_UNKNOWN_
-- OUTCOME`) podia ser retentado, duplicando o efeito externo se a
-- execução original ainda estivesse (ou tivesse ficado) em andamento.
-- Esta tabela é o "lease" (arrendamento de execução) REAL,
-- PERSISTENTE e ATÔMICO — `INSERT` com `PRIMARY KEY (mission_id,
-- task_id)` só sucede para QUEM chega primeiro; qualquer outra
-- tentativa (mesma instância, outra instância, ou depois de um
-- restart) encontra a linha já existente e nunca inicia uma segunda
-- execução — nunca reexecuta silenciosamente, nunca gira
-- indefinidamente. `status='UNKNOWN_OUTCOME'` sobrevive a restart
-- por definição (está no banco) e NUNCA vira retry automático — só
-- é resolvido quando a thread original genuinamente termina (mesma
-- `lease_token`) ou por uma ação explícita futura fora deste escopo.
-- COMANDO 2/6 — GÊMEO DIGITAL DO ATLAS: registro APPEND-ONLY de
-- afirmações sobre o estado do negócio. Cada linha é UMA versão de UMA
-- afirmação, com classe epistêmica (FACT/INFERENCE/HYPOTHESIS/FORECAST),
-- proveniência, observação, validade, qualidade, confiança e versão.
-- Nunca é atualizada nem apagada — o histórico é a própria tabela.
-- COMANDO 3/6 — planos operacionais do NAVIGATOR com CHECKPOINT por
-- passo: o estado de cada passo é gravado a cada transição, então uma
-- execução longa retoma do ponto exato após crash/restart (nunca
-- recomeça o fluxo inteiro, nunca repete um passo concluído).
-- COMANDO 5/6 — jornada de PEDIDO (checkout ≠ venda ≠ pagamento ≠ entrega).
-- `processed_events_json` garante idempotência por evento do gateway.
-- v1.0.1-OPERATIONAL-CANDIDATE: kill switch global persistente + histórico.
CREATE TABLE IF NOT EXISTS operational_kill_switch (
    id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL,
    changed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS operational_kill_switch_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, state TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL,
    changed_at REAL NOT NULL
);
-- v1.0.1: medição e limites de custo de IA/API.
CREATE TABLE IF NOT EXISTS ai_api_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, service TEXT NOT NULL, operation TEXT NOT NULL,
    created_at REAL NOT NULL, input_tokens INTEGER, output_tokens INTEGER, units REAL, cost REAL,
    cost_status TEXT NOT NULL, currency TEXT, job_id TEXT, project TEXT, campaign TEXT, success INTEGER NOT NULL
);
-- v1.0.2: RESERVA atômica de orçamento (autorizar+contabilizar sem corrida).
CREATE TABLE IF NOT EXISTS ai_api_reservations (
    reservation_id TEXT PRIMARY KEY, provider TEXT NOT NULL, service TEXT NOT NULL, operation TEXT NOT NULL,
    amount REAL NOT NULL, currency TEXT NOT NULL, project TEXT, campaign TEXT, job_id TEXT, status TEXT NOT NULL,
    created_at REAL NOT NULL, settled_at REAL, outcome TEXT, detail TEXT
);
-- v1.0.3: incidentes de custo (overrun / custo inválido reportado) — o
-- provedor fica BLOQUEADO até o Owner revisar.
CREATE TABLE IF NOT EXISTS ai_api_cost_incidents (
    incident_id TEXT PRIMARY KEY, provider TEXT NOT NULL, reservation_id TEXT, kind TEXT NOT NULL,
    reserved_amount REAL, reported_cost TEXT, currency TEXT, status TEXT NOT NULL, created_at REAL NOT NULL,
    resolved_at REAL, resolved_by TEXT, evidence TEXT
);
CREATE TABLE IF NOT EXISTS ai_api_limits (
    provider TEXT NOT NULL, kind TEXT NOT NULL, scope_id TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
    updated_at REAL NOT NULL, actor TEXT NOT NULL, PRIMARY KEY (provider, kind, scope_id)
);
-- v1.0.1: janelas do scheduler (sem duplicação/overlap) e heartbeat do worker.
CREATE TABLE IF NOT EXISTS scheduler_ticks (
    tick_name TEXT NOT NULL, slot INTEGER NOT NULL, started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
    detail TEXT, PRIMARY KEY (tick_name, slot)
);
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id TEXT PRIMARY KEY, host TEXT, pid INTEGER, at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS commercial_orders (
    order_id TEXT PRIMARY KEY,
    lead_id TEXT, product_id TEXT, offer_id TEXT, campaign_id TEXT, channel_id TEXT NOT NULL,
    traffic_source TEXT, amount REAL NOT NULL, product_cost REAL, commission REAL,
    status TEXT NOT NULL,
    history_json TEXT NOT NULL DEFAULT '[]',
    processed_events_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    -- CORREÇÃO PÓS-AUDITORIA (C): fingerprint imutável de cada evento.
    event_fingerprints_json TEXT NOT NULL DEFAULT '{}'
);

-- COMANDO 5/6 — registro verificável de CLAIMS/promessas comerciais.
CREATE TABLE IF NOT EXISTS commercial_claims (
    claim_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    channels_json TEXT NOT NULL,
    restrictions TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT NOT NULL DEFAULT '',
    valid_until REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS navigator_operation_runs (
    run_id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    steps_state_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS atlas_twin_facts (
    fact_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value_json TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL DEFAULT '',
    observed_at REAL NOT NULL,
    valid_until REAL NOT NULL,
    confidence REAL NOT NULL,
    quality REAL NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    derived_from_json TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL,
    supersedes TEXT,
    recorded_at REAL NOT NULL,
    recorded_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_atlas_twin_facts_key
    ON atlas_twin_facts (entity_type, entity_id, attribute);

-- COMANDO 2/6 — memória de decisões do ATLAS (persistente): o que foi
-- decidido, as alternativas comparadas, a evidência usada, a incerteza
-- declarada e o status (inclusive BLOCKED/ABSTAINED — não agir também
-- é decisão registrada).
CREATE TABLE IF NOT EXISTS atlas_decisions (
    decision_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    chosen TEXT,
    alternatives_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    justification TEXT NOT NULL,
    uncertainty TEXT NOT NULL DEFAULT '',
    supersedes TEXT,
    created_at REAL NOT NULL,
    actor TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_atlas_decisions_topic ON atlas_decisions (topic, scope, created_at);

-- COMANDO 2/6 — estratégias em teste no ciclo adaptativo (persistente).
CREATE TABLE IF NOT EXISTS atlas_strategies (
    strategy_id TEXT PRIMARY KEY,
    scope_channel_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    test_design TEXT NOT NULL,
    status TEXT NOT NULL,
    causal_status TEXT NOT NULL,
    hypothesis_fact_id TEXT NOT NULL,
    predictions_json TEXT NOT NULL DEFAULT '[]',
    last_evaluation TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_executor_task_leases (
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    status TEXT NOT NULL,
    leased_at REAL NOT NULL,
    resolved_at REAL,
    outcome_json TEXT,
    error TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    -- CORREÇÃO P1 (invariante dura — runner vivo NUNCA é convertido
    -- para UNKNOWN_OUTCOME por atraso do heartbeat): identidade do
    -- processo detentor + último sinal de vida. A conversão por TTL
    -- passa a exigir PROVA de que o processo detentor não existe
    -- mais; staleness de `leased_at` sozinho nunca converte um
    -- detentor vivo. Aditivo — linhas antigas ficam NULL (legado,
    -- comportamento anterior preservado).
    holder_pid INTEGER,
    holder_seen_at REAL,
    -- FECHAMENTO 1/6 (PID reutilizado): identidade temporal do processo
    -- detentor (instante de criação no relógio do SO, string opaca
    -- comparada por igualdade). NULL = legado/plataforma sem suporte.
    holder_proc_start TEXT,
    PRIMARY KEY (mission_id, task_id)
);

-- ATLAS PIPELINE 1 (INTELIGÊNCIA E DESCOBERTA): fila PERSISTENTE de
-- oportunidades de conteúdo — a peça que falta em `atlas.radar`
-- (cujo rastreamento é só em memória, não sobrevive a restart).
-- `identity_key` (determinística — plataforma+tema+formato+gancho
-- normalizados, nunca o `opportunity_id` aleatório) é a PRIMARY KEY:
-- um `INSERT ... ON CONFLICT DO UPDATE` atômico garante que a MESMA
-- oportunidade descoberta de novo atualiza a linha existente, nunca
-- duplica — mesmo sob concorrência real entre execuções
-- independentes (nunca um read-then-write vulnerável a corrida).
CREATE TABLE IF NOT EXISTS discovery_opportunities (
    identity_key TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    theme TEXT NOT NULL,
    content_format TEXT NOT NULL,
    hook TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    notes_json TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    discovered_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    score TEXT,
    confidence TEXT,
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    risk_level TEXT,
    justification_json TEXT,
    version INTEGER NOT NULL DEFAULT 1
);

-- MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real encontrada por
-- auditoria independente, 6ª rodada — fechamento): `VerifiedLineage`
-- (persistida dentro de `signals_json`, acima) não era ancorada a
-- nenhum registro INDEPENDENTE e AUTORITATIVO — um `acquisition_id`
-- podia ser trocado por qualquer string, e enquanto `value` e seu
-- digest continuassem batendo entre si, nada detectava a adulteração.
-- Esta tabela é o registro AUTORITATIVO e SEPARADO de cada aquisição
-- genuína — `VerifiedLineage` REFERENCIA `acquisition_id` aqui, nunca
-- o substitui. Revalidação (mesmo após restart, sem o broker
-- efêmero) sempre cruza a lineage persistida no sinal contra ESTE
-- registro independente — nunca confia apenas no que está dentro do
-- próprio blob do sinal. Nenhum segredo/token é persistido aqui —
-- só metadados de proveniência e um digest das observações.
CREATE TABLE IF NOT EXISTS discovery_acquisitions (
    acquisition_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    data_status TEXT NOT NULL,
    issued_at REAL NOT NULL,
    completed_at REAL NOT NULL,
    result_digest TEXT NOT NULL,
    observations_digest TEXT NOT NULL,
    observation_field_digests TEXT NOT NULL DEFAULT '{}'
);
"""

def _idempotency_table_ddl(table_name: str) -> str:
    return f"""
CREATE TABLE {table_name} (
    domain TEXT NOT NULL,
    operation TEXT NOT NULL,
    channel_id TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    record_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (domain, operation, channel_id, project_id, idempotency_key)
)
"""


IDEMPOTENCY_TABLE_SQL = _idempotency_table_ddl("idempotency_records")

# MICROCORREÇÃO 6 (auditoria — reprovado na 5ª rodada):
#
# item 1 (RECONHECER OS ESTADOS LEGADOS CORRETOS): a V4 real deixava a
# tabela antiga com o nome `idempotency_records_v3_old` — a V5
# só procurava `idempotency_records_migration_staging` (o nome que a
# PRÓPRIA V5 usa internamente), então nunca reconhecia o estado real
# deixado por uma V4 interrompida, e caía de volta no bug original
# (tratava a existência de `idempotency_records` com schema novo como
# prova de migração concluída). Agora AMBOS os nomes históricos são
# reconhecidos, e mais de uma tabela legada presente simultaneamente é
# suportado (todas são lidas e mescladas).
#
# item 2 (PRESERVAR A IDEMPOTÊNCIA DE TODAS AS FONTES): a recuperação
# anterior APAGAVA a tabela nova (que podia já ter operações legítimas
# confirmadas DEPOIS da interrupção) e reconstruía só a partir da
# tabela antiga — perdendo esses registros e permitindo duplicação no
# retry. Agora a reconstrução MESCLA as fontes: registros já
# confirmados no schema novo + registros de TODAS as tabelas legadas
# encontradas. Identidades equivalentes (mesmo record_id e fingerprint)
# são consolidadas; identidades com conteúdo incompatível bloqueiam a
# recuperação inteira (`MigrationBlocked`) — nunca escolhe uma versão
# arbitrariamente, nunca sobrescreve, nunca descarta em silêncio.
#
# item 3 (RECUPERAÇÃO ATÔMICA E REPETÍVEL): detecção, leitura de TODAS
# as fontes, construção do resultado mesclado, validação de contagem e
# troca definitiva de tabelas vivem dentro de UMA transação SQLite
# explícita (mesmo mecanismo `BEGIN IMMEDIATE`/commit-dentro-do-try/
# rollback+close já corrigido na V5) — uma falha em qualquer ponto
# (cópia, troca de tabelas, ou o commit em si) reverte tudo, preserva
# todas as fontes originais intactas, e fecha a conexão corretamente.
_LEGACY_TABLE_NAMES = ("idempotency_records_v3_old", "idempotency_records_migration_staging")
_REBUILD_TABLE_NAME = "idempotency_records_rebuild_v6"
_INLINE_LEGACY_TABLE_NAME = "idempotency_records_inline_legacy_v6"
_OLD_IDEMPOTENCY_COLUMNS = {"composite_key", "domain", "record_id", "fingerprint", "created_at"}
_NEW_IDEMPOTENCY_COLUMNS = {
    "domain", "operation", "channel_id", "project_id", "idempotency_key", "record_id", "fingerprint", "created_at",
}


class MigrationBlocked(Exception):
    """Levantado quando a migração/recuperação de schema encontra um
    estado que não pode ser resolvido com segurança automaticamente. A
    inicialização é interrompida DELIBERADAMENTE — nada é descartado,
    sobrescrito ou apagado; todas as fontes permanecem exatamente como
    estavam."""


def remove_db_file(db_path: str) -> None:
    """CORREÇÃO (microcorreção final — vazamento de recursos): modo
    WAL (ver `get_connection()`) cria arquivos auxiliares `-wal`/`-shm`
    junto ao banco principal. `os.remove(db_path)` sozinho deixa esses
    arquivos órfãos em disco (ex: em `/tmp`, quando testes usam
    `tempfile.mkstemp()`). Este helper remove o banco principal E
    qualquer `-wal`/`-shm`/`-journal` associado — uso recomendado em
    vez de `os.remove()` direto sempre que um teste (ou qualquer
    código) precisa apagar um banco temporário por completo."""
    # v1.0.1: o banco configurado foi removido explicitamente — kill switch e
    # guarda de custo deixam de apontar para ele (não há estado a consultar).
    try:
        from core.operations import cost_guard as _cg
        from core.operations import kill_switch as _ks

        if _ks.configured_db() == db_path:
            _ks.configure(None)
        if _cg.configured_db() == db_path:
            _cg.configure(None)
    except ImportError:
        pass
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)


def get_connection(db_path: str) -> sqlite3.Connection:
    """CORREÇÃO (travamento reproduzível da regressão monolítica):
    antes, conexões usavam o modo padrão ("rollback journal") do
    SQLite — escrita exige lock EXCLUSIVO e bloqueia TODOS os leitores
    durante a escrita, e sem `busy_timeout` explícito, uma conexão
    concorrente podia ficar esperando indefinidamente. Corrigido com
    modo WAL (permite múltiplos leitores concorrentes com um único
    escritor — a causa raiz real de contenção desaparece na maioria
    dos casos) + `busy_timeout` explícito.

    CORREÇÃO (microcorreção final — instabilidade intermitente de
    encerramento/concorrência): o valor original de `busy_timeout`
    (10000ms) colidia, na pior hipótese, com o timeout de
    `RadarWorker.stop()`/`RadarWebWorker.stop()` usado em alguns testes
    (`stop(timeout=5.0)`, ver `tests/test_atlas_radar_worker.py` e
    outros) — se a operação SQLite do worker estivesse bloqueada
    esperando o lock por até 10s, o `join(timeout=5.0)` do `stop()`
    podia desistir de esperar ANTES da thread realmente terminar,
    deixando uma thread genuinamente presa rodando em segundo plano
    por mais alguns segundos — uma condição de corrida sensível a
    timing, exatamente o tipo de instabilidade intermitente relatada
    (às vezes a suíte termina limpa, às vezes não, dependendo de como
    os timings coincidem). Reduzido para 2000ms — ainda generoso para
    a contenção residual que resta sob WAL (que já elimina a maior
    parte dos casos de espera), mas com margem folgada (2.5x) abaixo
    do MENOR timeout de `stop()` usado em qualquer teste."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 2000;")
    return conn


def get_autocommit_connection(db_path: str) -> sqlite3.Connection:
    """Conexão em modo autocommit (`isolation_level=None`) para operações
    que precisam controlar manualmente `BEGIN IMMEDIATE` / `COMMIT` /
    `ROLLBACK` em torno de MÚLTIPLAS instruções como uma única transação
    atômica. O chamador é responsável por commit/rollback/close.

    CORREÇÃO (mesma razão de `get_connection()`): WAL + `busy_timeout`
    explícito, reduzido para 2000ms (ver docstring de `get_connection()`
    para o raciocínio completo sobre a margem em relação aos timeouts
    de `stop()` dos workers)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 2000;")
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_fragment: str) -> None:
    """ALTER TABLE ADD COLUMN aditivo e seguro (SQLite preenche linhas
    existentes com o DEFAULT declarado) — usado para `version` em
    `leads`/`jobs` em bancos criados antes desta coluna existir."""
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl_fragment}")


def _migrate_claim_registry_composite_key(db_path: str) -> None:
    """MICROCORREÇÃO BF3 MULTITENANT — reconstrói `evidence_claim_
    registry` com PRIMARY KEY composta (application_id, tenant_id,
    governance_context_id, claim_id) quando o banco ainda tem o
    schema ANTIGO (claim_id como PRIMARY KEY sozinho). SQLite não
    permite `ALTER TABLE ... ADD CONSTRAINT`/mudar PRIMARY KEY
    diretamente — o caminho seguro é reconstruir: criar a tabela nova
    com o schema correto, copiar todos os dados, dropar a antiga,
    renomear a nova para o nome original.

    IDEMPOTENTE: a checagem do schema atual acontece DENTRO da mesma
    transação que faria a reconstrução (`BEGIN IMMEDIATE` — adquire o
    lock de escrita antes de checar) — uma segunda chamada
    concorrente, depois de esperar o lock, vê o schema JÁ migrado e
    sai sem fazer nada. ATÔMICA: toda a reconstrução roda dentro de
    UMA transação — nunca deixa o banco num estado intermediário
    (tabela nova criada mas dados não copiados, ou tabela antiga já
    dropada mas a nova ainda não renomeada) se algo falhar no meio;
    um erro reverte tudo via `rollback()`."""
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            pk_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(evidence_claim_registry)").fetchall()
                if row["pk"] > 0
            }
            if pk_columns == {"application_id", "tenant_id", "governance_context_id", "claim_id"}:
                conn.rollback()  # já migrada — nada a fazer, apenas libera a transação aberta
                return

            conn.execute("DROP TABLE IF EXISTS evidence_claim_registry_new")
            conn.execute(
                "CREATE TABLE evidence_claim_registry_new ("
                "claim_id TEXT NOT NULL, strategy_id TEXT NOT NULL, claim_type TEXT NOT NULL, "
                "statement TEXT NOT NULL, related_metric_names TEXT NOT NULL, status TEXT NOT NULL, "
                "evidence_requirements TEXT NOT NULL, created_at REAL NOT NULL, "
                "application_id TEXT NOT NULL DEFAULT '', tenant_id TEXT NOT NULL DEFAULT '', "
                "governance_context_id TEXT NOT NULL DEFAULT '', "
                "PRIMARY KEY (application_id, tenant_id, governance_context_id, claim_id))"
            )
            conn.execute(
                "INSERT INTO evidence_claim_registry_new "
                "(claim_id, strategy_id, claim_type, statement, related_metric_names, status, "
                "evidence_requirements, created_at, application_id, tenant_id, governance_context_id) "
                "SELECT claim_id, strategy_id, claim_type, statement, related_metric_names, status, "
                "evidence_requirements, created_at, application_id, tenant_id, governance_context_id "
                "FROM evidence_claim_registry"
            )
            conn.execute("DROP TABLE evidence_claim_registry")
            conn.execute("ALTER TABLE evidence_claim_registry_new RENAME TO evidence_claim_registry")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _split_old_composite(domain: str, composite_key: str) -> tuple[str, str, str, str] | None:
    """Reconstrói (operation, channel_id, project_id, idempotency_key) a
    partir do formato ANTIGO de composite_key, por domínio. Best-effort:
    channel_id/project_id/job_type nunca continham '::' na prática, então
    o split isola corretamente a idempotency_key mesmo que ELA contenha
    '::'. Retorna None se o formato não bater — o chamador trata isso
    como falha de migração (nunca descarta silenciosamente)."""
    if domain in ("lead", "sales_event"):
        parts = composite_key.split("::", 1)
        if len(parts) != 2:
            return None
        channel_id, idem_key = parts
        operation = "create_lead" if domain == "lead" else "record_sales_event"
        return operation, channel_id, "", idem_key
    if domain == "finance_entry":
        parts = composite_key.split("::", 1)
        if len(parts) != 2:
            return None
        channel_raw, idem_key = parts
        channel_id = "" if channel_raw == "GLOBAL" else channel_raw
        return "record_finance_entry", channel_id, "", idem_key
    if domain == "job":
        parts = composite_key.split("::", 3)
        if len(parts) != 4:
            return None
        channel_raw, project_raw, job_type, idem_key = parts
        channel_id = "" if channel_raw == "-" else channel_raw
        project_id = "" if project_raw == "-" else project_raw
        return job_type, channel_id, project_id, idem_key
    return None


def _detect_present_legacy_tables(conn: sqlite3.Connection) -> list[str]:
    return [name for name in _LEGACY_TABLE_NAMES if _table_exists(conn, name)]


def _read_legacy_rows(conn: sqlite3.Connection, table_name: str) -> list[sqlite3.Row]:
    columns = _table_columns(conn, table_name)
    if not _OLD_IDEMPOTENCY_COLUMNS.issubset(columns):
        raise MigrationBlocked(
            f"tabela legada '{table_name}' encontrada em formato inesperado — "
            "inicialização bloqueada por segurança; nenhum dado foi apagado ou alterado"
        )
    return conn.execute(f"SELECT * FROM {table_name}").fetchall()


def _read_confirmed_new_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Lê as linhas JÁ CONFIRMADAS em `idempotency_records` no schema
    novo — operações legítimas que podem ter sido gravadas DEPOIS de uma
    migração/recuperação anterior interrompida e ANTES desta rodar.
    Nunca podem ser perdidas pela recuperação (item 2 da auditoria)."""
    if not _table_exists(conn, "idempotency_records"):
        return []
    columns = _table_columns(conn, "idempotency_records")
    if "composite_key" in columns:
        return []  # ainda schema antigo — tratado como fonte legada "inline", não aqui
    if not _NEW_IDEMPOTENCY_COLUMNS.issubset(columns):
        raise MigrationBlocked(
            "tabela 'idempotency_records' existente está em formato inesperado (nem schema "
            "antigo reconhecido, nem schema novo completo) — inicialização bloqueada por "
            "segurança; nenhum dado foi apagado ou alterado"
        )
    return conn.execute("SELECT * FROM idempotency_records").fetchall()


def _needs_recovery(conn: sqlite3.Connection) -> bool:
    if _detect_present_legacy_tables(conn):
        return True
    if _table_exists(conn, "idempotency_records"):
        if "composite_key" in _table_columns(conn, "idempotency_records"):
            return True
    return False


def _run_unified_idempotency_recovery(conn: sqlite3.Connection) -> None:
    """Reconstrói `idempotency_records` MESCLANDO todas as fontes
    conhecidas — qualquer tabela legada presente (com QUALQUER um dos
    nomes históricos, inclusive mais de uma ao mesmo tempo) MAIS os
    registros já confirmados no schema novo (se `idempotency_records`
    já existir parcial ou totalmente populada). Identidades equivalentes
    (mesmo record_id + fingerprint) são consolidadas silenciosamente;
    identidades com conteúdo incompatível bloqueiam a recuperação
    inteira. Tudo dentro de UMA transação atômica — falha em qualquer
    ponto reverte tudo, preservando todas as fontes originais."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"DROP TABLE IF EXISTS {_REBUILD_TABLE_NAME}")

        legacy_tables = _detect_present_legacy_tables(conn)

        # Se `idempotency_records` em si ainda estiver no schema antigo
        # (V3 nunca migrada), ela TAMBÉM é uma fonte legada — renomeia
        # para um nome de trabalho ANTES de ler as demais fontes, para
        # não colidir com o nome final quando tudo for trocado.
        inline_old_schema = False
        if _table_exists(conn, "idempotency_records"):
            if "composite_key" in _table_columns(conn, "idempotency_records"):
                inline_old_schema = True
                conn.execute(f"DROP TABLE IF EXISTS {_INLINE_LEGACY_TABLE_NAME}")
                conn.execute(f"ALTER TABLE idempotency_records RENAME TO {_INLINE_LEGACY_TABLE_NAME}")
                legacy_tables = [*legacy_tables, _INLINE_LEGACY_TABLE_NAME]

        confirmed_new_rows = [] if inline_old_schema else _read_confirmed_new_rows(conn)

        merged: dict[tuple, tuple[str, str, float]] = {}
        conflicts: list[str] = []

        def consider(key: tuple, record_id: str, fingerprint: str, created_at: float, source: str) -> None:
            existing = merged.get(key)
            if existing is None:
                merged[key] = (record_id, fingerprint, created_at)
                return
            existing_record_id, existing_fp, _ = existing
            if existing_record_id == record_id and existing_fp == fingerprint:
                return  # equivalente — consolida sem duplicar, sem conflito
            conflicts.append(
                f"identidade {key}: fonte '{source}' aponta para record_id='{record_id}' "
                f"mas já havia um registro incompatível (record_id='{existing_record_id}')"
            )

        # Prioridade de leitura não importa para o merge (equivalência é
        # comutativa), mas lemos as confirmadas no schema novo primeiro
        # por clareza de raciocínio.
        for row in confirmed_new_rows:
            key = (row["domain"], row["operation"], row["channel_id"], row["project_id"], row["idempotency_key"])
            consider(key, row["record_id"], row["fingerprint"], row["created_at"], "idempotency_records (confirmado)")

        for table_name in legacy_tables:
            for row in _read_legacy_rows(conn, table_name):
                parsed = _split_old_composite(row["domain"], row["composite_key"])
                if parsed is None:
                    raise MigrationBlocked(
                        f"registro de idempotência em formato inesperado na tabela legada "
                        f"'{table_name}' (domain='{row['domain']}') — abortando com segurança, "
                        "nenhuma fonte foi alterada"
                    )
                operation, channel_id, project_id, idem_key = parsed
                key = (row["domain"], operation, channel_id, project_id, idem_key)
                consider(key, row["record_id"], row["fingerprint"], row["created_at"], table_name)

        if conflicts:
            raise MigrationBlocked(
                "conflito entre fontes de idempotência durante a recuperação — abortando com "
                "segurança, nenhuma fonte foi alterada: " + "; ".join(conflicts)
            )

        conn.execute(_idempotency_table_ddl(_REBUILD_TABLE_NAME))
        for key, (record_id, fingerprint, created_at) in merged.items():
            domain, operation, channel_id, project_id, idem_key = key
            conn.execute(
                f"INSERT INTO {_REBUILD_TABLE_NAME} "
                "(domain, operation, channel_id, project_id, idempotency_key, record_id, fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (domain, operation, channel_id, project_id, idem_key, record_id, fingerprint, created_at),
            )

        rebuilt_count = conn.execute(f"SELECT COUNT(*) as n FROM {_REBUILD_TABLE_NAME}").fetchone()["n"]
        if rebuilt_count != len(merged):
            raise MigrationBlocked(
                "contagem pós-reconstrução não bate com o conjunto consolidado — abortando "
                "com segurança, nenhuma fonte foi alterada"
            )

        # Só agora, com o resultado mesclado validado, trocamos as
        # tabelas definitivamente.
        if not inline_old_schema and _table_exists(conn, "idempotency_records"):
            conn.execute("DROP TABLE idempotency_records")
        conn.execute(f"ALTER TABLE {_REBUILD_TABLE_NAME} RENAME TO idempotency_records")

        for table_name in legacy_tables:
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")

        conn.commit()  # commit DENTRO do try — falha aqui também aciona o except abaixo
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass  # não mascara o erro original
        raise


def _migrate_idempotency_table_if_needed(db_path: str) -> None:
    """Ponto de entrada — abre sua PRÓPRIA conexão em modo autocommit.
    Se há QUALQUER coisa a recuperar (tabela legada com qualquer nome
    histórico, ou `idempotency_records` ainda no schema antigo), delega
    para a recuperação unificada (mesclagem de todas as fontes). Caso
    contrário, apenas garante que a tabela nova existe (banco novo) ou
    não faz nada (já migrada corretamente, sem nada pendente)."""
    conn = get_autocommit_connection(db_path)
    try:
        if _needs_recovery(conn):
            _run_unified_idempotency_recovery(conn)
            return

        if not _table_exists(conn, "idempotency_records"):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(IDEMPOTENCY_TABLE_SQL)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        # else: já migrada corretamente, nada pendente — não faz nada.
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    # Passo 1: schema aditivo (CREATE TABLE IF NOT EXISTS) — seguro via
    # executescript, pois nenhuma dessas tabelas tem a ambiguidade que
    # exige atomicidade cross-statement.
    # v1.0.2: detectar se a tabela do kill switch nasce AGORA (banco novo ou
    # upgrade da v1.0.0) — só então o estado inicial explícito é gravado.
    _probe = get_connection(db_path)
    try:
        _ks_table_existed = _probe.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operational_kill_switch'").fetchone() is not None
    finally:
        _probe.close()
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        if not _ks_table_existed:
            _now = time.time()
            conn.execute("INSERT OR IGNORE INTO operational_kill_switch (id, state, reason, actor, changed_at) "
                         "VALUES (1, 'DISABLED', 'estado inicial explícito na criação da tabela', 'system:init_db', ?)",
                         (_now,))
            conn.execute("INSERT INTO operational_kill_switch_events (state, reason, actor, changed_at) "
                         "VALUES ('DISABLED', 'estado inicial explícito na criação da tabela', 'system:init_db', ?)",
                         (_now,))
            conn.commit()
        _ensure_column(conn, "leads", "version", "version INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "jobs", "version", "version INTEGER NOT NULL DEFAULT 0")
        # ATLAS+RADAR (item 2 — desempenho multidimensional): colunas de
        # dimensão adicionais em `sales_events`, aditivas e retrocompatíveis
        # (linhas existentes recebem '' via DEFAULT). Nunca populadas
        # automaticamente com dado inventado — ficam vazias até que o
        # chamador informe um valor real ao registrar o evento.
        _ensure_column(conn, "sales_events", "product_id", "product_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "sales_events", "campaign_id", "campaign_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "sales_events", "funnel_id", "funnel_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "sales_events", "platform", "platform TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "sales_events", "traffic_source", "traffic_source TEXT NOT NULL DEFAULT ''")
        # CORREÇÃO (NAVIGATOR — idempotência completa): colunas aditivas
        # para a identidade completa da execução. `idempotency_key` é o
        # hash estável (mission_id, task_id, action, platform,
        # account_context, channel_id, payload completo) —
        # `fingerprint` é o MESMO valor guardado separadamente para a
        # checagem de conflito (mesma chave, conteúdo diferente).
        _ensure_column(conn, "navigator_execution_log", "idempotency_key", "idempotency_key TEXT")
        _ensure_column(conn, "navigator_execution_log", "fingerprint", "fingerprint TEXT")
        # CORREÇÃO (item 1 — replan/reexecute com NAVIGATOR real):
        # colunas aditivas para rastreabilidade de tentativa —
        # `execution_id` agrupa todas as tentativas de uma mesma
        # rodada; `attempt_id` identifica cada tentativa individual.
        _ensure_column(conn, "navigator_execution_log", "execution_id", "execution_id TEXT")
        _ensure_column(conn, "navigator_execution_log", "attempt_id", "attempt_id TEXT")
        # CORREÇÃO (item 4 — Visão Evolutiva: identidade estável e
        # versionamento longitudinal). Separa 3 conceitos antes
        # colapsados em `version`: `strategy_version` (contador GLOBAL
        # da estratégia, cresce a cada revisão, qualquer ano),
        # `annual_snapshot_version`/`revision_number` (revisão DENTRO
        # do mesmo ano — nunca [1,1] sem `revision_reason` explícito).
        _ensure_column(conn, "strategy_history", "strategy_version", "strategy_version INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "strategy_history", "annual_snapshot_version", "annual_snapshot_version INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "strategy_history", "revision_number", "revision_number INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "strategy_history", "revision_reason", "revision_reason TEXT")
        # CORREÇÃO (round 9 — continuidade da governança após restart):
        # migração aditiva para bancos criados ANTES desta correção
        # (que não tinham `canonical_registry_id` na tabela de
        # bootstrap raiz) — nunca perde o registro já existente.
        _ensure_column(
            conn, "authority_root_bootstrap", "canonical_registry_id",
            "canonical_registry_id TEXT NOT NULL DEFAULT ''",
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_navigator_execution_idempotency "
            "ON navigator_execution_log(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        # CORREÇÃO (FORGE — bloqueio persistido): coluna para severidade
        # do diagnóstico/bloqueio (persistida junto de status/evidence,
        # já existentes desde a expansão anterior).
        _ensure_column(conn, "forge_diagnostics", "severity", "severity TEXT NOT NULL DEFAULT 'UNKNOWN'")
        # MICROCORREÇÃO BF3 (isolamento multi-tenant real do
        # ClaimRegistry): migração aditiva para bancos criados ANTES
        # desta correção (tabela `evidence_claim_registry` sem as
        # colunas de escopo) — nunca perde um claim já persistido.
        _ensure_column(conn, "evidence_claim_registry", "application_id", "application_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "evidence_claim_registry", "tenant_id", "tenant_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(
            conn, "evidence_claim_registry", "governance_context_id",
            "governance_context_id TEXT NOT NULL DEFAULT ''",
        )
        # MICROCORREÇÃO (ATLAS EXPANSION 1 — recuperação de crash real
        # em GATE_IN_PROGRESS — falha real reproduzida por auditoria
        # independente): coluna de "fencing generation" — um contador
        # monotônico que nunca decresce, incrementado a cada
        # reivindicação (`ACQUIRED`→`GATE_IN_PROGRESS` ou reclaim de
        # um `GATE_IN_PROGRESS` expirado). Qualquer instância com uma
        # generation antiga (obsoleta) nunca pode finalizar/sobrescrever
        # o lease — complementa `lease_token` (que já muda a cada
        # reivindicação) com um valor TOTALMENTE ORDENADO, útil para
        # diagnóstico/auditoria (quantas vezes esta tarefa precisou
        # ser reivindicada de novo). Aditiva — nunca perde um lease já
        # persistido de uma versão anterior (linhas existentes
        # recebem `generation=0` via o DEFAULT).
        _ensure_column(conn, "graph_executor_task_leases", "generation", "generation INTEGER NOT NULL DEFAULT 0")
        # CORREÇÃO P1 (invariante dura — runner vivo NUNCA é convertido
        # para UNKNOWN_OUTCOME por atraso do heartbeat): `holder_pid`
        # registra QUEM detém o lease (na aquisição/claim);
        # `holder_seen_at` é o último sinal de vida (renovado pelo
        # MESMO UPDATE do heartbeat). A conversão por TTL em
        # `graph_executor._read_and_maybe_reclaim_lease_durable`
        # consulta estes campos como PROVA determinística de morte do
        # detentor — frescor de `leased_at` deixa de ser a única
        # evidência. Aditivo e idempotente — bancos antigos recebem
        # NULL (legado = comportamento anterior).
        _ensure_column(conn, "graph_executor_task_leases", "holder_pid", "holder_pid INTEGER")
        _ensure_column(conn, "graph_executor_task_leases", "holder_seen_at", "holder_seen_at REAL")
        # FECHAMENTO 1/6 (PID reutilizado após restart/crash): identidade
        # temporal do processo detentor — sem ela, um PID reciclado por
        # outro processo (ex.: o próprio app reiniciado com o mesmo PID)
        # era aceito como o detentor anterior e o lease ficava preso.
        # Aditiva e idempotente; linhas existentes recebem NULL e mantêm
        # o comportamento anterior (prova apenas por PID).
        _ensure_column(conn, "graph_executor_task_leases", "holder_proc_start", "holder_proc_start TEXT")
        # COMANDO 2/6 (previsão × realidade): linha de base REAL da
        # previsão — sem ela, "acertou a direção" era tautológico para
        # métricas de nível não-negativas. Aditiva; linhas antigas NULL.
        _ensure_column(conn, "learning_predictions", "baseline_value", "baseline_value REAL")
        _ensure_column(conn, "learning_records", "baseline_value", "baseline_value REAL")
        # COMANDO 5/6: validade temporal da Biblioteca Mestre (NULL = sem prazo).
        _ensure_column(conn, "master_library_entries", "valid_until", "valid_until REAL")
        # CORREÇÃO PÓS-AUDITORIA (C): vínculo evento → conteúdo (aditivo).
        # v1.0.2: valor efetivamente cobrado do orçamento (separado do custo real).
        _ensure_column(conn, "ai_api_usage", "budget_charge", "budget_charge REAL")
        _ensure_column(conn, "ai_api_usage", "reservation_id", "reservation_id TEXT")
        # v1.0.5: fingerprint da liquidação (replay divergente => conflito) e
        # campos de RECONCILIAÇÃO financeira dos incidentes.
        _ensure_column(conn, "ai_api_reservations", "settlement_fingerprint", "settlement_fingerprint TEXT")
        # v1.0.6: fingerprint da reconciliação (idempotência/conflito) e migrações de moeda.
        _ensure_column(conn, "ai_api_cost_incidents", "reconciliation_fingerprint", "reconciliation_fingerprint TEXT")
        # v1.0.7: valor EFETIVO materializado por migração (cadeia auditável), identidade da
        # migração e dados FX estruturados da reconciliação (original, taxas, ajustes).
        for _c, _d in (("converted_amount", "REAL"), ("converted_currency", "TEXT"), ("fx_migration_id", "INTEGER"),
                       ("fx_chain", "TEXT")):
            _ensure_column(conn, "ai_api_usage", _c, f"{_c} {_d}")
        for _c, _d in (("original_amount", "REAL"), ("original_currency", "TEXT"), ("fees", "REAL"),
                       ("adjustments", "REAL"), ("expected_amount", "REAL")):
            _ensure_column(conn, "ai_api_cost_incidents", _c, f"{_c} {_d}")
        # v1.0.8 (B4): incidente vinculado ao uso sem custo + backfill de upgrade (idempotente)
        # para usos órfãos de versões anteriores — todo bloqueio tem objeto reconciliável.
        _ensure_column(conn, "ai_api_cost_incidents", "usage_id", "usage_id INTEGER")
        # v1.0.9: snapshot imutável da política no evento (B3) e correlação reserva<->uso (B4).
        for _c, _d in (("policy_snapshot", "TEXT"), ("snapshot_upper_bound", "REAL"), ("snapshot_currency", "TEXT")):
            _ensure_column(conn, "ai_api_cost_incidents", _c, f"{_c} {_d}")
        _ensure_column(conn, "ai_api_reservations", "correlation_id", "correlation_id TEXT")
        # v1.0.10: fingerprint do payload do correlation_id (B1) e identidade idempotente do
        # uso sem reserva (B3) com índice único persistente.
        _ensure_column(conn, "ai_api_reservations", "correlation_fingerprint", "correlation_fingerprint TEXT")
        _ensure_column(conn, "ai_api_usage", "usage_event_id", "usage_event_id TEXT")
        _ensure_column(conn, "ai_api_usage", "usage_event_fingerprint", "usage_event_fingerprint TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_api_usage_event ON ai_api_usage (usage_event_id) "
                     "WHERE usage_event_id IS NOT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_api_reservations_correlation ON ai_api_reservations "
                     "(correlation_id) WHERE correlation_id IS NOT NULL")
        conn.execute("INSERT OR IGNORE INTO ai_api_cost_incidents (incident_id, provider, kind, reported_cost, status, "
                     "created_at, usage_id) SELECT 'inc_upgrade_usage_' || u.usage_id, u.provider, 'USAGE_COST_UNKNOWN', "
                     "'uso legado sem custo (upgrade)', 'OPEN', u.created_at, u.usage_id FROM ai_api_usage u "
                     "WHERE u.budget_charge IS NULL AND NOT EXISTS (SELECT 1 FROM ai_api_cost_incidents i "
                     "WHERE i.usage_id = u.usage_id)")
        # v1.0.8 (B2): defesa no banco com o teto da POLÍTICA (1e12), não só 1e308.
        for _tbl, _cols in (("ai_api_reservations", ("amount",)), ("ai_api_limits", ("amount",)),
                            ("ai_api_usage", ("cost", "budget_charge", "converted_amount"))):
            _cond = " OR ".join(f"(NEW.{c} IS NOT NULL AND (NEW.{c} < 0 OR NEW.{c} > 1e12))" for c in _cols)
            for _ev in ("INSERT", "UPDATE"):
                conn.execute(f"CREATE TRIGGER IF NOT EXISTS trg_money_v108_{_tbl}_{_ev.lower()} BEFORE {_ev} ON {_tbl} "
                             f"WHEN {_cond} BEGIN SELECT RAISE(ABORT, 'MONETARY_LIMIT_EXCEEDED: valor fora de [0, 1e12]'); END")
        conn.commit()
        conn.execute("CREATE TABLE IF NOT EXISTS ai_api_currency_migrations (migration_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "provider TEXT NOT NULL, from_currency TEXT NOT NULL, to_currency TEXT NOT NULL, fx_rate REAL NOT NULL "
                     "CHECK (fx_rate > 0), fx_source TEXT NOT NULL, fx_timestamp TEXT NOT NULL, evidence TEXT NOT NULL, "
                     "actor TEXT NOT NULL, created_at REAL NOT NULL)")
        _ensure_column(conn, "ai_api_currency_migrations", "migration_fingerprint", "migration_fingerprint TEXT")
        for _c, _d in (("final_amount", "REAL"), ("final_currency", "TEXT"), ("reconciliation", "TEXT"),
                       ("fx_rate", "REAL"), ("fx_source", "TEXT"), ("fx_timestamp", "TEXT"), ("source", "TEXT")):
            _ensure_column(conn, "ai_api_cost_incidents", _c, f"{_c} {_d}")
        # v1.0.3: valores monetários SEMPRE finitos e >= 0 (NaN chega como NULL;
        # ±Infinity como REAL infinito) — defesa em profundidade além da validação.
        for _tbl, _cols in (("ai_api_reservations", ("amount",)), ("ai_api_limits", ("amount",)),
                            ("ai_api_usage", ("cost", "budget_charge"))):
            _cond = " OR ".join(f"(NEW.{c} IS NOT NULL AND (NEW.{c} < 0 OR abs(NEW.{c}) > 1e308))" for c in _cols)
            if _tbl != "ai_api_usage":
                _cond = f"NEW.amount IS NULL OR {_cond}"
            for _ev in ("INSERT", "UPDATE"):
                conn.execute(f"CREATE TRIGGER IF NOT EXISTS trg_money_{_tbl}_{_ev.lower()} BEFORE {_ev} ON {_tbl} "
                             f"WHEN {_cond} BEGIN SELECT RAISE(ABORT, 'valor monetário inválido (negativo/não finito)'); END")
        _ensure_column(conn, "commercial_orders", "event_fingerprints_json",
                       "event_fingerprints_json TEXT NOT NULL DEFAULT '{}'")
        # ATLAS PIPELINE 1 (âncora durável campo-valor): migração idempotente para bancos SQLite existentes
        if _table_exists(conn, "discovery_acquisitions"):
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(discovery_acquisitions)").fetchall()
            }
            if "observation_field_digests" not in existing_cols:
                conn.execute(
                    "ALTER TABLE discovery_acquisitions ADD COLUMN observation_field_digests TEXT NOT NULL DEFAULT '{}'"
                )
        # Backfill idempotente: qualquer claim já persistido ANTES desta
        # correção (application_id='' — nunca teve escopo real) pertence
        # genuinamente ao ÚNICO tenant que já bootstrapou este banco
        # (arquitetura de bootstrap é singleton por `db_path` — ver
        # `authority_root_bootstrap.singleton_guard`) — backfill com o
        # escopo REAL desse bootstrap, nunca um valor inventado. Sem
        # bootstrap algum persistido ainda, não há nada para
        # retroalimentar (claims nesse caso, se existirem, ficam com o
        # placeholder vazio até um bootstrap real acontecer). Condição
        # `WHERE application_id = ''` torna a operação segura de
        # repetir a cada `init_db()` — já backfilado nunca é tocado de
        # novo.
        bootstrap_row = conn.execute(
            "SELECT application_id, tenant_id, canonical_registry_id FROM authority_root_bootstrap "
            "WHERE singleton_guard='ROOT'"
        ).fetchone()
        if bootstrap_row is not None and bootstrap_row["canonical_registry_id"]:
            conn.execute(
                "UPDATE evidence_claim_registry SET application_id = ?, tenant_id = ?, governance_context_id = ? "
                "WHERE application_id = ''",
                (bootstrap_row["application_id"], bootstrap_row["tenant_id"], bootstrap_row["canonical_registry_id"]),
            )
        conn.commit()
    finally:
        conn.close()

    # MICROCORREÇÃO BF3 MULTITENANT (identidade persistente
    # verdadeiramente escopada — falha real reproduzida por auditoria
    # independente): a PRIMARY KEY de `evidence_claim_registry` ainda
    # era `claim_id` sozinho — mesmo com as colunas de escopo
    # presentes e o SELECT já filtrado por elas, `INSERT OR REPLACE`
    # (chave = claim_id) permitia que appB/tenantB registrasse o
    # MESMO claim_id de appA/tenantA e SUBSTITUÍSSE/ocultasse o
    # registro de A. SQLite não permite ALTER de PRIMARY KEY
    # diretamente — reconstrói a tabela com a PRIMARY KEY composta
    # correta (application_id, tenant_id, governance_context_id,
    # claim_id), preservando todos os dados já persistidos (inclusive
    # os já backfilados no passo acima). Roda como um passo separado
    # (transação própria) DEPOIS do backfill de escopo commitar —
    # nunca reconstrói a tabela antes de application_id/tenant_id/
    # governance_context_id estarem populados corretamente.
    _migrate_claim_registry_composite_key(db_path)

    # Passo 2: migração da tabela de idempotência — atômica, com
    # recuperação segura de estados interrompidos (ver docstring acima).
    _migrate_idempotency_table_if_needed(db_path)


@contextmanager
def connect(db_path: str):
    # SQLite aceita um único writer por arquivo. Dentro deste processo,
    # serializamos a transação inteira por db_path; contenção entre
    # processos continua limitada pelo busy_timeout do próprio SQLite.
    with _db_lock(db_path):
        conn = get_connection(db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
