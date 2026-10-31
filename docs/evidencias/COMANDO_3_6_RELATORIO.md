# COMANDO MESTRE 3/6 — NAVIGATOR + FORGE + MÁQUINA DE OPERAÇÃO — RELATÓRIO FINAL

**Status: CONCLUÍDO. As duas lacunas do checkpoint 3 foram fechadas (seção 7).**
**Baseline imutável:** `multiverso_growth_studio_comando_2_6_final_2026-09-24.zip`
(SHA-256 `0e741a4aaf2b50a9b6700e6697edd5d517179dd5d46b110776af3f0a8eb19b1e`).

## 1. Resultado
| Testes | PASS | FAIL | ERROR | SKIP | RC | ResourceWarning |
|---|---|---|---|---|---|---|
| 1.963 | 1.961 | 0 | 0 | 2 | 0 | 0 |

Os 1.963 testes somam os 1.927 do baseline e 36 novos (`test_comando_3_navigator_forge_execucao.py`: 15; `test_comando_3_navigator_planejamento_e2e.py`: 21).

Os 2 skips são `test_real_windows_*`, aprovados em Windows real no Comando 1.

**Comandos 1 e 2 intactos:** `graph_executor.py`, `digital_twin.py`, `learning.py`, `director.py`, `loop.py`, `claims.py` e os testes desses comandos estão byte a byte idênticos ao baseline. O `db.py` recebeu apenas adições (0 linhas removidas).

**Mutação:** 19 de 19 mutantes detectados (6 execução, 8 planejador, 5 webhook/fila). Um deles passou cego na primeira rodada ("worker não reagenda plano ativo"); a lacuna de cobertura foi fechada com `test_active_plan_keeps_being_scheduled_until_terminal`.

## 2. Estado encontrado (já existia — reutilizado, não recriado)
- `BridgeHub`: roteamento API → conector → navegador.
- `NavigatorReal`: HTTP real, aprovação do Owner vinculada por fingerprint, anti-replay atômico, `PolicyActionRegistry` fail-safe.
- `TokenVault` (Fernet), `OAuth2Flow`, `RateLimiter`, `WebhookDispatcher` (HMAC e backoff), `SpendGuard`.
- `ExecutionObservability`, `EscalationDesk` persistente, `JobQueue` e `GraphExecutor`.

## 3. Defeitos de produção provados e corrigidos
| # | Prova | Antes | Depois |
|---|---|---|---|
| D1 | Servidor real executa e responde após o timeout | Claim liberado; retry **publicou 2×** | 1×; `UNKNOWN`, sem reenvio até a reconciliação do Owner |
| D2 | HTTP 202 | Tratado como sucesso | `ACCEPTED`, não concluído, nunca reenviado |
| D3 | Crash real após o envio e restart | Claim apagado; **reexecutou (2×)** | `UNKNOWN_OUTCOME`; reconciliação exclusiva do Owner |
| D4 | Leitura de confirmação chamada `GET_PUBLISH_VIDEO_STATUS` (código novo deste comando) | Classificada como CRÍTICA (fail-safe correto do registry) | Ação de leitura `GET_EXECUTION_STATUS` |
| D5 | Verificação consultada a cada `advance` (código novo) | Esgotava o rate limit da plataforma | Backoff próprio (`next_verify_at`) |
| D6 | `WebhookDispatcher.deliver` com servidor real e timeout aguardando a resposta | **1 entrega lógica chegou 3× ao receptor** e ainda foi reportada como falha | Repete só o que é certo que não foi processado (falha de conexão, 408/429/503); resultado ambíguo encerra como `UNKNOWN` e 4xx como `REJECTED` |
| D7 | `JobQueue.recover_stuck_jobs(timeout_seconds=0.0)` | `or` tratava 0.0 como ausente e aplicava 300 s em silêncio | `is None` |

**Conflito de requisitos:** `test_restart_after_crash_does_not_permanently_block` exigia a reexecução automática, que é o próprio vetor de D3. O teste foi atualizado mantendo a intenção original (nunca travar: após a reconciliação, executa 1× em menos de 5 s) e ganhou a garantia contra duplicação.

## 4. Arquitetura final
```
ATLAS (Comando 2: recomendação por evidência)
  └─► navigator.planning.OperationPlanner  — ordem topológica/ciclos, pré-condições (EvidenceGate do Gêmeo),
  │                                           caminho (BridgeHub), criticidade (PolicyActionRegistry),
  │                                           irreversíveis, custo declarado ou UNKNOWN, bloqueios ANTES de executar
  └─► navigator.planning.OperationRunner   — checkpoint por transição (navigator_operation_runs), retomada
        │                                     após restart, retry/backoff (RetryPolicy), cancelamento, replanejamento
        ├─► forge.preparation.ExecutionPreparer — campos obrigatórios, recusa de credencial no payload,
        │                                          hash do payload (2ª trava contra alteração), validação da saída
        ├─► bridge.execution.navigator_real.NavigatorReal — aprovação do Owner, claim anti-replay,
        │                                                    estado de entrega, reconciliação
        │      └─► PlatformApiAdapter (token só do TokenVault, rate limit, HTTP real)
        └─► ExecutionObservability ─► sincronização do Gêmeo ─► ciclo adaptativo (aprendizado)
```

**Escada de evidência por passo:**
- PLANNED → WAITING_APPROVAL → APPROVED → RUNNING → ACCEPTED | EXECUTED → CONFIRMED (este último só com leitura real de confirmação).
- Estados de falha: RETRYING, FAILED, BLOCKED, UNKNOWN, CANCELLED.
- Plano: PLANNED, RUNNING, WAITING_APPROVAL, SUCCEEDED, PARTIAL, FAILED, BLOCKED, CANCELLED.

## 5. Provas (todas pelo caminho real, com servidor HTTP local real)
- **Idempotência:**
  - D1 e D3 reduzidos a exatamente 1 execução;
  - replay devolve o resultado em cache;
  - `advance` repetido não reenvia;
  - toda ação de escrita e toda ação desconhecida é CRITICAL (fail-safe), portanto passa pelo claim.
- **Persistência e recuperação:** restart da aplicação no meio do plano; o passo concluído não se repete; o plano termina SUCCEEDED com exatamente 2 requisições.
- **Owner:**
  - nada executa sem aprovação;
  - payload alterado após a aprovação é recusado (hash do FORGE e fingerprint do `NavigatorReal`);
  - rebaixar a criticidade não funciona;
  - a reconciliação é exclusiva do Owner.
- **Segredos:** o token chega ao serviço (`Bearer …`), mas não aparece em `navigator_operation_runs`, `execution_observability`, `audit_events`, `owner_approval_requests` nem `navigator_execution_claims`. Credencial dentro do payload é recusada pelo FORGE.
- **Confiabilidade:**
  - 503 → RETRYING com backoff, sem reenvio antes do prazo, e sucesso depois;
  - 400 → FAILED, com o dependente BLOCKED e o plano PARTIAL;
  - 202 → ACCEPTED até a confirmação real;
  - resposta sem "published" → EXECUTED, nunca CONFIRMED;
  - rate limit local → NOT_SENT e retry seguro.
- **Fluxo ponta a ponta:**
  1. ATLAS valida uma estratégia com grupo de controle e recomenda SCALE;
  2. o plano usa a pré-condição factual do Gêmeo;
  3. o Owner aprova;
  4. o FORGE prepara;
  5. o `NavigatorReal` executa e o passo chega a CONFIRMED;
  6. o Gêmeo registra `integration:youtube.last_execution_success = True`;
  7. um novo resultado real de vendas é medido pelo ciclo adaptativo.
- **Falha externa:** vira `last_execution_success = False` no Gêmeo, nunca sucesso.
- **Roteamento:** com API oficial, navegador e Computer Use disponíveis, o planejador escolhe a API oficial. Sem adapter disponível, não há fallback inseguro.

## 6. Arquivos
- **Produção:**
  - `bridge/execution/platform_adapters.py`
  - `bridge/execution/navigator_real.py`
  - `navigator/planning.py` (novo)
  - `forge/preparation.py` (novo)
  - `core/models/db.py` (tabela aditiva `navigator_operation_runs`)
- **Testes:**
  - `tests/test_comando_3_navigator_forge_execucao.py` (novo)
  - `tests/test_comando_3_navigator_planejamento_e2e.py` (novo)
  - `tests/test_microcorrecao_atomic_antireplay_strict_spend.py` (conflito da seção 3)

## 7. Lacunas do checkpoint 3 — FECHADAS
1. **Conector WEBHOOK no `NavigatorReal`** (`register_webhook` e `_dispatch_webhook`):
   - reutiliza o `WebhookDispatcher` com uma tentativa por chamada; a repetição é decidida pelo claim e pelo estado de entrega;
   - assinatura HMAC com segredo lido do `TokenVault` só no envio;
   - provas com servidor real: execução única e assinada, segredo ausente de runs, auditoria e observabilidade; timeout ambíguo gera `UNKNOWN`, sem reentrega.
2. **Acionamento por fila e worker** (`schedule_advance` e `process_advance_jobs` sobre o `JobQueue` existente):
   - um tick idempotente por job; um plano ativo é reagendado até ficar terminal;
   - prova com crash REAL do worker (`os._exit` depois de o serviço executar e o claim ser consumido, antes do checkpoint):
     - restart da fila;
     - `recover_stuck_jobs` recupera o job;
     - um novo worker retoma o plano até SUCCEEDED com **exatamente 1** requisição (o claim CONSUMED devolve o resultado).

## 8. Pendências não bloqueadoras
- **MCP_PLUGIN e COMPUTER_USE_RPA:** o projeto não possui executor para esses mecanismos, e criá-lo seria infraestrutura nova, vedada pelo modo fechamento. O roteamento os coloca depois da API oficial e do webhook, e acioná-los resulta em falha segura com motivo explícito, sem execução nem fallback inseguro.
- APIs reais das plataformas não exercitadas (sem rede nem credenciais aqui); a validação usou servidor HTTP real local.
- O mapeamento do campo de status de confirmação por plataforma deve ser configurado por adapter.
- Recomenda-se revalidar os testes novos no Windows.

## 9. Arquivos adicionais desta rodada
- `bridge/execution/webhooks.py` (D6)
- `core/jobs/queue.py` (D7)
- `bridge/execution/navigator_real.py` (despacho de webhook)
- `navigator/planning.py` (acionamento por fila)
- `tests/test_comando_3_navigator_planejamento_e2e.py` (+6 testes)
