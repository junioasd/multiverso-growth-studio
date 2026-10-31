# CHANGELOG — Microcorreção ATLAS EXPANSION 1: Lease Nunca Órfão em GATE_IN_PROGRESS

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1_GATE_RACE_FIX`
(1680 testes). Corrige a falha real reproduzida pela auditoria,
preservando integralmente V1 + demais correções da Expansion 1.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1685  PASS: 1685  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~98-101s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0
compileall: OK
```

**1685 testes** (1680 preservados + **5 testes novos**).

## Falha real confirmada

Depois que um outcome tardio era reivindicado (lease atomicamente
movido para `GATE_IN_PROGRESS`, correção da rodada anterior), se
`quality_gate_checker` levantasse uma exceção dentro de
`_finalize_pending_gate_outcome()`, essa exceção propagava sem NUNCA
resolver o lease — ele ficava preso em `GATE_IN_PROGRESS` PARA
SEMPRE. Reproduzido: `TIMED_OUT_UNKNOWN_OUTCOME` → outcome tardio →
checker crasha (`RuntimeError` não tratada) → lease confirmado preso
em `GATE_IN_PROGRESS` no banco → nova instância com checker SAUDÁVEL
encontrava esse estado exclusivo bloqueante e retornava
`TIMED_OUT_UNKNOWN_OUTCOME` indefinidamente, nunca conseguindo
finalizar o outcome já obtido de verdade pelo `task_runner`.

## Correção — fail-closed e recuperável

A chamada a `_evaluate_quality_gate()` (que invoca
`quality_gate_checker`) agora é envolvida em tratamento de exceção,
tanto em `_finalize_pending_gate_outcome()` (o caminho reportado)
quanto em `_run_exclusive_retry_loop()` (o caminho de execução
fresca — mesma classe de falha, mesmo lease exclusivo em jogo, para
consistência e para nunca deixar uma exceção não tratada escapar de
`execute_plan()` também nesse caminho).

Novo `_handle_gate_evaluation_error()`:
- Registra o erro real (log de aviso via `logging` padrão do Python
  — nenhuma dependência nova) e persiste o motivo na própria coluna
  `error` do lease — telemetria real, nunca descartada
  silenciosamente.
- Devolve o lease ATOMICAMENTE para `OUTCOME_AVAILABLE_PENDING_GATE`
  usando o `lease_token` que o chamador detém no momento (seja o
  token `ACQUIRED` original, seja o token `GATE_IN_PROGRESS`
  reivindicado numa retomada) — NUNCA fica órfão.
- Nunca marca Job/Mission como `COMPLETED`/`VERIFIED`.
- Nunca reexecuta `task_runner` — o outcome já obtido é preservado.
- Devolve um novo status explícito e retryable:
  `"QUALITY_GATE_EVALUATION_ERROR"` — distinto de
  `"QUALITY_GATE_UNAVAILABLE"` (checker ausente) para melhor
  diagnóstico/observabilidade.

## Validado com os cenários exatos exigidos

`tests/test_microcorrecao_atlas_expansion1_orphaned_lease.py` (5
testes):
- **Cenário exato**: timeout → outcome tardio → restart → checker
  lança exceção → lease confirmado NUNCA órfão (via SQL direto) →
  nova instância com checker saudável → `runner` permanece
  EXATAMENTE 1x, gate executa de novo e finaliza `VERIFIED`.
- **Variante REJECT**: mesmo cenário, mas o checker saudável REJEITA
  — `runner` ainda 1x, missão `FAILED` definitivo (sem retries),
  nunca `VERIFIED`.
- **Variante REJECT com retries disponíveis**: gera uma tentativa
  NOVA e genuína de `task_runner` (nunca reaproveita o outcome tardio
  rejeitado).
- Telemetria confirmada diretamente: o motivo real da exceção fica
  persistido na coluna `error` do lease.
- A MESMA proteção aplicada ao caminho de execução fresca (dentro do
  timeout) — checker crasha, lease nunca órfão, chamada seguinte
  finaliza sem reexecutar.

## Preservação confirmada explicitamente

Reexecutei os 23 arquivos de teste dedicados de TODAS as rodadas
anteriores — **286 testes, todos passando sem alteração**, incluindo
os 71 testes das cinco rodadas anteriores de ATLAS EXPANSION 1 (sem
NENHUMA modificação necessária neles).

## Arquivos alterados
- `atlas/orchestration/graph_executor.py` — `import logging` +
  logger de módulo; novo `_handle_gate_evaluation_error()`;
  `_finalize_pending_gate_outcome()` e `_run_exclusive_retry_loop()`
  envolvem a chamada a `_evaluate_quality_gate()` em tratamento de
  exceção

## Arquivos novos
- `tests/test_microcorrecao_atlas_expansion1_orphaned_lease.py` (5 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- Um crash do PROCESSO (não apenas do checker) entre a reivindicação
  de `GATE_IN_PROGRESS` e a resolução ainda deixaria o lease preso —
  essa é uma limitação DIFERENTE (já documentada nas rodadas
  anteriores para `UNKNOWN_OUTCOME`) e permanece fora do escopo desta
  correção especificamente sobre exceções do `quality_gate_checker`
