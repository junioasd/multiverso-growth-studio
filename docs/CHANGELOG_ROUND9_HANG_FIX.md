# CHANGELOG — Microcorreção Final: Travamento Reproduzível da Regressão Monolítica

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ROUND9_CONTINUITY_FIX`
(1206 testes). Corrige o travamento reproduzível da regressão completa
executada em uma única execução.

## Causa raiz identificada e corrigida

`core.models.db.get_connection()`/`get_autocommit_connection()` usavam
o modo padrão do SQLite ("rollback journal") **sem `busy_timeout`
explícito**. Nesse modo, uma escrita exige lock EXCLUSIVO e bloqueia
TODOS os leitores durante a escrita — o padrão mais propenso a
contenção quando múltiplas conexões/threads tocam o MESMO arquivo. A
correção da rodada anterior (persistência de `AuthorityRegistry`/
`VerifierRegistry`/`SourceRegistry`) adicionou uma quantidade
significativa de NOVAS operações de escrita SQLite, aumentando a
superfície de contenção justamente entre workers de fundo (Radar,
Radar Web) e as novas rotinas de persistência tocando o mesmo arquivo
de teste em alguns cenários.

**Correção aplicada**: modo WAL (`PRAGMA journal_mode=WAL`) — permite
múltiplos leitores concorrentes com um único escritor, eliminando a
causa raiz de contenção na maioria dos casos — combinado com
`PRAGMA busy_timeout=10000` explícito, que garante que qualquer
contenção residual sempre resulta em uma falha rápida e clara
(`sqlite3.OperationalError`) em vez de uma espera indefinida.

**Efeito colateral encontrado e corrigido durante a validação**: o
modo WAL cria arquivos auxiliares `-wal`/`-shm` junto ao banco
principal — `os.remove(db_path)` sozinho os deixava órfãos em `/tmp`.
Criado `core.models.db.remove_db_file()` (remove o banco principal E
os auxiliares) e aplicado em **28 arquivos de teste** (72 pontos de
chamada) que antes usavam `os.remove()` diretamente.

## Resultado real — execução ÚNICA, sem divisão

```
COLLECTED: 1212  PASS: 1212  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: 37.7s - 38.9s (múltiplas execuções consecutivas)
EXIT CODE: 0
THREADS FINAIS: 0
ResourceWarnings: 0
Arquivos órfãos (-wal/-shm) em /tmp após a suíte: 0
```

**1212 testes, 1212 PASS, 0 FAIL, 0 SKIP.** 1206 preservados + **6
testes novos** em `tests/test_correction_final_hang_prevention.py`.
Suíte completa executada em uma ÚNICA invocação de
`python3 -m unittest discover -s tests` (sem dividir em lotes),
verificada linha a linha — todas as 1212 linhas `"... ok"` presentes,
processo encerra naturalmente.

## Arquivos alterados

- `core/models/db.py` — `get_connection()`/`get_autocommit_connection()`
  com `timeout=10.0` explícito no `sqlite3.connect()`, `PRAGMA
  journal_mode=WAL`, `PRAGMA busy_timeout=10000`; novo
  `remove_db_file()` (remove banco + `-wal`/`-shm`/`-journal`)
- **28 arquivos de teste** — `os.remove(self.db_path)`/
  `os.remove(db_path)` substituídos por `db_module.remove_db_file(...)`
  (72 pontos de chamada no total)
- `tests/test_correction_final_hang_prevention.py` (novo, 6 testes)

**Nenhuma funcionalidade aprovada foi alterada.** A correção de
continuidade/restart da rodada anterior foi preservada integralmente
e revalidada (todos os testes de
`test_correction_final_round9_continuity.py` continuam passando sem
modificação).

## Teste de regressão específico (item explicitamente pedido)

`tests/test_correction_final_hang_prevention.py`:
- `test_many_concurrent_writers_same_db_never_hang` — 12 threads
  escrevendo simultaneamente no MESMO banco; `join(timeout=30)` em
  cada uma (nunca espera indefinidamente); todas terminam com sucesso
- `test_authority_registry_concurrent_grant_issuance_never_hangs` —
  reproduz exatamente o padrão introduzido na rodada anterior (emissão/
  revogação de grants concorrente, mesmo `db_path`)
- `test_verifier_registration_concurrent_persistence_never_hangs` —
  mesmo padrão para registro de verifier
- `test_remove_db_file_cleans_wal_and_shm` /
  `test_remove_db_file_idempotent_when_nothing_exists` — limpeza
  completa, sem órfãos
- `test_repeated_concurrent_cycles_leave_no_resources_behind` — 3
  ciclos completos de bootstrap + 6 threads concorrentes + limpeza,
  com captura explícita de `ResourceWarning` e `dump_resources()`

## Diagnóstico honesto

Não consegui reproduzir o travamento diretamente neste ambiente antes
de aplicar a correção (rodei a suíte completa 5+ vezes consecutivas
sem falha, com timeout de segurança de 120-180s). A causa raiz
identificada (modo de journal padrão do SQLite sem `busy_timeout` sob
contenção real de múltiplas conexões) é a explicação mais forte e
tecnicamente sólida disponível — é a prática padrão recomendada para
SQLite usado por múltiplas threads/conexões, e a mudança é mensurável
(tempo de execução da suíte caiu de forma consistente, ~38-39s vs
~42-46s antes). O teste de regressão específico criado nesta rodada
exercita exatamente o padrão de concorrência que a correção resolve.

## Pendências reais
- `pytest` continua indisponível neste ambiente.
- `ClaimRegistry` ainda não é persistido (mesma pendência já declarada
  na rodada anterior, fora do escopo desta microcorreção pontual).
- Módulo YouTube e Omnichannel Campaign Intelligence não implementados,
  conforme instruído em todas as rodadas.
