# CHANGELOG — Microcorreção ATLAS EXPANSION 1: Caminho Sem Quality Gate — Persistência Imediata + Liveness/TTL

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1_FRESH_PATH_UNIFICATION`
(1699 testes). Corrige a falha real reproduzida pela auditoria,
preservando integralmente V1 + demais correções da Expansion 1.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1704  PASS: 1704  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~114-118s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, subprocessos órfãos: 0
compileall: OK
```

**1704 testes** (1699 preservados + **5 testes novos**).

## Falha real confirmada (reproduzida com subprocessos reais)

No caminho SEM Quality Gate, `task_runner` aplicava o efeito e
retornava sucesso, mas o outcome só era persistido no lease como
ÚLTIMO passo — depois de `mission.transition()` → `mission.verify()`
→ `job.complete()`. Se o processo morresse ABRUPTAMENTE em qualquer
ponto antes dessa última linha, o lease ficava preso em
`IN_PROGRESS` com `outcome_json=NULL` — e `IN_PROGRESS` não tinha
NENHUM mecanismo de TTL/reclaim (só `GATE_IN_PROGRESS` tinha).
Reproduzido com dois subprocessos reais: (1) crash entre a resolução
planejada do lease e a sincronização de Mission/Job; (2) crash
DENTRO do próprio `task_runner`, antes dele retornar.

## Correção — 3 mudanças, fail-closed e honestas sobre garantias

### 1. Persistência PRIMEIRO, sincronização DEPOIS

No caminho sem gate, o outcome agora é persistido e o lease resolvido
para `COMPLETED` como o PRIMEIRO passo após `task_runner` retornar
sucesso — Mission/Job são sincronizados DEPOIS. Um crash nessa janela
final deixa Mission/Job temporariamente dessincronizados (confirmado:
Mission permanece em `RUNNING`), mas NUNCA perde o outcome.

Novo `_advance_mission_to_completed_and_verify()` — avança a missão a
partir de QUALQUER estado intermediário válido (`QUEUED`→`PLANNED`→
`RUNNING`→`COMPLETED`→`VERIFIED`) em que possa estar, idempotente.
`_finalize_cached_completed()` (usado tanto na retomada de um lease
já `COMPLETED` quanto por esta correção) foi reescrito para usar este
helper — mais robusto que a versão anterior, que só tentava a
transição direto para `COMPLETED` e engolia silenciosamente uma
eventual falha sem realmente avançar o estado.

### 2. Liveness/TTL para o lease `IN_PROGRESS`

`IN_PROGRESS` (a fase de execução do `task_runner`) agora também tem
TTL — reutilizando `gate_in_progress_ttl_seconds` — mas com um
desfecho DELIBERADAMENTE diferente do reclaim de `GATE_IN_PROGRESS`:
NUNCA reclama como `ACQUIRED` (isso reexecutaria `task_runner`).
Passado o TTL sem nenhuma resolução, transiciona ATOMICAMENTE
(compare-and-set condicionado a `status` E `leased_at` exatos) para
`UNKNOWN_OUTCOME` — terminal, nunca retentado automaticamente.

### 3. Sem promessa de exactly-once sem prova de idempotência

Documentado explicitamente (código + este changelog): este executor
NUNCA promete exactly-once para efeitos externos arbitrários — retry
automático só ocorre sobre um desfecho DEFINITIVAMENTE conhecido (uma
exceção real do `task_runner`, ou uma rejeição de gate sobre um
outcome já obtido). Um desfecho genuinamente desconhecido
(`UNKNOWN_OUTCOME`) nunca dispara retry algum — confirmado com
múltiplas tentativas seguidas após o TTL, nenhuma reexecuta.

## Validado com os 3 cenários exatos exigidos

`tests/test_microcorrecao_atlas_expansion1_no_gate_liveness.py` (5
testes), usando dois novos workers reais
(`tests/_helpers/crash_no_gate_worker.py`,
`tests/_helpers/crash_during_runner_worker.py`, ambos via
`subprocess.run()`):
- **(a)** crash real após `runner` retornar, sem gate — lease
  confirmado `COMPLETED` com outcome persistido (nunca
  `IN_PROGRESS`/`outcome_json=NULL`, o bug original), Mission
  confirmada dessincronizada nesse ponto; retomada reconcilia sem
  reexecutar. Também confirmado: a reconciliação de um lease já
  `COMPLETED` nunca espera (diferente de um reclaim por TTL).
- **(b)** crash real DENTRO do `task_runner` — lease confirmado
  `IN_PROGRESS` com `outcome_json=NULL` (desfecho genuinamente
  desconhecido); tentativa imediata (dentro do TTL) nunca reclama;
  após o TTL expirar de verdade, transiciona para `UNKNOWN_OUTCOME`,
  `runner` NUNCA reexecutado — confirmado também com 3 tentativas
  seguidas, nenhuma reexecuta.
- **(c)** restart após `COMPLETED` persistido (Mission artificialmente
  travada em `RUNNING`, simulando o cenário real) — reconcilia
  Mission/Job para `VERIFIED`/`COMPLETED` sem executar `runner`
  novamente, usando o outcome REAL já persistido no lease.
- Confirmado sem flakiness rodando o cenário completo 5× seguidas.

## Preservação confirmada explicitamente

Reexecutei os 27 arquivos de teste dedicados de TODAS as rodadas
anteriores — **305 testes, todos passando sem alteração**, incluindo
os 90 testes das nove rodadas anteriores de ATLAS EXPANSION 1 (sem
NENHUMA modificação necessária neles).

## Arquivos alterados
- `atlas/orchestration/graph_executor.py` — novo
  `_advance_mission_to_completed_and_verify()`;
  `_finalize_cached_completed()` reescrito para usá-lo;
  `_run_exclusive_retry_loop()`'s caminho sem gate reordenado
  (persiste+resolve lease primeiro, sincroniza depois);
  `_acquire_lease_durable()`/`_read_and_maybe_reclaim_lease_durable()`/
  `_acquire_lease_memory()` com TTL para `IN_PROGRESS` (transição
  atômica para `UNKNOWN_OUTCOME`, nunca para `ACQUIRED`)

## Arquivos novos
- `tests/_helpers/crash_no_gate_worker.py` — worker que resolve o
  lease para `COMPLETED` e morre antes de sincronizar Mission/Job
- `tests/_helpers/crash_during_runner_worker.py` — worker que morre
  dentro do próprio `task_runner`, antes de retornar
- `tests/test_microcorrecao_atlas_expansion1_no_gate_liveness.py` (5 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- `IN_PROGRESS` reutiliza `gate_in_progress_ttl_seconds` como seu
  TTL — se `timeout_per_task` (que já protege "task_runner demorando
  demais" DENTRO do mesmo processo) for configurado MAIOR que
  `gate_in_progress_ttl_seconds`, uma tarefa legitimamente lenta
  (mas ainda viva) poderia, em tese, ter seu lease `IN_PROGRESS`
  erroneamente elegível para transição a `UNKNOWN_OUTCOME` antes de
  `task_runner` genuinamente terminar — o responsável pela integração
  deve calibrar `gate_in_progress_ttl_seconds` considerando também o
  `timeout_per_task` máximo esperado, não só o tempo do
  `quality_gate_checker`
