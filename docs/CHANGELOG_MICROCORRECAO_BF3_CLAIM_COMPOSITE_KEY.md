# CHANGELOG — Microcorreção BF3 MULTITENANT: Identidade Persistente Escopada (Composite Primary Key)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BF3_MULTITENANT_ISOLATION`
(1591 testes). Corrige a falha real reproduzida pela auditoria,
preservando integralmente BF1/BF2/BF3 restante.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1600  PASS: 1600  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~85-87s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, arquivos órfãos: 0
```

**1600 testes** (1591 preservados + **9 testes novos**).

## Falha real confirmada

`evidence_claim_registry` ainda usava `claim_id` como `PRIMARY KEY`
GLOBAL — mesmo com as colunas de escopo (application_id/tenant_id/
governance_context_id) já presentes e o `SELECT` já filtrado por
elas (correção anterior), a identidade de ESCRITA continuava sendo
só `claim_id`. `_persist_claim()` usa `INSERT OR REPLACE`, que
resolve conflito pela PRIMARY KEY — então `appB/tenantB` registrando
o MESMO `claim_id` de `appA/tenantA` SUBSTITUÍA a linha de A por
completo. Reproduzido antes de corrigir: após ambos registrarem
`claim_id="claim_same"`, apenas 1 linha sobrevivia no banco (deveria
ser 2), e A deixava de conseguir resolver seu próprio claim.

## Correção — identidade composta real

`PRIMARY KEY (application_id, tenant_id, governance_context_id,
claim_id)` — os QUATRO campos juntos formam a identidade real de um
claim persistido. O mesmo `claim_id` agora coexiste livremente em
escopos diferentes, sem colisão. `INSERT OR REPLACE` continua
funcionando corretamente DENTRO do mesmo escopo (reregistrar o
próprio claim atualiza em vez de duplicar — testado).

### Migração — reconstrução de tabela, atômica e idempotente

SQLite não permite `ALTER TABLE` para mudar `PRIMARY KEY`
diretamente. `_migrate_claim_registry_composite_key()`:
1. Checa a PK atual (`PRAGMA table_info`) — se já é a composta,
   sai sem fazer nada (idempotência).
2. A checagem acontece DENTRO de `BEGIN IMMEDIATE` (adquire o lock
   de escrita antes de checar) — uma segunda chamada concorrente,
   depois de esperar o lock, vê o schema já migrado e sai limpo.
3. Cria tabela nova com a PK correta, copia todos os dados, dropa a
   antiga, renomeia a nova — tudo em UMA transação; erro no meio
   reverte tudo via `rollback()`, nunca deixa o banco num estado
   intermediário.
4. Roda DEPOIS do passo de backfill de escopo (correção anterior)
   commitar — nunca reconstrói a tabela antes de application_id/
   tenant_id/governance_context_id estarem populados corretamente.

## Validado com o teste negativo exato exigido

`tests/test_microcorrecao_bf3_claim_composite_key.py`:
- A registra `claim_same`; B registra `claim_same` também; após
  RESTART REAL (novas instâncias, mesmo `db_path`) — A resolve
  SOMENTE o conteúdo de A, B resolve SOMENTE o conteúdo de B, com
  **duas linhas persistidas** confirmadas diretamente via SQL.
- Ordem inversa (B primeiro, A depois) — B permanece intacto.
- Reregistrar o PRÓPRIO claim_id no MESMO escopo ainda atualiza
  corretamente (nunca duplica) — confirma que a correção não quebrou
  o caso de uso legítimo de atualização.
- Três tenants com o MESMO claim_id — três linhas, três conteúdos
  isolados.
- Migração testada em 3 cenários: banco da versão IMEDIATAMENTE
  anterior (colunas de escopo já presentes, PK antiga), banco BEM
  ANTIGO (sem nenhuma coluna de escopo — cadeia completa de migração
  numa única passada), e banco fresco (já nasce com a PK composta).
  Idempotência confirmada rodando a migração 4× seguidas.

## Preservação confirmada explicitamente

Reexecutei os 14 arquivos de teste dedicados de BF1/BF2/BF3 — **188
testes, todos passando sem alteração**. Nenhuma outra área do código
foi tocada além de `core/models/db.py` (schema + nova função de
migração).

## Arquivos alterados
- `core/models/db.py` — `evidence_claim_registry` com PRIMARY KEY
  composta (schema base); nova `_migrate_claim_registry_composite_key()`
  chamada em `init_db()`

## Arquivos novos
- `tests/test_microcorrecao_bf3_claim_composite_key.py` (9 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
