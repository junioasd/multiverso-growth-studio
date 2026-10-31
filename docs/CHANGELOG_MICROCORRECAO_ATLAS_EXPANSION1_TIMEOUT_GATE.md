# CHANGELOG — Microcorreção ATLAS EXPANSION 1: Timeout+Retry Nunca Duplica + Quality Gate Consistente

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1`
(1643 testes). Corrige as 2 falhas reais reproduzidas pela auditoria,
preservando integralmente V1 + resto da Expansion 1.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1657  PASS: 1657  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~93-96s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0
compileall: OK
```

**1657 testes** (1643 preservados + **14 testes novos**).

## Falha 1 — Timeout + retry podia duplicar efeito externo

**Reproduzido antes de corrigir**: com `max_retries=1`, a primeira
execução estourava o timeout (thread A ainda viva), o loop de retry
iniciava uma SEGUNDA thread (B) da MESMA tarefa sem checar se A ainda
estava rodando. Medido: 2 aplicações reais do efeito externo, enquanto
o relatório final dizia `FAILED`.

**Causa raiz**: `_run_with_timeout()` simplesmente "desistia de
esperar" no timeout e devolvia controle — o loop de retry em
`_dispatch_with_retry()` não tinha NENHUMA noção de que a execução
anterior podia ainda estar viva, e simplesmente iniciava outra.

**Corrigido — fail-closed, registro real de threads em voo**: novo
`GraphExecutor._inflight` (dict protegido por lock, chaveado por
`(mission_id, task_id)`). `_run_with_timeout_tracked()` — substitui
`_run_with_timeout()` — SEMPRE consulta este registro ANTES de
iniciar qualquer execução:
- Já existe uma entrada e a thread AINDA está viva → NUNCA inicia uma
  nova. Devolve `TIMED_OUT_UNKNOWN_OUTCOME` (nunca `FAILED` — que
  liberaria retry automático via `JobQueue`).
- Já existe uma entrada e a thread JÁ TERMINOU (desde a última
  checagem) → usa o resultado REAL dela, nunca reexecuta.
- Nenhuma entrada → inicia uma nova thread genuinamente.

Em `_dispatch_with_retry()`: ao receber `TIMED_OUT_UNKNOWN_OUTCOME`,
a função retorna IMEDIATAMENTE — nunca chama `job.fail()` (que
liberaria RETRYING), Job e Mission permanecem em `RUNNING` (estado
honesto de incerteza, nunca mascarado como falha nem sucesso). Uma
nova chamada de `execute_plan()` para o MESMO `mission_id`
reconsulta o MESMO registro antes de tentar de novo.

Novo método público `wait_for_inflight_completion(timeout=...)` —
aguarda de forma limitada e DETERMINÍSTICA (nunca `time.sleep()`
arbitrário) toda thread em voo terminar; usado por testes e para
coordenação explícita de shutdown, nunca chamado automaticamente
pela lógica de negócio.

## Falha 2 — Quality Gate rejeitado deixava estado inconsistente

**Reproduzido antes de corrigir**: o Job virava `COMPLETED` e a
Missão virava `COMPLETED` ANTES da checagem do Quality Gate.
`MissionTracker.verify()` levanta `InvalidMissionTransition` quando o
veredito é `REJECTED` — essa exceção NUNCA era tratada, propagando
para fora de `execute_plan()`. Resultado: Missão presa em
`COMPLETED` (nunca pode voltar para RETRYING/RUNNING nessa state
machine — só pode ir para VERIFIED ou FAILED), Job preso em
`COMPLETED` (terminal), e uma exceção não tratada.

**Corrigido — reordenação**: o Quality Gate agora é avaliado ANTES
de qualquer transição de Mission/Job:
- Gate aprovado (ou nenhum gate aplicável): Mission
  RUNNING→COMPLETED→VERIFIED (via `verify()`), Job `complete()` SÓ
  DEPOIS disso — nunca antes.
- Gate rejeitado: Mission NUNCA vai para COMPLETED. `job.fail(...)`
  é chamado diretamente (mesmo mecanismo de uma exceção real do
  `task_runner`) — decide RETRYING (retries disponíveis, Mission
  RUNNING→RETRYING, tenta de novo — seguro, a execução anterior já
  terminou de verdade) ou FAILED (retries esgotados, Mission
  RUNNING→FAILED, terminal e consistente).

## Validado com os cenários exatos exigidos

`tests/test_microcorrecao_atlas_expansion1_timeout_gate.py` (14
testes):
- Efeito externo aplicado exatamente 1x mesmo com timeout+retry
  (contagem real, nunca um proxy).
- Duas chamadas de `execute_plan()` para o mesmo `mission_id`
  enquanto a primeira thread ainda vive — nunca inicia uma segunda.
- Retomada depois que a thread original termina sozinha (mas antes
  da limpeza) — usa o resultado real, nunca reexecuta.
- Múltiplas tarefas com timeout — cada uma isolada em seu próprio
  registro, nunca contaminação cruzada.
- Nenhuma thread residual interfere em testes seguintes (baseline
  antes, limpeza determinística durante, confirmação depois —
  inclusive 5 execuções repetidas sem acumular threads).
- Quality Gate: APROVADO (VERIFIED), REJEITADO sem retries (FAILED,
  nunca preso em COMPLETED), REJEITADO com retries (retenta até
  aprovar), retomada após rejeição com retries disponíveis (continua
  consistente, nunca duplica), retomada após rejeição definitiva
  (permanece FAILED, nunca uma exceção).
- Sem `quality_gate` definido continua funcionando exatamente como
  antes (nenhuma regressão no caso comum).

Também corrigidos os 2 testes pré-existentes que referenciavam o
status antigo (`FAILED`) para timeout — agora `TIMED_OUT_UNKNOWN_OUTCOME`
— e os 3 testes de `CleanShutdown` que a auditoria encontrou como
flaky: agora usam `wait_for_inflight_completion()` (determinístico)
em vez de `time.sleep()` arbitrário.

## Descoberta incidental (documentada, não uma falha desta correção)

Durante a investigação, descobri que `JobQueue.try_start()` em modo
EM MEMÓRIA (sem `db_path`) não valida `_STARTABLE_STATES` antes de
transicionar para RUNNING (só o caminho durável via SQLite faz essa
checagem) — um `try_start()` repetido sobre um job já RUNNING
incrementa `attempts` de novo sem impedimento. Isso NÃO ameaça a
correção acima: a defesa real contra duplicação de efeito externo é
o registro `_inflight` do `GraphExecutor`, que é a ÚNICA porta de
entrada para invocar `task_runner` — verificado e confirmado
independente dessa peculiaridade do `JobQueue`. Não alterei
`JobQueue` (fora do escopo desta correção, módulo preservado).

## Preservação confirmada explicitamente

Reexecutei os 19 arquivos de teste dedicados de TODAS as rodadas
anteriores (BF1/BF2/BF3 + composite-key + shutdown residual + ATLAS
EXPANSION 1) — **258 testes, todos passando sem alteração**.

## Arquivos alterados
- `atlas/orchestration/graph_executor.py` — `_inflight` registry,
  `_run_with_timeout_tracked()` (substitui `_run_with_timeout()`),
  `wait_for_inflight_completion()`, `_evaluate_quality_gate()`,
  `_dispatch_with_retry()` reordenado
- `tests/test_atlas_expansion1_graph_executor.py` — 2 testes
  atualizados para o novo status honesto e limpeza determinística

## Arquivos novos
- `tests/test_microcorrecao_atlas_expansion1_timeout_gate.py` (14 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- Um `TIMED_OUT_UNKNOWN_OUTCOME` cancelado (nunca resolvido nem por
  sucesso nem por confirmação de término) deixa uma entrada residual
  em `self._inflight` que nunca é limpa automaticamente — um
  vazamento de memória limitado (uma entrada por tarefa nessa
  situação específica, nunca ilimitado em operação normal); a thread
  em si é daemon e sempre termina sozinha (nunca trava o processo)
