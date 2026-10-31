# CHANGELOG — Microcorreção ATLAS EXPANSION 1: Conclusão Tardia Nunca Equivale a Conclusão Validada

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1_MULTIINSTANCE`
(1666 testes). Corrige a falha real reproduzida pela auditoria,
preservando integralmente V1 + demais correções, incluindo toda a
proteção multi-instância/lease da rodada anterior.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1673  PASS: 1673  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~97-100s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0
compileall: OK
```

**1673 testes** (1666 preservados + **7 testes novos**).

## Falha real confirmada

Após um `TIMED_OUT_UNKNOWN_OUTCOME`, se a thread de fundo terminasse
depois COM SUCESSO, `_run_with_timeout_tracked()` resolvia o lease
DIRETAMENTE como `"COMPLETED"` — sem nunca consultar o Quality Gate.
Em restart/nova instância, `_finalize_cached_completed()` usava esse
resultado cacheado e chamava `mission_tracker.verify(...,
quality_gate_verdict=None)` INCONDICIONALMENTE — ignorando por
completo `quality_gate_checker`, mesmo quando `task.quality_gate`
estava definido. Reproduzido: uma tarefa com gate configurado para
SEMPRE rejeitar terminava `VERIFIED` na retomada.

## Correção — novo estado intermediário explícito, fail-closed

### Novo status de lease: `OUTCOME_AVAILABLE_PENDING_GATE`

Quando a thread de fundo termina COM SUCESSO depois que o chamador já
desistiu por timeout, o lease agora é resolvido como
`"OUTCOME_AVAILABLE_PENDING_GATE"` — NUNCA `"COMPLETED"` diretamente.
O outcome real é preservado (nunca perdido, nunca força uma
reexecução), mas marcado explicitamente como AINDA NÃO VALIDADO. Uma
falha tardia (exceção real) continua indo direto para `"FAILED"` —
sem sentido em passar por um Quality Gate.

### Reivindicação atômica para avaliação do gate

Ao encontrar esse status na aquisição do lease, um NOVO
`lease_token` é emitido atomicamente (`UPDATE ... WHERE status =
'OUTCOME_AVAILABLE_PENDING_GATE'` — condicional ao status, não ao
token antigo, que pertence à execução original já encerrada) — só um
chamador por vez "ganha" o direito de avaliar o gate e finalizar; em
memória, a mesma semântica sob o lock de classe já existente.

### Gate obrigatório antes de qualquer `COMPLETED`/`VERIFIED`

Novo `_finalize_pending_gate_outcome()`: avalia
`quality_gate_checker` sobre o outcome tardio ANTES de qualquer
transição de Mission/Job — `task_runner` NUNCA é reexecutado para
obter esse resultado (o outcome tardio já é o efeito real e
definitivo). Se aprovado (ou nenhum gate aplicável): finaliza Job/
Mission/lease como de costume. Se rejeitado: segue o MESMO fluxo
normal de rejeição/retry/falha que uma rejeição recém-obtida
seguiria — com retries disponíveis, uma tentativa NOVA e GENUÍNA de
`task_runner` é iniciada (nunca reaproveita o outcome tardio
rejeitado como se fosse válido); sem retries, `FAILED` definitivo.

O loop principal de retry (`_dispatch_with_retry`) foi extraído para
`_run_exclusive_retry_loop()` — reutilizado tanto pela primeira
execução quanto por uma nova tentativa pós-rejeição de outcome
tardio, sob o MESMO lease já reivindicado.

## Validado com os 2 cenários exatos exigidos

`tests/test_microcorrecao_atlas_expansion1_delayed_gate.py` (7
testes):
1. **Timeout + resultado tardio + retomada + gate REJECT**: testado
   em memória (mesma instância) e em SQLite com uma instância
   GENUINAMENTE nova simulando restart — `runner` executa
   EXATAMENTE 1 vez, o gate é efetivamente consultado, a missão
   NUNCA termina `VERIFIED` (termina `FAILED`).
2. **Mesmo cenário com gate APPROVED**: `runner` executa exatamente
   1 vez, finaliza corretamente (`VERIFIED`) sem reexecução — testado
   em memória e SQLite/restart, e também o caso sem `quality_gate`
   aplicável (comportamento já aprovado, sem regressão).

Adicional: rejeição com retries disponíveis dispara uma tentativa
NOVA e genuína (nunca reaproveita o outcome tardio rejeitado);
reivindicação atômica entre duas instâncias descobrindo o mesmo
outcome tardio simultaneamente — `task_runner` nunca reexecutado,
convergência sempre para `VERIFIED` (cada chamador reporta
`VERIFIED` ou `SKIPPED_ALREADY_DONE`, dependendo da ordem real das
threads — comportamento correto e já estabelecido desde a primeira
versão, não uma regressão).

## Preservação confirmada explicitamente

Reexecutei os 21 arquivos de teste dedicados de TODAS as rodadas
anteriores — **274 testes, todos passando sem alteração**, incluindo
os 57 testes das três rodadas anteriores de ATLAS EXPANSION 1 (sem
NENHUMA modificação necessária neles) — toda a proteção
multi-instância/lease da rodada anterior permanece intacta.

## Arquivos alterados
- `atlas/orchestration/graph_executor.py` —
  `_run_with_timeout_tracked()` resolve sucesso tardio como
  `OUTCOME_AVAILABLE_PENDING_GATE`; `_acquire_lease_durable()`/
  `_acquire_lease_memory()` reivindicam esse status atomicamente;
  `_dispatch_with_retry()` reestruturado (loop principal extraído
  para `_run_exclusive_retry_loop()`); novo
  `_finalize_pending_gate_outcome()`

## Arquivos novos
- `tests/test_microcorrecao_atlas_expansion1_delayed_gate.py` (7 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- A mesma pendência da rodada anterior permanece: um outcome cujo
  processo original morreu ANTES de resolver o lease (crash genuíno)
  fica persistido em `UNKNOWN_OUTCOME` para sempre — nenhuma forma
  automática de desbloqueio foi implementada (deliberado, fora do
  escopo desta correção)
