# CHANGELOG — Microcorreção ATLAS EXPANSION 1: Liveness/Heartbeat também para IN_PROGRESS

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1_NO_GATE_LIVENESS`
(1704 testes). Corrige a falha real reproduzida pela auditoria,
preservando integralmente V1 + demais correções da Expansion 1.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1708  PASS: 1708  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~112-114s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, subprocessos órfãos: 0
compileall: OK
```

**1708 testes** (1704 preservados + **4 testes novos**).

## Falha real confirmada

O TTL de `IN_PROGRESS` (correção de duas rodadas atrás) usava
`gate_in_progress_ttl_seconds`, mas não existia heartbeat/renovação
enquanto o `task_runner` estivesse genuinamente rodando. Reproduzido:
com um runner SAUDÁVEL demorando mais que o TTL (0.3s) mas menos que
`timeout_per_task` (5s), uma SEGUNDA instância convertia o lease
(ainda vivo) para `UNKNOWN_OUTCOME` e retornava
`TIMED_OUT_UNKNOWN_OUTCOME` — depois a PRIMEIRA execução terminava
com sucesso e SOBRESCREVIA para `COMPLETED` (já que `_resolve_lease()`
só é condicionado ao `lease_token`, que a conversão para
`UNKNOWN_OUTCOME` não rotaciona) — duas respostas diferentes e
contraditórias para a mesma tarefa.

## Correção — duas partes, a segunda descoberta durante o desenvolvimento

### 1. Heartbeat real durante a execução do `task_runner`

`_run_with_timeout_tracked()` agora inicia uma thread de heartbeat,
junto com a thread que executa `task_runner`, que renova `leased_at`
atomicamente (via `_renew_lease_heartbeat()`, agora estendido para
também aceitar `status='IN_PROGRESS'`, não só `GATE_IN_PROGRESS`) —
condicionada ao `lease_token` ATUAL — enquanto a thread real estiver
viva. O heartbeat continua rodando mesmo DEPOIS do chamador desistir
de esperar por `timeout_seconds`, cessando SOMENTE quando a thread
real sinaliza `done_event` (terminou) ou o processo morre (thread
daemon).

### 2. Bug descoberto ao testar a correção acima: o próprio chamador sobrescrevia o lease

Ao escrever o teste do cenário exato exigido, descobri que
`_run_exclusive_retry_loop()` resolvia o lease para `UNKNOWN_OUTCOME`
IMEDIATAMENTE sempre que `_run_with_timeout_tracked()` retornava
`TIMED_OUT_UNKNOWN_OUTCOME` — INDEPENDENTEMENTE de a thread real
ainda estar genuinamente viva (protegida pelo heartbeat) ou não. Isso
anulava completamente a correção acima: o PRÓPRIO chamador, ao ter
seu `timeout_per_task` esgotado, destruía a proteção do heartbeat
sobrescrevendo o lease na hora. Corrigido: essa resolução imediata
foi REMOVIDA — o lease nunca é tocado neste ponto; fica exatamente
como `_run_with_timeout_tracked()`/o heartbeat o deixaram. Se a
thread real ainda está viva, o heartbeat continua protegendo; se
genuinamente morreu, uma chamada FUTURA (desta ou de outra instância)
verá o TTL expirado sem heartbeat e converterá para `UNKNOWN_OUTCOME`
de verdade, atomicamente, na aquisição do lease — nunca o próprio
chamador que apenas desistiu de esperar.

## Validado com os cenários exatos exigidos

`tests/test_microcorrecao_atlas_expansion1_inprogress_heartbeat.py`
(4 testes):
- **Cenário exato**: runner saudável >TTL e <`timeout_per_task` —
  monitorado continuamente via SQL, o lease NUNCA passa por
  `UNKNOWN_OUTCOME` durante toda a execução; `runner` executa
  exatamente 1x; estado final `VERIFIED`/`COMPLETED`, consistente
  (a "segunda instância" concorrente reporta corretamente que não
  pôde prosseguir — comportamento esperado, nunca um bug — o que
  importa é que o LEASE em si nunca foi corrompido).
- Prova positiva direta via SQL: `leased_at` confirmado avançando
  genuinamente durante uma execução mais longa que o TTL.
- Requisito explícito confirmado: o heartbeat continua mesmo DEPOIS
  do chamador desistir de esperar por `timeout_per_task`, enquanto a
  thread real ainda está viva.
- **Reconfirmação do teste de crash real** (preservado explicitamente
  nesta rodada): `os._exit()` mata a thread real e a de heartbeat
  juntas; após o TTL sem nenhum heartbeat, uma nova instância
  converte corretamente para `UNKNOWN_OUTCOME`, sem rerun automático.
- Confirmado sem flakiness rodando o cenário completo 5× seguidas.

## Preservação confirmada explicitamente

Reexecutei os 28 arquivos de teste dedicados de TODAS as rodadas
anteriores — **309 testes, todos passando sem alteração**, incluindo
os 95 testes das dez rodadas anteriores de ATLAS EXPANSION 1 (sem
NENHUMA modificação necessária neles).

## Arquivos alterados
- `atlas/orchestration/graph_executor.py` — `_renew_lease_heartbeat()`
  estendido para `IN_PROGRESS`; `_run_with_timeout_tracked()` agora
  inicia uma thread de heartbeat paralela à execução do
  `task_runner`, rastreada via `_local_threads` para
  `wait_for_inflight_completion()`; `_run_exclusive_retry_loop()` —
  removida a resolução imediata (e incorreta) do lease para
  `UNKNOWN_OUTCOME` ao receber `TIMED_OUT_UNKNOWN_OUTCOME`

## Arquivos novos
- `tests/test_microcorrecao_atlas_expansion1_inprogress_heartbeat.py` (4 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- A mesma pendência de rodadas anteriores permanece: o intervalo de
  heartbeat (`TTL/3`) é uma heurística — um ambiente com latência de
  escrita SQLite extremamente alta poderia, em teoria, ainda perder
  uma renovação a tempo; limitação inerente de qualquer heartbeat
  baseado em tempo, não específica desta implementação
