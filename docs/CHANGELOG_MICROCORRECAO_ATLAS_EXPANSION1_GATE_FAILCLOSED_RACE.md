# CHANGELOG — Microcorreção ATLAS EXPANSION 1: Quality Gate Fail-Closed sem Checker + Reivindicação Realmente Exclusiva

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1_DELAYED_GATE`
(1673 testes). Corrige as 2 falhas reais reproduzidas pela auditoria,
preservando integralmente V1 + demais correções da Expansion 1.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1680  PASS: 1680  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~102-104s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0
compileall: OK
```

**1680 testes** (1673 preservados + **7 testes novos**).

## Falha 1 — Fail-open de Quality Gate sem checker

**Reproduzido antes de corrigir**: `_evaluate_quality_gate()` tratava
"nenhum `task.quality_gate` aplicável" e "`task.quality_gate` definido
mas `quality_gate_checker` ausente nesta chamada" como o MESMO caso —
`if task.quality_gate is None or quality_gate_checker is None: return
None, True` — ambos implicitamente aprovados. Confirmado: 1ª execução
`TIMED_OUT_UNKNOWN_OUTCOME`; retomada SEM `quality_gate_checker`
terminou `VERIFIED`, com `runner` 1x e o gate NUNCA avaliado.

**Corrigido**: `_evaluate_quality_gate()` agora distingue os dois
casos explicitamente — sem `task.quality_gate`: aprovado implícito
(inalterado). Com `task.quality_gate` definido mas checker ausente:
novo desfecho `"UNAVAILABLE"` — FAIL-CLOSED. Novo
`_handle_gate_unavailable()`: nunca marca Job/Mission
`COMPLETED`/`VERIFIED`, nunca reexecuta `task_runner`, devolve
`TaskExecutionResult(status="QUALITY_GATE_UNAVAILABLE", ...)`, e
preserva o outcome no lease (`OUTCOME_AVAILABLE_PENDING_GATE`) para
uma chamada FUTURA com checker real resolver — nunca perdido. Aplica-
se tanto a um outcome tardio quanto a uma execução DENTRO do timeout
(a mesma classe de fail-open existia em ambos os caminhos, já que
ambos usam a mesma função de avaliação).

## Falha 2 — Reivindicação de `OUTCOME_AVAILABLE_PENDING_GATE` não exclusiva em SQLite

**Reproduzido antes de corrigir**: a reivindicação anterior fazia
`UPDATE ... SET lease_token = ? WHERE status = 'OUTCOME_AVAILABLE_
PENDING_GATE'` — o `status` nunca era alterado pela própria
atualização, então uma SEGUNDA instância rodando a MESMA instrução
SQL encontrava a condição `WHERE` AINDA satisfeita (nada tinha
mudado) e TAMBÉM "ganhava" — não era um compare-and-set genuíno.
Confirmado com 2 instâncias SQLite reais: gate chamado 2x; uma
`VERIFIED`, a outra `ConcurrentMissionUpdate` não tratada.

**Corrigido**: o `UPDATE` agora TRANSICIONA `status` para um valor
exclusivo `'GATE_IN_PROGRESS'` na mesma instrução —
`UPDATE ... SET lease_token = ?, status = 'GATE_IN_PROGRESS', ... 
WHERE status = 'OUTCOME_AVAILABLE_PENDING_GATE'`. A MESMA instrução
rodada por uma segunda instância, mesmo microssegundos depois,
encontra `status` já `'GATE_IN_PROGRESS'` — `rowcount == 0` —
nunca reivindicando de novo; observa `"GATE_IN_PROGRESS"` e devolve
`TIMED_OUT_UNKNOWN_OUTCOME` imediatamente, sem girar, sem avaliar o
gate. `_dispatch_with_retry()` atualizado para tratar
`"GATE_IN_PROGRESS"` da mesma forma que `"IN_PROGRESS"`/
`"UNKNOWN_OUTCOME"`. Mesma disciplina aplicada ao caminho em memória
(consistência de estado, mesmo sem o mesmo risco de corrida entre
conexões distintas).

Se a avaliação do gate (pelo vencedor da reivindicação) resultar em
`UNAVAILABLE` (checker ausente também nesta chamada), o lease é
devolvido para `OUTCOME_AVAILABLE_PENDING_GATE` (usando o token
`GATE_IN_PROGRESS` que este chamador detém) — liberando a
reivindicação para uma chamada futura, nunca deixando o lease preso
em `GATE_IN_PROGRESS` para sempre.

## Validado com os cenários exatos exigidos

`tests/test_microcorrecao_atlas_expansion1_gate_failclosed_race.py`
(7 testes):
- Retomada sem checker nunca chega a `VERIFIED` — `runner` 1x,
  status `QUALITY_GATE_UNAVAILABLE`.
- Chamada POSTERIOR com checker real ainda resolve corretamente, sem
  reexecutar (outcome nunca perdido).
- Execução DENTRO do timeout também fail-closed sem checker (mesma
  classe de bug, caminho diferente).
- **Duas instâncias SQLite REAIS** (conexões/objetos distintos,
  disputando o mesmo outcome pendente de gate): gate executado
  EXATAMENTE 1 vez, ZERO exceções de concorrência
  (`ConcurrentMissionUpdate` ou qualquer outra), `task_runner` nunca
  reexecutado, estado final da missão sempre `VERIFIED` de forma
  consistente.
- O perdedor da corrida pela reivindicação observa o estado
  exclusivo e devolve imediatamente, sem girar, sem avaliar o gate.
- Confirmado sem flakiness rodando o cenário de corrida 5× seguidas
  com até 3 instâncias competindo simultaneamente.

## Preservação confirmada explicitamente

Reexecutei os 22 arquivos de teste dedicados de TODAS as rodadas
anteriores — **281 testes, todos passando sem alteração**, incluindo
os 64 testes das quatro rodadas anteriores de ATLAS EXPANSION 1 (sem
NENHUMA modificação necessária neles).

## Arquivos alterados
- `atlas/orchestration/graph_executor.py` — `_evaluate_quality_gate()`
  distingue `APPROVED`/`REJECTED`/`UNAVAILABLE`; novo
  `_handle_gate_unavailable()`; `_acquire_lease_durable()`/
  `_acquire_lease_memory()` transicionam para `GATE_IN_PROGRESS`
  (compare-and-set genuíno); `_dispatch_with_retry()` trata
  `GATE_IN_PROGRESS` como estado exclusivo bloqueante;
  `_run_exclusive_retry_loop()`/`_finalize_pending_gate_outcome()`
  atualizados para o novo desfecho de 3 vias

## Arquivos novos
- `tests/test_microcorrecao_atlas_expansion1_gate_failclosed_race.py` (7 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- Um outcome preso em `GATE_IN_PROGRESS` porque o processo que
  reivindicou morreu ANTES de resolver (crash genuíno, não apenas
  ausência de checker) fica persistido nesse estado — mesma
  limitação já documentada para `UNKNOWN_OUTCOME` nas rodadas
  anteriores, deliberadamente fora do escopo desta correção
