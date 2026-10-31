# CHANGELOG — Microcorreção BF3: Isolamento Multi-Tenant Real

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BF3_V1_FINAL`
(1578 testes). Corrige a falha real reproduzida pela auditoria,
preservando integralmente BF1/BF2/BF3 restante.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1591  PASS: 1591  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~84-92s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, arquivos órfãos: 0
```

**1591 testes** (1578 preservados + **13 testes novos**).

## Falha real confirmada

Usando o MESMO `db_path`, `appA/tenantA` registra um claim;
`appB/tenantB` conseguia executar `restore_for_app()` e resolver o
claim de A.

**Causa raiz dupla**:
1. `restore_for_app()` aceitava o `application_id`/`tenant_id` do
   CHAMADOR sem validar contra o que foi genuinamente persistido em
   `authority_root_bootstrap` — apenas sobrescrevia
   `authority_registry.application_id`/`tenant_id` com o que o
   chamador afirmava, sem nunca comparar.
2. `ClaimRegistry` carregava TODOS os claims de
   `evidence_claim_registry` sem filtro de escopo algum — a tabela
   nem tinha colunas de escopo.

## Correção — duas camadas, fail-closed

### Camada 1 — `restore_for_app()` valida contra o persistido

Novo `AuthorityRegistry.persisted_bootstrap_scope()` — devolve
`(application_id, tenant_id)` genuinamente persistido em
`authority_root_bootstrap` para aquele `db_path`. `restore_for_app()`
agora SEMPRE compara o que o chamador está pedindo contra esse valor
persistido — qualquer divergência (application_id OU tenant_id)
levanta `GovernanceContextError(error_code="REGISTRY_CONTEXT_MISMATCH")`,
ANTES de restaurar qualquer coisa. A checagem `NOT_BOOTSTRAPPED`
(já existente) continua vindo antes, preservada sem alteração.

### Camada 2 — `evidence_claim_registry` escopado (defesa em profundidade)

Nova tabela com 3 colunas: `application_id`, `tenant_id`,
`governance_context_id`. `ClaimRegistry.__init__()` agora RECEBE esse
escopo como parâmetros do construtor — fixado ANTES de
`_load_persisted_claims()` rodar (nunca setado depois, o que
deixaria uma janela sem filtro). Todo `SELECT`/`INSERT` contra
`evidence_claim_registry` é filtrado pelos 3 campos. Isso protege
mesmo um `ClaimRegistry(db_path=mesmo_banco)` construído DIRETAMENTE,
fora de `governance_context.py` — nunca depende apenas da validação
da Camada 1.

`governance_context.py` atualizado nos DOIS pontos
(`bootstrap_for_app()`/`restore_for_app()`) para passar
`application_id`/`tenant_id`/`governance_context_id` reais ao
construir `ClaimRegistry`.

### Migração aditiva + backfill idempotente

`core/models/db.py`: 3 `_ensure_column()` para bancos criados ANTES
desta correção (tabela sem as colunas de escopo) — nunca perde um
claim já persistido. Backfill: claims com `application_id=''`
(nunca tiveram escopo real) recebem o escopo do ÚNICO bootstrap já
persistido naquele banco (arquitetura de bootstrap é singleton por
`db_path`) — nunca um valor inventado. Condição `WHERE
application_id = ''` torna a operação segura de repetir a cada
`init_db()` (testado rodando 3× seguidas, nunca duplica/corrompe).

## Validado com o cenário negativo exato reportado

`tests/test_microcorrecao_bf3_multitenant_isolation.py`:
- `restore_for_app()` com `application_id` diferente → rejeitado
  (`REGISTRY_CONTEXT_MISMATCH`).
- `restore_for_app()` com `tenant_id` diferente → rejeitado.
- `restore_for_app()` com o MESMO escopo → continua funcionando
  (caso legítimo preservado).
- **Cenário negativo completo**: appA registra claim → appB tenta
  `restore_for_app()` (bloqueado) → appB tenta `ClaimRegistry`
  direto com seu próprio escopo (nunca resolve o claim de A, `all()`
  vazio, `matches()` falso) → appA com o MESMO escopo ainda vê seu
  próprio claim (controle positivo).
- Isolamento mantido mesmo com `governance_context_id` IGUAL mas
  `application_id`/`tenant_id` diferentes (os 3 campos precisam
  bater).
- Migração: claim legado corretamente backfilado com o escopo real
  do bootstrap existente; idempotente rodando 3× seguidas; banco
  fresco já nasce com as colunas.

## Preservação confirmada explicitamente

Reexecutei os 13 arquivos de teste dedicados de BF1/BF2/BF3 — **175
testes, todos passando sem alteração**. Nenhuma outra área do código
foi tocada além de `core/evidence/claim_registry.py`,
`core/evidence/governance_context.py` (as 2 construções de
`ClaimRegistry` + a validação em `restore_for_app()`),
`core/evidence/authority_registry.py` (1 método novo, aditivo) e
`core/models/db.py` (schema + migração, aditivos).

## Arquivos alterados
- `core/evidence/authority_registry.py` — `persisted_bootstrap_scope()`
- `core/evidence/governance_context.py` — validação em
  `restore_for_app()`; escopo passado a `ClaimRegistry` nos 2 pontos
- `core/evidence/claim_registry.py` — escopo no construtor, usado em
  toda leitura/escrita
- `core/models/db.py` — colunas de escopo + migração + backfill

## Arquivos novos
- `tests/test_microcorrecao_bf3_multitenant_isolation.py` (13 testes)

## Pendências reais (honestamente declaradas)
- A arquitetura de bootstrap permanece singleton por `db_path` (uma
  única `authority_root_bootstrap` por arquivo) — o escopo de
  `evidence_claim_registry` é defesa em profundidade dentro dessa
  arquitetura, não uma mudança para multi-tenant-por-arquivo genuíno;
  isso é consistente com o pedido (corrigir a falha real, sem
  expandir escopo)
- `pytest` continua indisponível neste ambiente
