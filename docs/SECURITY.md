# Security

Modelo de ameaça e controles implementados (Seção 58 e testes da Seção 69).

## Controles
- RBAC + princípio de menor privilégio (`core/security/rbac.py`)
- Project/Channel Isolation (`core/isolation/isolation.py`)
- Audit Trail sanitizado (`core/security/audit.py` + `core/security/sanitize.py`)
- Sessão assinada HMAC com expiração e revogação (`core/security/auth.py`)
- Rate limiting de login (5 tentativas / 5 minutos)
- Hash de senha PBKDF2-HMAC-SHA256, 260.000 iterações

## Testes adversariais executados (`tests/test_adversarial.py`)
Vazamento de segredo em audit trail, token forjado, spoof de Owner, acesso cross-channel/cross-project, copyright UNKNOWN, clonagem de voz sem autorização, venda/receita "inventada" sem tipo declarado, táticas de crescimento proibidas (fake engagement, account farming, spam, policy bypass), publicação sem credencial real, idempotência de job duplicado, input malicioso (SQL-like) em isolamento.

## Nunca implementado propositalmente nesta fase
Bypass de CAPTCHA/2FA, scraping não autorizado, publicação real sem OAuth do Owner concluído, qualquer chamada paga de API sem credencial explicitamente configurada pelo Owner.

---

## MICROCORREÇÃO DE HARDENING (auditoria externa)

### Governed Facade (`core/governance/facade.py`)
Ponto único obrigatório para mutações de CRM, Sales, Finance e Jobs. Toda
chamada passa por: identidade válida → binding de canal/projeto válido →
RBAC → isolamento → (opcional) proteção de replay → delegação → audit
trail. Fail-closed em qualquer checagem ambígua (`InvalidIdentity`,
`InvalidBinding`, `AccessDenied`, `IsolationViolation`).

### Integridade de evidências
- `SalesTracker`/`FinanceLedger`/`CRM` agora suportam `idempotency_key`:
  eventos/leads/lançamentos duplicados (retry, replay, reprocessamento)
  nunca duplicam venda, receita, comissão ou lead.
- `GovernedFacade.ReplayGuard`: proteção adicional por `operation_id`,
  independente da idempotência de domínio.
- REAL/ESTIMATED/FORECAST/UNKNOWN seguem estruturalmente separados —
  `evaluate_scaling_readiness` sempre revalida a partir de dados
  persistidos no momento da chamada, nunca de cache.

### Concorrência
- `JobQueue` usa `threading.RLock` em todas as mutações (antes não havia
  proteção — condição de corrida podia burlar capacity control ou criar
  jobs duplicados sob concorrência).
- `CRM`, `SalesTracker`, `FinanceLedger` usam `threading.Lock` com
  check-and-insert atômico para idempotência sob concorrência.

### Service boundaries
- Serviços internos (`commercial/crm.py`, `sales_tracking.py`,
  `finance.py`, `core/jobs/queue.py`) documentados como "internos" —
  chamadores externos/API devem usar `GovernedFacade`.
- `api/bootstrap.py` expõe `GrowthStudioApp.facade` como caminho
  recomendado; Owner também opera por ele.

---

## MICROCORREÇÃO 2 (reprovado na 1ª auditoria externa — corrigido)

A primeira rodada de hardening (HARDENING_FIX) foi reprovada para
operação real. Os 6 pontos abaixo foram corrigidos nesta rodada:

### 1. Isolamento — idempotência com escopo
A idempotência antes indexava só pela `idempotency_key` crua. Agora
CRM/Sales/Finance/Jobs compõem a chave com canal/projeto/tipo de
operação (`core/governance/idempotency.py`). Reuso da mesma chave em
escopo diferente cria um registro separado; reuso no MESMO escopo com
conteúdo diferente levanta `IdempotencyConflict` em vez de devolver
silenciosamente o registro errado.

### 2. Governança — autorização obrigatória de fato
`GovernedFacade._execute()` é o único caminho real para mutações.
Operações sem canal/projeto ("globais") agora exigem
`Permission.MANAGE_GLOBAL_OPERATIONS` além da permissão de domínio —
só o Owner a possui por padrão. `Principal` só é reconstruído a partir
de sessão HMAC verificada (`principal_from_session`) — a camada HTTP
(`api/server.py`) nunca aceita role/escopo vindos do corpo da
requisição. Não há mais nenhum endpoint HTTP que mute CRM/Sales/
Finance/Jobs sem passar pela fachada.

### 3. Auditoria — disciplina de outcome
`SUCCESS` só é gravado depois que a operação de domínio retorna sem
erro. Falhas pós-autorização (`IdempotencyConflict`,
`InvalidLeadTransition` etc.) geram `ERROR` auditado — antes, a
fachada registrava SUCCESS antes mesmo de chamar o serviço interno.

### 4. Integridade comercial — receita líquida
`SalesSummary` separa `gross_revenue_*` (só eventos `sale`) de
`refunds_amount_*` (só eventos `refund`). `real_revenue`/
`estimated_revenue`/`forecast_revenue` são propriedades que retornam o
valor líquido. Uma venda REAL de 100 + reembolso REAL de 100 resulta em
receita líquida 0 (antes resultava em 200, pois refund e sale eram
somados no mesmo acumulador).

### 5. Persistência — armazenamento durável real
CRM, SalesTracker, FinanceLedger, JobQueue e AuditTrail aceitam
`db_path` e passam a persistir de fato (write-through + reload no
boot) em vez de só ter tabelas criadas e não usadas. `api/bootstrap.py`
conecta tudo a `SETTINGS.db_path` por padrão.

### 6. Acesso Owner e painel
`/login` e `/dashboard` são páginas HTML funcionais (antes só existiam
endpoints JSON). `main.py set-owner-password` completa o fluxo de
configuração segura. `build_app()` carrega `OWNER_EMAIL`/
`OWNER_PASSWORD_HASH` do `.env` por padrão. O painel é alimentado com
dados reais persistidos (sales/finance/CRM agregados globalmente);
corrigido também um bug em que "leads" mostrava contagem de eventos de
funil em vez da contagem real de registros do CRM.

### Testes de regressão adicionados
`tests/test_scoped_idempotency.py` (14), `tests/test_governance_v2.py`
(11), `tests/test_revenue_integrity.py` (8), `tests/test_persistence.py`
(12), `tests/test_http_flow.py` (18) — 63 novos testes cobrindo
especificamente cada um dos 6 pontos.

---

## MICROCORREÇÃO 3 (reprovado na 2ª auditoria externa — corrigido)

Cinco pendências apontadas na auditoria da entrega V2:

### 1. Isolamento e autorização do painel
`/dashboard` e `/api/dashboard` aceitavam sessão restrita a um canal
mas retornavam dados de TODOS os canais. Corrigido:
`GovernedFacade.get_dashboard_view()` resolve o escopo real da sessão
verificada (`resolve_readable_channels`) e filtra CRM/vendas/finanças
por ele. Agregado global exige `Permission.VIEW_GLOBAL_DASHBOARD`
explícita (só o Owner por padrão) — nunca inferido de canal/papel/
escopo que o cliente declare na requisição.

### 2. Persistência atômica e idempotência
Era possível duplicar leads com duas instâncias no mesmo banco usando
a mesma chave, ou após falha entre a gravação do registro e da
idempotência seguida de retry. Corrigido: `CRM`, `SalesTracker`,
`FinanceLedger` e `JobQueue` agora gravam o registro de negócio + sua
idempotência na MESMA transação SQLite (`BEGIN IMMEDIATE` manual,
autocommit desligado). Conflito de PRIMARY KEY na tabela
`idempotency_records` (nunca mais `INSERT OR REPLACE` ali) reverte a
transação inteira; a resolução do conflito sempre consulta o registro
JÁ PERSISTIDO no banco, nunca o cache em memória da instância. Qualquer
outra falha entre as duas gravações também reverte tudo e fecha a
conexão (bug real encontrado e corrigido durante a validação: uma
exceção que não fosse `IntegrityError` deixava a conexão aberta com
lock preso).

### 3. Revogação durável de sessões
Uma sessão encerrada por logout voltava a ser aceita por uma NOVA
instância do serviço de autenticação (revogação só existia em memória).
Corrigido: `SessionStore` persiste revogações em SQLite
(`revoked_sessions`); `is_revoked()` sempre consulta o banco além do
cache local — uma sessão revogada por qualquer instância fica inválida
em todas. Logout também limpa o cookie no navegador (`Max-Age=0`).

### 4. Separação dos indicadores
Uma venda FORECAST aparecia contada junto com vendas REAIS no painel.
Corrigido: `SalesSummary` separa contagens e valores REAL/ESTIMATED/
FORECAST/UNKNOWN completamente; `.sales`/`.revenue` (campos genéricos)
significam SEMPRE e SOMENTE REAL. Painel HTML e JSON mostram cada tipo
de evidência em campo próprio e rotulado; War Room nunca apresenta
previsão/estimativa como resultado comprovado.

### 5. Carregamento da configuração
O guia orientava preencher `.env`, mas nada carregava o arquivo de
fato. Corrigido: `core/config.py` carrega o `.env` (cwd, depois raiz do
projeto) ANTES de `SETTINGS` ser construído — parser literal (nunca
expande `$VAR` nem executa comandos), precedência do ambiente real
preservada, hashes com `$` preservados exatamente.

### Testes de regressão adicionados (rodada 3)
`tests/test_dashboard_isolation.py` (14), `tests/test_atomic_idempotency.py`
(14), `tests/test_session_revocation.py` (7), `tests/test_indicator_separation.py`
(11), `tests/test_dotenv_loading.py` (9) — 57 novos testes.

---

## MICROCORREÇÃO 4 (reprovado na 3ª auditoria externa — corrigido)

Quatro pendências apontadas na auditoria da entrega V3:

### 1. Idempotência sem conflito entre módulos
`idempotency_records` usava `composite_key TEXT PRIMARY KEY` — uma
string concatenada SEM incluir o domínio. A mesma `idempotency_key` no
mesmo canal usada por CRM e por Sales colidia, bloqueando uma operação
legítima. Corrigido: colunas próprias
(`domain, operation, channel_id, project_id, idempotency_key`) com
PRIMARY KEY composta de verdade — sem concatenação de string. Migração
automática (`core/models/db.py: _migrate_idempotency_table_if_needed`)
detecta o schema antigo, renomeia a tabela, recria com o schema novo,
e migra cada registro reconstruindo `operation`/`channel_id`/
`project_id` a partir do formato antigo — preservando todos os
registros e a proteção contra duplicação. Nunca apaga o banco.

### 2. Coerência entre memória e banco após falhas
`create_lead`/`record_event`/`record` sem `idempotency_key` colocavam
o objeto no cache em memória ANTES de persistir — uma falha na
gravação deixava o registro "existir" só em memória. `transition()` e
as mutações de `Job` (`start`/`complete`/`fail`/`cancel`) mutavam o
objeto compartilhado in-place ANTES de persistir. Corrigido: toda
mutação persiste primeiro; o cache só é tocado (via `dataclasses.replace`,
nunca mutação in-place) depois da confirmação. Falha = nem memória nem
banco mudam.

### 3. Leituras atualizadas entre instâncias
Leituras (`get`/`by_channel`/`summarize`/`snapshot`/`all`) liam apenas
o cache carregado uma vez no `__init__` — uma escrita de outra
instância só aparecia após recriar a aplicação. Corrigido: com
`db_path` configurado, essas leituras consultam o banco diretamente a
cada chamada (fonte de verdade); `transition`/`start`/`complete`/
`fail`/`cancel` também relêem o estado atual do banco antes de mutar,
evitando sobrescrever uma mudança feita por outra instância.

### 4. Classificação de evidência no War Room
`based_on_real_data` era inferido apenas de "o valor é diferente de
UNKNOWN?" — uma receita FORECAST (presente, não-UNKNOWN) era marcada
como `based_on_real_data=True`. Corrigido: `WarRoomAnswer` agora carrega
`evidence_type` explícito ("REAL"|"ESTIMATED"|"FORECAST"|"UNKNOWN"|"N/A"),
escolhido pelo chamador para cada pergunta — nunca inferido da presença
do valor — e `based_on_real_data` é sempre exatamente
`evidence_type == "REAL"`.

### Testes de regressão adicionados (rodada 4)
`tests/test_cross_domain_idempotency.py` (15, incluindo migração V3→V4),
`tests/test_memory_db_coherence.py` (12), `tests/test_cross_instance_fresh_reads.py`
(14), `tests/test_war_room_evidence_classification.py` (9) — 50 novos testes.

---

## MICROCORREÇÃO 5 (reprovado na 4ª auditoria externa — corrigido)

Três pendências em transações e concorrência apontadas na auditoria da
entrega V4:

### 1. Migração atômica e recuperável
A migração V3→V4 fazia rename/create/cópia/drop como passos SEPARADOS
(não atômicos). Uma falha no meio podia deixar a tabela nova
incompleta; na reinicialização, o código via `idempotency_records` já
existindo com o schema novo e concluía (erradamente) que a migração
estava completa — permitindo duplicação. Corrigido: a migração inteira
roda dentro de UMA transação SQLite explícita
(`core/models/db.py: _migrate_idempotency_table_if_needed`), usando
`get_autocommit_connection` + `BEGIN IMMEDIATE` manual (nunca
`executescript`, que força commit implícito e quebra atomicidade).
SQLite tem DDL totalmente transacional — RENAME/CREATE/DROP TABLE são
revertidos por ROLLBACK — então uma falha em qualquer ponto deixa o
banco anterior exatamente como estava, com sua idempotência intacta.
Nenhuma linha que falhe ao converter ou colida é descartada
silenciosamente (`MigrationBlocked` aborta a transação inteira). Se
uma tabela de staging órfã de uma migração interrompida por uma
versão de código ANTERIOR a esta correção for encontrada, o sistema
tenta recuperação segura re-copiando a partir dela (fonte de verdade
ainda íntegra); se o formato for inesperado, bloqueia a inicialização
com diagnóstico sanitizado em vez de prosseguir às cegas.

### 2. Tratamento completo de falhas no commit
`conn.commit()` ficava num bloco `else:` FORA do try/except em todos
os quatro serviços persistentes — uma falha no PRÓPRIO commit (não na
inserção) escapava sem rollback nem fechamento da conexão, deixando
uma transação aberta e travando outros escritores. Corrigido: `BEGIN`,
as gravações e o `COMMIT` agora vivem dentro do MESMO try/except em
CRM, Sales, Finance e Jobs — qualquer falha (inserção ou o commit em
si) aciona rollback + close antes de propagar; erro de limpeza nunca
mascara o erro original.

### 3. Atualizações e capacidade atômicas entre instâncias
Duas falhas de "check depois escrever" como passos separados: (a)
`transition()`/mutações de `Job` liam o estado e escreviam em dois
passos, permitindo que uma atualização baseada em leitura obsoleta
sobrescrevesse uma transição já confirmada por outra instância; (b)
`start()` verificava capacidade e depois escrevia RUNNING
separadamente, permitindo que duas instâncias iniciassem jobs
simultaneamente além do limite. Corrigido: colunas `version` em
`leads`/`jobs` (migração aditiva via `ALTER TABLE ADD COLUMN`);
`transition`/`complete`/`fail`/`cancel` usam UPDATE condicionado à
versão lida, com retry (até 5 tentativas) relendo o estado fresco em
caso de conflito, e recusam agir sobre um job/lead já em estado
terminal. `start()` verifica capacidade, reserva a vaga e transiciona
para RUNNING em UM ÚNICO UPDATE atômico (subquery de contagem na
própria cláusula WHERE) — testado com threads reais sincronizadas por
barreira disputando o mesmo limite compartilhado.

### Testes de regressão adicionados (rodada 5)
`tests/test_migration_atomicity.py` (7), `tests/test_commit_failure_handling.py`
(13), `tests/test_atomic_concurrency.py` (8) — 28 novos testes,
incluindo concorrência real com threads/barreiras (confirmados
estáveis em múltiplas execuções consecutivas).

---

## MICROCORREÇÃO 6 (reprovado na 5ª auditoria externa — corrigido)

Escopo estritamente limitado à recuperação de bancos antigos após
migração V4 interrompida. Dois problemas reproduzidos pela auditoria:

### 1. Reconhecimento incorreto dos estados legados
A V4 real deixava a tabela antiga com o nome `idempotency_records_v3_old`
— a V5 só procurava `idempotency_records_migration_staging` (o nome
que a própria V5 usa internamente), então nunca reconhecia o estado
real deixado por uma V4 interrompida, e caía de volta no bug original
(tratava a existência de `idempotency_records` com colunas novas como
prova de migração concluída). Corrigido: `_LEGACY_TABLE_NAMES` agora
reconhece AMBOS os nomes históricos, e mais de uma tabela legada
presente simultaneamente é suportado (todas são lidas e mescladas).

### 2. Perda de idempotência confirmada durante a recuperação
A recuperação anterior **apagava** a tabela nova (que podia já ter
operações legítimas confirmadas *depois* da interrupção, antes da
recuperação rodar) e reconstruía só a partir da tabela antiga —
perdendo esses registros e permitindo duplicação no retry. Corrigido:
`_run_unified_idempotency_recovery` agora **mescla** todas as fontes
conhecidas (tabelas legadas com qualquer nome histórico + registros já
confirmados no schema novo). Identidades equivalentes (mesmo
`record_id` e `fingerprint`) são consolidadas silenciosamente;
identidades com conteúdo incompatível entre fontes bloqueiam a
recuperação inteira (`MigrationBlocked`) — nunca escolhe uma versão
arbitrariamente, nunca sobrescreve, nunca descarta em silêncio.

### Mecanismo
Detecção + leitura de todas as fontes + construção do resultado
mesclado + validação de contagem + troca definitiva de tabelas vivem
dentro de UMA transação SQLite explícita (reaproveitando o mecanismo
`BEGIN IMMEDIATE`/commit-dentro-do-try/rollback+close já corrigido na
V5) — falha em qualquer ponto (cópia, troca de tabelas, ou o commit em
si) reverte tudo, preservando todas as fontes originais intactas.

### Testes de regressão adicionados (rodada 6)
`tests/test_migration_v6_recovery.py` (12 novos: nome real da tabela
V4, registros confirmados após interrupção preservados, duas tabelas
legadas simultâneas, conflito genuíno entre fontes bloqueado com
segurança, falhas injetadas em cópia/troca/commit, duas inicializações
consecutivas, cobertura dos 4 domínios). Dois testes existentes de
`test_migration_atomicity.py` foram ajustados (não enfraquecidos —
mesma asserção, mesmo objetivo) para refletir os novos nomes internos
de tabela de trabalho usados pelo algoritmo de mesclagem.

---

## ATLAS+RADAR (correção incremental sobre o ATLAS V1)

Implementa Radar Permanente de Oportunidades Comerciais, desempenho
multidimensional e persistência de aprovações do Owner — ver
`docs/ATLAS_RADAR.md` para detalhes completos.

Extensões de schema (aditivas, seguindo o padrão já estabelecido nas
Microcorreções 5/6): colunas dimensionais em `sales_events`
(`product_id`, `campaign_id`, `funnel_id`, `platform`, `traffic_source`);
tabela `owner_approval_requests` com índice único parcial em
`idempotency_key`. Nenhuma tabela existente foi removida ou alterada de
forma destrutiva.

Durante a implementação, um bug real foi encontrado e corrigido antes
da entrega: `SalesTracker.summarize_filtered()` não aplicava o filtro
de `channel_id` no modo em memória (só no modo persistente) — corrigido
e validado em ambos os modos antes de prosseguir.

Testes de regressão adicionados: `tests/test_atlas_radar_discovery.py` (27),
`tests/test_atlas_multidimensional_performance.py` (18),
`tests/test_atlas_approval_persistence.py` (20, incluindo concorrência
real com threads — confirmada estável em 10 execuções consecutivas),
`tests/test_atlas_radar_integration.py` (15) — 80 novos testes.
