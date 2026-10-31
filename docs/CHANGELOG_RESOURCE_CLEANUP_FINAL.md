# CHANGELOG — Microcorreção Final: ResourceWarnings Residuais (Resource Cleanup)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ROUND9_HANG_FIX`
(1212 testes, 1212/1212 PASS, hang já resolvido). Corrige os
`ResourceWarning`s residuais — conexões SQLite não fechadas,
especialmente nos testes de migração.

## Causa raiz encontrada e corrigida

Vários testes de migração (`test_migration_v6_recovery.py`,
`test_migration_atomicity.py`) usavam
`with sqlite3.connect(self.db_path) as conn:` diretamente.

**Fato pouco conhecido sobre o `sqlite3.Connection` como context
manager**: `with sqlite3.connect(...) as conn:` só cuida de
COMMIT/ROLLBACK automático ao sair do bloco — **nunca fecha a
conexão**. Isso é diferente de `core.models.db.connect()` (um
context manager PRÓPRIO, decorado com `@contextmanager`, que sempre
executa `finally: conn.close()`). As conexões abertas por esse padrão
ficavam vivas até a coleta de lixo eventualmente as finalizar —
gerando os `ResourceWarning`s residuais.

**Corrigido**: todas as 7 ocorrências de
`with sqlite3.connect(self.db_path) as conn: conn.row_factory =
sqlite3.Row` foram substituídas por `with db_module.connect(self.db_path)
as conn:` (que já define `row_factory` automaticamente e SEMPRE fecha
— em sucesso, erro, e rollback).

## Auditoria completa realizada

Usando análise AST (não apenas busca textual), verifiquei TODA
função em produção e testes que abre uma conexão SQLite bruta
(`sqlite3.connect()`/`get_connection()`/`get_autocommit_connection()`)
diretamente: cada uma ou (a) fecha explicitamente dentro da própria
função, ou (b) devolve a conexão/fábrica para o chamador assumir a
responsabilidade (`return conn`/`return factory`) — e nesses casos,
confirmei que o CHAMADOR de fato fecha (ex: `init_db()` e
`_migrate_idempotency_table_if_needed()` em `core/models/db.py`, ambos
com `finally: conn.close()`).

**Nenhuma outra conexão sem fechamento foi encontrada** em toda a base
de código (produção + testes).

Durante a própria escrita do teste de regressão desta correção,
encontrei e corrigi um vazamento adicional (irônico): o próprio
arquivo de teste novo abria arquivos de código-fonte com `open(path).read()`
sem fechar — corrigido para `with open(path) as f: f.read()`.

## Resultado real — execução ÚNICA, sem divisão, com verificação máxima

```
Execução normal:              1212 PASS, 0 FAIL, 0 ERROR, ~39-44s
Execução -W error::ResourceWarning (avisos viram exceções): 1215 PASS, 0 FAIL — TODOS passam
  mesmo com a checagem mais rigorosa possível (qualquer conexão
  esquecida abortaria o teste imediatamente)
ResourceWarnings (captura explícita + gc.collect() triplo): 0
Threads finais: 0
Arquivos órfãos (-wal/-shm) em /tmp: 0
```

**1215 testes** (1212 preservados + **3 testes novos** em
`tests/test_correction_final_resource_cleanup.py`), **1215 PASS, 0
FAIL, 0 ERROR**, em uma ÚNICA invocação de `python3 -m unittest
discover -s tests`.

## Arquivos alterados

- `tests/test_migration_v6_recovery.py` — 5 ocorrências de
  `with sqlite3.connect(...)` → `with db_module.connect(...)`
- `tests/test_migration_atomicity.py` — 2 ocorrências, mesma correção
- `tests/test_correction_final_resource_cleanup.py` (novo, 3 testes)

**Nenhuma funcionalidade aprovada foi alterada.** Nenhuma mudança de
arquitetura. A correção do travamento (modo WAL + `busy_timeout`) e a
correção de continuidade/restart das rodadas anteriores foram
preservadas integralmente e revalidadas sem modificação.

## Teste de regressão específico (item explicitamente pedido)

`tests/test_correction_final_resource_cleanup.py`:
- `test_no_bare_with_sqlite3_connect_blocks` — checagem estática:
  nenhum arquivo (produção ou teste) pode conter
  `with sqlite3.connect(...)` diretamente — sempre um bug, nunca
  fecha sozinho
- `test_every_function_that_opens_a_raw_connection_closes_it_or_hands_it_off`
  — checagem estática (AST) mais ampla: toda função que abre uma
  conexão bruta precisa fechar explicitamente OU devolver a
  responsabilidade ao chamador
- `test_migration_tests_produce_no_resource_warnings` — checagem em
  tempo de execução: roda os 5 arquivos de teste de migração/
  idempotência em um subprocesso com `-W error::ResourceWarning`;
  qualquer conexão esquecida faria o subprocesso falhar imediatamente

Essas checagens pegam automaticamente qualquer reintrodução futura do
padrão perigoso, mesmo em código ainda não escrito.

## Pendências reais
- `pytest` continua indisponível neste ambiente.
- `ClaimRegistry` ainda não é persistido (pendência já declarada em
  rodadas anteriores, fora do escopo desta microcorreção pontual).
- Módulo YouTube e Omnichannel Campaign Intelligence não implementados,
  conforme instruído em todas as rodadas.
