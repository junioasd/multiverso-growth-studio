# CHANGELOG — Microcorreção ATLAS EXPANSION 1: Proteção Multi-Instância Real

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1_FIX`
(1657 testes). Corrige a falha real reproduzida pela auditoria,
preservando integralmente V1 + demais correções.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1666  PASS: 1666  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~87-91s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0
compileall: OK
```

**1666 testes** (1657 preservados + **9 testes novos**).

## Falha real confirmada

A proteção contra execução concorrente de `(mission_id, task_id)`
(`_inflight`) era local à INSTÂNCIA de `GraphExecutor`. Com duas
instâncias (dois processos, ou dois objetos `GraphExecutor` no mesmo
processo), nenhuma sabia da outra. Em memória, `JobQueue.try_start()`
não validava `_STARTABLE_STATES` (só o caminho durável fazia),
permitindo reiniciar um job já `RUNNING`. Em SQLite/restart, uma nova
instância (registro em memória vazio) não tinha como saber que uma
execução anterior ainda estava — ou tinha ficado — em andamento; após
`recover_stuck_jobs()` mudar o `Job` para `RETRYING`, uma nova
tentativa podia reexecutar `task_runner` mesmo que o efeito original
já tivesse ocorrido.

## Correção — 4 mudanças, fail-closed

### 1. `JobQueue.try_start()` em memória valida `_STARTABLE_STATES`

Mesma checagem que o caminho durável (`_try_atomic_start`) sempre
teve — um job já `RUNNING`/`COMPLETED`/etc. nunca é "reiniciado" de
novo silenciosamente. Escopo estritamente `try_start()` (usado pelo
`GraphExecutor`) — `start()` não foi tocado (fora do escopo pedido).

### 2. Lease real — compartilhado (memória) ou persistente (SQLite)

Nova tabela `graph_executor_task_leases`
`(mission_id, task_id, lease_token, status, leased_at, resolved_at,
outcome_json, error)` com `PRIMARY KEY (mission_id, task_id)` —
`INSERT` atômico só sucede para quem chega primeiro. Com `db_path`
ausente, o MESMO papel é cumprido por um registro de **classe**
(`GraphExecutor._memory_leases`, nunca de instância — essa era
exatamente a lacuna original), chaveado também pela identidade do
`JobQueue` para nunca confundir mundos em memória genuinamente
diferentes.

`_dispatch_with_retry()` adquire o lease UMA VEZ no início (cobre
todos os retries internos, sequenciais e seguros). Status possíveis
na aquisição:
- `ACQUIRED` — direito exclusivo concedido, executa.
- `IN_PROGRESS`/`UNKNOWN_OUTCOME` — outra execução (desta ou de
  OUTRA instância) já detém o lease — devolve
  `TIMED_OUT_UNKNOWN_OUTCOME` IMEDIATAMENTE.
- `COMPLETED`/`FAILED` — resultado real já existe — usa direto,
  nunca reexecuta.

### 3. `UNKNOWN_OUTCOME` sobrevive a restart, nunca vira retry automático

Persistido no banco (quando há `db_path`) — uma nova instância lê a
MESMA linha. `recover_stuck_jobs()` (mecanismo existente,
preservado sem alteração) pode mudar o `Job` para `RETRYING`, mas o
LEASE — não o `Job` — é a fonte de verdade para o `GraphExecutor`;
testado explicitamente que isso nunca libera reexecução.

Quando a thread de fundo (que existe apenas dentro do MESMO
processo/instância que a iniciou) termina DE VERDADE depois que o
chamador já desistiu por timeout, ela mesma resolve o lease com o
resultado REAL (condicionado ao `lease_token`, nunca sobrescrevendo
um lease que uma ação futura tenha assumido) — para que uma chamada
seguinte (desta instância) use o resultado real em vez de reexecutar.

### 4. Nova instância nunca gira, nunca reexecuta silenciosamente

O antigo `time.sleep(0.01); continue` (que criava um busy-loop
infinito genuíno quando `try_start()` passou a rejeitar corretamente
um job já `RUNNING`, item 1) foi eliminado — a decisão de
`IN_PROGRESS`/`UNKNOWN_OUTCOME` agora é tomada UMA VEZ, na aquisição
do lease, e devolvida imediatamente como `TIMED_OUT_UNKNOWN_OUTCOME`.

## Validado com os cenários exatos exigidos

`tests/test_microcorrecao_atlas_expansion1_multiinstance.py` (9
testes) + `JobQueueTryStartInMemoryValidatesStatesTests`:
- `try_start()` em memória rejeita job já `RUNNING`/`COMPLETED`,
  continua funcionando para `QUEUED`/`RETRYING`.
- **Duas instâncias em memória, mesmo `JobQueue`, disputando a
  MESMA tarefa** — efeito externo aplicado exatamente 1x (contagem
  real, nunca proxy).
- Controle negativo: instâncias com `JobQueue` DIFERENTES não
  compartilham lease indevidamente.
- **SQLite/restart**: nova instância (nenhum objeto Python
  compartilhado com a original) nunca reexecuta e retorna em
  milissegundos (nunca gira).
- **Cenário exato do `recover_stuck_jobs()`**: mudança de estado do
  `Job` NUNCA libera reexecução — o lease permanece a fonte de
  verdade.
- Lease persistido confirmado diretamente via SQL, sem nenhuma
  referência à instância original (simulação forte de processo
  separado).
- Resolução tardia (thread original termina depois) — chamada
  seguinte usa o resultado real, nunca reexecuta.

## Preservação confirmada explicitamente

Reexecutei os 20 arquivos de teste dedicados de TODAS as rodadas
anteriores — **267 testes, todos passando sem alteração**, incluindo
os 48 testes das duas rodadas anteriores de ATLAS EXPANSION 1 (sem
NENHUMA modificação necessária neles — o novo design é
comportamentalmente compatível com todos os invariantes já
estabelecidos).

## Arquivos alterados
- `core/jobs/queue.py` — `try_start()` em memória valida
  `_STARTABLE_STATES`
- `core/models/db.py` — nova tabela `graph_executor_task_leases`
- `atlas/orchestration/graph_executor.py` — lease real
  (`_acquire_lease`/`_resolve_lease`, memória de classe + SQLite),
  `_dispatch_with_retry()` redesenhado (lease adquirido uma vez,
  cobre retries internos), `_run_with_timeout_tracked()` resolve o
  lease na conclusão tardia, `_finalize_cached_completed()` (novo)
- `atlas/director.py` — `db_path` passado ao `GraphExecutor`

## Arquivos novos
- `tests/test_microcorrecao_atlas_expansion1_multiinstance.py` (9 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- Um `UNKNOWN_OUTCOME` cujo processo original morreu ANTES da thread
  resolver o lease (crash genuíno, não apenas um restart controlado)
  fica persistido nesse estado PARA SEMPRE — nenhuma forma automática
  de "destravar" foi implementada nesta correção (deliberadamente —
  o pedido foi explícito: nunca reexecutar sem confirmação real de
  término/cancelamento ou prova de idempotência; nenhuma das duas
  foi construída aqui). Desbloquear esse caso exigiria uma ação
  administrativa explícita fora do escopo desta entrega.
