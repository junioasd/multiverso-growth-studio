# Relatório — Microcorreção Final: 4 Pontos Residuais

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V2_CORRECTION`
(792 testes). Esta rodada corrige exatamente os 4 pontos residuais
apontados, sem implementar nenhuma expansão nova (RADAR WEB COMERCIAL,
Visão Evolutiva, raciocínio multimodal, ALL-ROUNDER — nada disso foi
tocado).

## Contagem de testes — extraída da execução real

```
Ran 825 tests
OK
```

**825 testes, 825 PASS, 0 FAIL.** 792 preservados (nenhum removido —
alguns poucos ajustados para refletir mudanças intencionais de
comportamento, listados abaixo) + **33 testes novos**
(`tests/test_microcorrection_final.py`). Suíte completa confirmada
estável em 3 execuções consecutivas.

## Lista exata dos arquivos alterados

**Reescritos:**
- `bridge/hub.py` — `best_available_execution_path` ganha `allowed_mechanisms`
- `navigator/executor.py` — reescrito por completo (mecanismo real + tools_allowed)
- `core/reasoning/engine.py` — reescrito por completo (replan/reexecute real)
- `core/reasoning/self_correction.py` — remove gravação duplicada de EXECUTE
- `core/reasoning/adapters.py` — `NavigatorTaskExecutor` propaga `tools_allowed`
- `forge/models.py` — `BlockClassification`, estado `ROLLBACK_FAILED`
- `forge/engineer.py` — `release_operation_block`/`rollback` reescritos
- `atlas/director.py` — `run_mission_with_reasoning` propaga `mission.tools_allowed`

**Novos:**
- `navigator/mechanism_executors.py`

**Testes ajustados** (comportamento mudou intencionalmente):
- `tests/test_navigator.py` — 2 testes de retry migrados de
  `nav.adapters[...]` (removido) para `register_mechanism_executor`
- `tests/test_forge.py` — 2 testes de bloqueio/liberação migrados para
  `classification=ADMINISTRATIVE` (o fluxo simplificado antigo só é
  válido para bloqueios não-técnicos agora)

**Novo arquivo de teste:**
- `tests/test_microcorrection_final.py` (33 testes)

## Ponto 1 — tools_allowed controla a execução real ✅

`BridgeHub.best_available_execution_path(platform, allowed_mechanisms=...)`
filtra os candidatos ANTES de aplicar a prioridade — um mecanismo fora
de `allowed_mechanisms` nunca é retornado. `NavigatorExecutor.execute(...,
tools_allowed=[...])` converte a lista em `ConnectorType`s e propaga.
Sem caminho permitido disponível: sempre `BLOCKED`, `evidence` traz
`requested_tools`/`allowed_tools`/`selected_tool`/`rejected_tools`.
Cadeia completa fechada: `AtlasDirector.run_mission_with_reasoning`
passa `mission.tools_allowed` → `NavigatorTaskExecutor` → `NavigatorExecutor.execute`
→ `BridgeHub`.

**Validado manualmente**: `tools_allowed=["OFFICIAL_API"]` com
OFFICIAL_API indisponível e MOCK disponível → `BLOCKED`, MOCK nunca
executado — inclusive quando a chamada vem de uma missão real do
ATLAS via `run_mission_with_reasoning` (a missão nunca chega a
`VERIFIED`, vai para `FAILED`).

## Ponto 2 — BRIDGE → NAVIGATOR → EXECUTOR REAL ✅

`navigator/mechanism_executors.py`: uma classe por `ConnectorType`
(`OfficialApiExecutor`, `ConnectorMcpExecutor`, `WebhookExecutor`,
`BrowserAutomationExecutor`, `ComputerUseExecutor`,
`MockFallbackExecutor`). Sem integração real registrada, os
executores de mecanismos "reais" levantam erro claro — nunca fingem
sucesso. `NavigatorExecutor` agora chama o executor correspondente ao
mecanismo que o BRIDGE selecionou (`register_mechanism_executor` é o
ponto de integração real futura). Antes de `SUCCESS`,
`selected_mechanism` é verificado contra o `mode` genuinamente
devolvido — incompatibilidade (mecanismo real + modo MOCK) sempre
vira `FAILED`.

**Validado manualmente e testado**: API configurada + executor real
injetado → sucesso com `mode=REAL`, `actual_executor` correto;
inconsistência mecanismo/executor → sempre falha; nenhum executor
disponível → `BLOCKED`.

## Ponto 3 — ReasoningEngine: REPLAN + REEXECUTE real ✅

`ReasoningEngine._run_task_with_replan` — loop genuíno limitado por
`max_replan_attempts` (nunca infinito): VERIFY falha → CRITIQUE →
REPLAN (ajusta a tarefa, força nova seleção de ferramenta entre as
`available_tools` ainda não tentadas) → REEXECUTE → VERIFY de novo.
Erro de permissão nunca é contornável por replan — escala
imediatamente. Cada tentativa registrada separadamente no trace.

**Validado manualmente o cenário exato exigido**: executor com
tentativa 1 inválida / tentativa 2 sucesso → executor chamado 2 vezes,
REPLAN registrado, VERIFY final PASS. 3 falhas até o limite → FAILED
sem loop infinito. Idempotência e proteção de ação crítica preservadas
através de múltiplas rodadas de replan via NAVIGATOR real (só 1
aprovação criada, nunca duplicada).

## Ponto 4 — FORGE: release e rollback reais ✅

**4.1** `release_operation_block()` agora distingue
`BlockClassification.TECHNICAL` (padrão — exige o diagnóstico em
`VALIDATED`, ou seja, fix aplicado + testado + não-regressão +
validado, antes de aceitar liberar; caso contrário
`InvalidPipelineTransition` com "RELEASE DENIED") de
`ADMINISTRATIVE` (fluxo simplificado, mas exige `justification`
explícita e auditada).

**4.2** `execute_repair()` persiste o backup do `RepairExecutor` na
evidência do diagnóstico (`repair_backup` — sobrevive a restart).
`rollback(..., executor=...)` restaura o recurso de verdade a partir
desse backup, valida a restauração comparando o estado, e só então
confirma `ROLLED_BACK`. Falha na restauração → `ROLLBACK_FAILED`
(nunca terminal — operação continua bloqueada, nunca finge sucesso).

**Validado manualmente todos os cenários exigidos**: autorização
sozinha não libera bloqueio técnico; fix sem teste não libera; teste
sem validation não libera; fluxo completo libera; rollback restaura
A→B→A de verdade; rollback funciona após restart (backup persistido);
rollback sem checkpoint preserva o comportamento anterior; falha
parcial em rollback → `ROLLBACK_FAILED`, operação continua bloqueada;
múltiplos fixes sequenciais — rollback restaura ao estado anterior à
correção mais recente.

## Testes adversariais — cobertura exata pedida
Todos os cenários explicitamente listados no pedido foram
implementados como testes formais em `tests/test_microcorrection_final.py`
E validados manualmente antes de serem formalizados — ver seções
acima para a correspondência ponto a ponto.

## Pendências reais
- Nenhuma integração externa real (mantido). Os executores "reais" por
  mecanismo (`OfficialApiExecutor` etc.) são pontos de integração
  prontos — uma integração de verdade se conecta via
  `register_mechanism_executor` sem alterar o NAVIGATOR.
- `rollback()` com executor requer que o CHAMADOR reconecte o mesmo
  `resource_store` após um restart (FORGE não pode reconstruir
  recursos externos sozinho) — documentado no docstring do método.
- RADAR WEB COMERCIAL, Visão Evolutiva, raciocínio multimodal e
  ALL-ROUNDER não foram implementados, conforme instruído.
