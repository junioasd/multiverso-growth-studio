# CORREÇÃO PÓS-AUDITORIA INDEPENDENTE — MULTIVERSO GROWTH STUDIO

**Auditoria:** `docs/evidencias/AUDITORIA_INDEPENDENTE_GROWTH_STUDIO_2026-09-24.md`, veredito **NÃO APROVADO** com 4 bloqueadores.
**Base corrigida:** `MULTIVERSO_GROWTH_STUDIO_MASTER_FINAL_2026-09-24.zip` (SHA `9b498594…cf7e4`, verificado).
**Status:** correções concluídas; o pacote **não** está aprovado para operação real. A decisão depende de nova auditoria independente.

## 1. Resultado
| | MASTER anterior | MASTER corrigido |
|---|---|---|
| Testes | 2.031 | 2.040 (+9) |
| PASS / FAIL / ERROR | 2.029 / 0 / 0 | 2.038 / 0 / 0 |
| SKIP | 2 (Windows real) | 2 (Windows real) |
| RC / ResourceWarning / compileall | 0 / 0 / OK | 0 / 0 / OK |
| Bloqueador A (mock → VERIFIED) | **VERIFIED** | FAILED + `SIMULATED_NOT_VERIFIED` |
| Bloqueador B (navegar = publicar) | **success=True** | NOT_SENT, navegador nem acionado |
| Bloqueador C (replay de pagamento divergente) | **aceito (1 e 999)** | `JourneyConflict` |
| Bloqueador D (`order_id` reutilizado divergente) | **aceito em silêncio** | `JourneyConflict` |

Os 2 skips (`test_real_windows_*`) exigem Windows real e foram aprovados em Windows real no Comando 1. A auditoria registrou 7 skips em Python 3.13 por diferenças de plataforma (Chromium do Playwright, `pid_max`); ver as observações da auditoria.

**Evidência reproduzível:**
- script: `docs/evidencias/reproducao_bloqueadores_auditoria.py`;
- saídas: `reproducao_ANTES_master_anterior.txt` (defeitos presentes) e `reproducao_DEPOIS_corrigido.txt` (todos rejeitados).

## 2. Causas-raiz e correções (somente a causa)
### A — `MOCK_FALLBACK` produzia missão VERIFIED
- **Causa-raiz:** `NavigatorResultVerifier.verify` só comparava `execution_result == SUCCESS` e ignorava a evidência `mode=MOCK` e `selected_mechanism=MOCK_FALLBACK`.
- **Correção:** o verificador rejeita evidência simulada. A missão termina **FAILED** com `SIMULATED_NOT_VERIFIED` e a evidência simulada é preservada.
- **Causa associada, revelada ao corrigir:** o `ReasoningEngine` re-executava uma tarefa que o executor já reportara como SUCCESS mas não verificada. Com mock gerava 3 execuções; com execução real seria vetor de duplicação. Agora um SUCCESS não verificável **nunca é re-executado**. Falhas técnicas e FAILED continuam replanejáveis.
- **Arquivos:** `core/reasoning/adapters.py`, `core/reasoning/engine.py`, `atlas/director.py` (mensagem de classificação).

### B — navegador marcava ação mutável como concluída por abrir a página
- **Causa-raiz:** `NavigatorReal._dispatch_browser` descartava `action` e `params` e tratava "página aberta" como efeito da ação, consumindo a aprovação crítica.
- **Correção:** o navegador só atende ações que o `PolicyActionRegistry` classifica como ROUTINE (leitura). Ação mutável (PUBLISH, SEND, UPDATE, CREATE, BUY, DELETE…) recebe `NOT_SENT` **antes** de qualquer efeito. A aprovação não é consumida e o navegador nem é acionado. A leitura continua funcionando, com `EXECUTED`.
- **Arquivo:** `bridge/execution/navigator_real.py`.
- **Nota:** o executor de navegador para mutações continua `BLOCKED` na matriz, porque não há executor catalogado que execute e confirme a mutação.

### C — replay de evento de pagamento não era vinculado ao conteúdo
- **Causa-raiz:** `OrderJourney._transition` tratava qualquer `event_id` já processado como retry. `confirm_payment` só validava o valor para eventos novos.
- **Correção:**
  - fingerprint imutável por evento (`order_id` + transição + evidência + valor), persistido na coluna aditiva `commercial_orders.event_fingerprints_json`;
  - mesmo ID com mesmo conteúdo é retry idempotente; mesmo ID com conteúdo diferente gera `JourneyConflict`;
  - vale para pagamento, reembolso, entrega e demais transições, inclusive na via concorrente e após restart;
  - linhas legadas sem fingerprint mantêm o comportamento anterior (documentado).
- **Arquivos:** `commercial/journey.py`, `core/models/db.py`.

### D — `order_id` reutilizado com payload diferente era aceito
- **Causa-raiz:** `start_checkout` usava `INSERT OR IGNORE` sem comparar o payload existente com o novo.
- **Correção:** compara canal, valor, lead, produto, oferta, campanha, origem de tráfego, custo e comissão. Divergência gera `JourneyConflict` (com os campos divergentes); mesmo payload continua idempotente.
- **Arquivo:** `commercial/journey.py`.

## 3. Testes
**Adicionado:** `tests/test_correcao_auditoria_independente.py`, com 9 testes que usam as mesmas provas da auditoria:

| Bloqueador | Testes |
|---|---|
| A | missão via MOCK nunca VERIFIED, com execução única; contrato do verificador (REAL continua verificável); motor não repete SUCCESS |
| B | 6 ações mutáveis recusadas sem navegar e sem consumir a aprovação; leitura continua via navegador |
| C | replays 1 e 999 e evidência divergente geram conflito; retry idêntico é aceito; reembolso e entrega também vinculados; fingerprint sobrevive a restart |
| D | canal, valor, oferta e campanha divergentes geram conflito; payload idêntico é idempotente |

**Mutação:** 7 de 7 mutantes detectados, incluindo os 4 defeitos originais reintroduzidos. Runner estrito: RC 0.

**Testes existentes atualizados porque codificavam o próprio defeito auditado.** Nenhum foi removido, e a intenção principal de cada um foi preservada e reforçada:
- `tests/test_visao_evolutiva_e_agentic.py::test_full_mission_lifecycle_via_reasoning_engine`
- `tests/test_reasoning_operational.py::test_atlas_mission_reaches_verified_only_with_real_success`
- `tests/test_correction_v61_agentic_agents_radar.py::test_direct_execution_by_designated_agent_never_needs_delegation`
- `tests/test_correction_v61_agentic_agents_radar.py::test_valid_delegation_allows_execution`
- `tests/test_microcorrection_final.py::test_mission_with_allowed_mock_still_verifies`
- `tests/test_microcorrection_v4.py::test_full_chain_replan_navigator_idempotency_executor_real`

Os seis exigiam VERIFIED sobre `MOCK_FALLBACK` (o de v4 usa um executor que é subclasse de `MockFallbackExecutor`). Agora exigem execução autorizada (não BLOCKED), execução única e `SIMULATED_NOT_VERIFIED`. O replanejamento do teste v4 continua provado (2 chamadas, passos REPLAN).

Mais dois ajustes de teste, também sem afrouxar:
- `tests/test_comando_5_maquina_comercial.py::test_retries_never_duplicate…`: o retry omitia comissão e custo e dependia do defeito D. Agora repete o **mesmo** payload, conforme o próprio comentário do teste.
- `tests/test_microcorrecao_seguranca_bloco_final2.py`: `types` foi incluído na lista stdlib (`sys.stdlib_module_names`), o mesmo falso positivo tratado para `py_compile` no Comando 2.

## 4. Integridade dos Comandos 1–6
- **Produção alterada:** apenas os 6 arquivos acima (`adapters.py`, `engine.py`, `director.py`, `navigator_real.py`, `journey.py`, `db.py`, este com coluna aditiva).
- Nenhum outro arquivo de produção dos Comandos 1–6 foi alterado.
- As suítes dedicadas de C1–C6 estão verdes dentro da regressão completa.

## 5. Mock × execução real
- `MOCK_FALLBACK` nunca produz VERIFIED nem `verified=True`, e nunca é re-executado para "virar" real.
- O `NavigatorReal` já devolvia `success=False` com `is_mock_fallback` (verificado no Comando 6).
- A escada de evidência do Comando 3 (EXECUTED ≠ CONFIRMED) continua intacta.
