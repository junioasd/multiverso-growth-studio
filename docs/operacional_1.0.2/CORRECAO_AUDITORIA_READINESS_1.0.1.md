# CORREÇÃO DA AUDITORIA DE READINESS 1.0.1 → 1.0.2-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_INDEPENDENTE_READINESS_OPERATIONAL_1_0_1_2026-09-24.md` (nesta pasta), com veredito **NÃO APROVADA** e 4 bloqueadores.
- **Predecessor:** 1.0.1 candidate `a04d5093…c86c9` (arquivado, inalterado).
- **Baseline:** v1.0.0 `932292a9…e42fe` (inalterada).
- **Status:** CANDIDATE — nova auditoria independente de readiness necessária.

## Resultado
| | 1.0.1 (auditada) | 1.0.2 |
|---|---|---|
| Testes | 2.073 | 2.086 (+13) |
| PASS / FAIL / ERROR / SKIP | 2.071 / 0 / 0 / 2 | 2.084 / 0 / 0 / 2 |
| 2 chamadas de 0,60 com limite 1,00 | **2 executadas**, ledger vazio | 1 executada, 2ª `LIMIT_EXCEEDED`, 0,60 cobrado |
| Corrida: limite 10, 2 × 6 | **2 autorizadas, 12 gastos** | 1 autorizada, 6 gastos |
| Kill switch entre retries de webhook | **2ª tentativa saiu (EXECUTED)** | 1 tentativa; nenhuma após o kill switch |
| Linha do kill switch ausente | **DISABLED (permitido)** | `UNREADABLE`, bloqueado |
| Banco novo | sem estado inicial | estado inicial explícito + evento |
| **Liveness (extra)** | **`advance()` travava para sempre** | retorna; passo PLANNED com motivo estável |

**Reprodução:** `reproducao_bloqueadores_readiness_1.0.1.py` e `reproducao_liveness_runner.py`, com saídas `reproducao_ANTES_1.0.1.txt` e `reproducao_DEPOIS_1.0.2.txt` — mesmo script nos dois pacotes.

## Causas-raiz e correções
1. **Provedor sem contabilização.**
   - **Causa:** `HttpGenerationProvider.call` chamava `authorize()` e nunca registrava o uso.
   - **Correção:** reserva atômica antes da chamada e **liquidação pelo desfecho real** depois:
     - EXECUTED → custo reportado pelo provedor (`CONFIRMED`) ou, sem custo informado, `COST_UNKNOWN` cobrando a reserva, sem inventar custo;
     - NOT_EXECUTED (conexão recusada, 4xx, 408/429/503) → reserva liberada;
     - UNKNOWN (timeout após envio, 500/502/504) → cobrança mantida até reconciliação do Owner.
2. **Corrida no limite.**
   - **Causa:** checar e registrar eram operações separadas.
   - **Correção:** `reserve()` numa transação `BEGIN IMMEDIATE`, somando cobranças registradas + reservas abertas + estimativa.
   - `authorize()` passou a **reservar** (a prévia sem efeito virou `preview()`); `record_usage()` liquida a reserva correspondente, sem cobrança dupla.
   - Reservas persistem: uma reserva órfã de crash continua contando até `reconcile()`, exclusivo do Owner e com evidência.
   - Uso com custo desconhecido fora de reserva exige reconciliação (`COST_RECONCILIATION_REQUIRED`).
3. **Kill switch entre retries.**
   - **Causa:** `WebhookDispatcher.deliver` verificava o kill switch só antes do laço.
   - **Correção:** a verificação passou para **dentro** do laço, antes de cada tentativa.
   - **Revisão dos demais laços:** `WebhookDispatcher` é o único laço de retry interno com chamada externa. O `OperationRunner` reverifica a cada `advance` (teste 6). Worker e scheduler reverificam a cada ciclo e janela. O provedor pago não tem retry interno. Os workers do radar não fazem chamadas de rede diretas.
4. **Linha ausente = DISABLED.**
   - **Causa:** `state()` tratava linha ausente como estado desligado.
   - **Correção:**
     - linha ausente ou estado inválido → `UNREADABLE` (bloqueio);
     - `init_db` grava o estado inicial explícito **somente quando a tabela é criada** (banco novo ou upgrade da v1.0.0);
     - apagar a linha e reiniciar **não** a recria como DISABLED; só o Owner redefine.
5. **Liveness — defeito REAL da 1.0.1, não apontado pela auditoria e revelado por um mutante que travou.**
   - **Causa:** com o kill switch ativo antes do pedido de aprovação, o `OperationRunner` marcava `WAITING_APPROVAL` sem escalação e com carimbo novo a cada iteração, e `advance()` girava para sempre (o worker travaria justamente numa emergência).
   - **Correção:** sem escalação criada, o passo não muda de status (motivo estável); kill switch verificado também antes de pedir aprovação; limite de iterações em `advance()` como defesa em profundidade.

## Arquivos alterados
- **Produção:**
  - `core/operations/cost_guard.py`
  - `core/operations/kill_switch.py`
  - `core/agents/runtime/http_generation_provider.py`
  - `bridge/execution/webhooks.py`
  - `navigator/planning.py`
  - `core/models/db.py` (tabela `ai_api_reservations`; colunas `budget_charge` e `reservation_id`; estado inicial do kill switch; tudo aditivo)
- **Testes:**
  - novo `tests/test_operacional_1_0_2.py` (13 testes);
  - `tests/test_operacional_1_0_1.py` com um ajuste: `test_06_07` agora exige **também** o evento de estado inicial (item 8 da auditoria), o que o torna mais estrito.

## Testes (itens mínimos da auditoria)
| Item | Teste |
|---|---|
| 1 | `test_1_sequential_calls_respect_cumulative_limit` |
| 2 | `test_2_accounting_is_integrated_into_the_real_provider` (CONFIRMED / COST_UNKNOWN / NOT_EXECUTED / UNKNOWN) |
| 3 | `test_3_threads_never_exceed_limit`, `test_3b_separate_processes_never_exceed_limit` (4 processos), `test_3c_concurrent_real_provider_calls` |
| 4 | `test_4_orphan_reservation_survives_crash_and_owner_reconciles` (SIGKILL real), `test_4b_ambiguous_timeout_keeps_charge_until_reconciled` |
| 5 | `test_5_kill_switch_between_webhook_retries_stops_next_attempt` |
| 6 | `test_6_operation_runner_retry_is_stopped_by_kill_switch`, `test_6b_kill_switch_before_first_advance_never_hangs_runner_or_worker` |
| 7 | `test_7_missing_or_invalid_row_fails_closed_and_restart_does_not_heal_it` |
| 8 | `test_8_new_and_upgraded_databases_get_explicit_initial_state` |
| 9, 10 | suíte 1.0.1 (24/24), correções de auditoria e replay legado: verdes na regressão |
| controle | `test_0_external_host_is_refused_by_the_test_control` (a guarda de rede exige controle de `urlopen`; chamadas reais limitadas a loopback) |

- **Mutação:** 10 de 10 detectados, incluindo os 4 defeitos originais, mais o mutante de liveness (código da 1.0.1). Nesse caso, o limite de iterações impediu o travamento e o teste acusou o estado inválido.
- **Runner estrito:** RC 0.

## Defeitos meus encontrados durante a correção
- A liquidação rotulava custo **estimado** como `CONFIRMED`; agora respeita `cost_confirmed`.
- Um teste registrava uso de chamadas **não autorizadas**; corrigido para o cenário da auditoria.
