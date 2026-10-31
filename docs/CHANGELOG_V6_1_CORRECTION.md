# CHANGELOG — Correção V6.1: 11 Resíduos da Auditoria da Expansão V6

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_AGENTIC_RADAR`
(917 testes). Esta rodada corrige os 11 pontos residuais apontados
pela auditoria, sem implementar nenhuma expansão nova.

## Contagem de testes — extraída da execução real

```
Ran 991 tests
OK
```

**991 testes, 991 PASS, 0 FAIL.** 917 preservados (6 testes
atualizados para refletir mudanças de comportamento explicitamente
exigidas — listados abaixo, nenhum enfraquecido) + **74 testes
novos**. Suíte completa confirmada estável em 3 execuções
consecutivas.

## Arquivos alterados

**Novos:**
- `core/coordination/delegation.py` — enforcement do agente designado
- `core/agents/base.py` — `AgentRegistry`, `AgentRegistration`, `ScaffoldAgent`
- `core/agents/default_registry.py` — os 25 agentes registrados
- `radar_web/scheduler.py` + `radar_web/worker.py` — execução contínua
- `radar_web/quality_gate.py` — `RadarWebEvidenceGate`

**Reescritos:**
- `core/capabilities/registry.py` — `CapabilityHealth` corrigido
- `radar_web/models.py` — `compute_strategy_fingerprint()`, Knowledge Freshness
- `radar_web/engine.py` — `MockStrategySource` usa fingerprint estável
- `radar_web/evolution.py` — `strategy_version`/`annual_snapshot_version`/`revision_number` separados
- `radar_web/adoption.py` — pipeline completo com Quality Gate obrigatório
- `atlas/director.py` — `delegation_registry`, `agent_registry`, `radar_web_scheduler`/`worker`, `resolve_agent_for_task`, `delegate_task`, `run_mission_with_reasoning` com enforcement
- `api/bootstrap.py` — shutdown para o worker do RADAR WEB COMERCIAL também
- `core/models/db.py` — tabelas `radar_web_schedule` + colunas aditivas em `strategy_history`

**Testes atualizados** (mudança de comportamento explicitamente exigida):
- `tests/test_capabilities_and_multimodal.py` — `HealthStatus` genérico → `CapabilityHealth`
- `tests/test_radar_web_comercial.py` — `AdoptionPipelineTests` reescrita para o novo fluxo com Quality Gate

**Novos arquivos de teste:**
- `tests/test_correction_v61_agentic_agents_radar.py` (39 testes — itens 1, 2, 3, 9, 10)
- `tests/test_correction_v61_evolution_quality_adoption.py` (21 testes — itens 4, 5, 6, 11)
- Mais 14 testes adicionados aos arquivos existentes (health/multimodal)

## Os 11 pontos corrigidos

### Item 1 — Agentic-first: executor respeita o agente designado ✅
**Causa raiz**: `run_mission_with_reasoning()` sempre executava via
NAVIGATOR, mesmo com `mission.agent="FORGE"`. **Correção**:
`core.coordination.delegation.resolve_authorized_executor()` — DIRECT
(mesmo agente) ou DELEGATED (via `DelegationRecord` auditável) são os
ÚNICOS caminhos válidos; qualquer outro caso levanta
`InvalidExecutorError` e a missão vai para `BLOCKED`, nunca `VERIFIED`.
`AtlasDirector.delegate_task()` é o mecanismo explícito e auditável
(`original_agent`/`delegated_agent`/`reason`/`permissions`/
`capabilities`/`task_scope`/`evidence`) — o agente original continua
dono da missão (`mission.agent` nunca muda).

### Item 2 — 25 agentes com base honesta ✅
`core.agents.base.AgentRegistry` + `default_registry.
build_default_agent_registry()`: 25 agentes registrados —
ATLAS/NAVIGATOR/FORGE `ACTIVE` (contrato real), SENTINEL/BRIDGE/
RADAR/RADAR_WEB_COMERCIAL `PARTIAL`, os demais 18 `SCAFFOLD` (nunca
fingem execução — `AgentNotImplemented`). `AgentRegistry.register()`
recusa declarar `ACTIVE` sem `contract` real.

### Item 3 — RADAR WEB COMERCIAL: execução contínua real ✅
`radar_web/scheduler.py` + `radar_web/worker.py` — mesmo padrão já
hardened do RADAR de oportunidades, tabela `radar_web_schedule`
PRÓPRIA e SEPARADA. `last_run`/`next_run`/`status`/`error`/`duration`/
`discoveries` registrados. Concorrência protegida via
`JobQueue.try_start()` + checagem adicional de estado terminal (item
que corrigiu um bug real: execução manual duplicando a automática em
modo memória).

### Item 4 — Visão Evolutiva: identidade estável ✅
`radar_web.models.compute_strategy_fingerprint()` — hash estrutural
(mecanismo+nicho+canal), nunca UUID aleatório. `strategy_version`
(global) separado de `annual_snapshot_version`/`revision_number`
(dentro do ano) — segunda revisão no mesmo ano exige `revision_reason`
explícito (`DuplicateAnnualReviewError` sem isso).

### Item 5 — SENTINEL: Quality Gate para RADAR WEB COMERCIAL ✅
`radar_web.quality_gate.RadarWebEvidenceGate` — reaproveita
`core.quality.gates.QualityGate`. `{"roi": 999999}` sem source/
evidence → sempre `REJECTED`/`BLOCKED`, nunca `APPROVED`. `ESTIMATE`
com evidência parcial nunca promovido a `FACT`.

### Item 6 — Pipeline de adoção comercial corrigido ✅
`radar_web.adoption.AdoptionPipeline` — fluxo completo DISCOVERY→
CLASSIFICATION→EVIDENCE_COLLECTION→ANALYSIS→HYPOTHESIS→
SENTINEL_QUALITY_GATE→CONTROLLED_TEST→METRICS→VALIDATION→ADOPT/
RETEST/REJECT. `SENTINEL_QUALITY_GATE` é etapa obrigatória —
`REJECTED`/`BLOCKED` do gate nunca avança para `CONTROLLED_TEST`.
Métricas exigem `provenance` explícita.

### Item 7 — Capability Registry: health real ✅
Novo `CapabilityHealth` (HEALTHY/DEGRADED/UNKNOWN/UNHEALTHY/DISABLED)
corrige o bug relatado (`UNKNOWN` tratado como utilizável). `DEGRADED`
só usável com `allow_degraded=True` explícito. `record_health_check()`
é o único caminho para mudar saúde.

### Item 8 — Multimodal: capacidade real vs declarada ✅
`CapabilityRegistration.effective_status` (`EffectiveCapabilityStatus`:
ACTIVE/MOCK/PARTIAL/UNAVAILABLE) — provider `MOCK` NUNCA reporta
`ACTIVE`, mesmo com `available=True`/`health=HEALTHY`.

### Item 9 — ALL-ROUNDER: resolução de conflito ✅
`AtlasDirector.resolve_agent_for_task()` — prioridade exata exigida:
1) especialista do domínio, 2) especialista secundário compatível,
3) ALL-ROUNDER autorizado (respeitando `required_capability` quando
especificado), 4) escalonamento. Rationale resumido e auditável,
nunca chain-of-thought privado.

### Item 10 — ATLAS: orquestração corrigida ✅
Mesmo método consulta `agent_registry` + `capability_registry`
(saúde/especialização/disponibilidade/permissões/autoridade) — nunca
seleciona por nome/fallback genérico. Um `preferred_agent` inexistente
no registro nunca é selecionado silenciosamente.

### Item 11 — RADAR WEB COMERCIAL + Biblioteca Mestre ✅
`AtlasDirector.promote_strategy_to_library()` só promove um
`AdoptionRecord` já `ADOPTED` (passou pelo Quality Gate + teste
controlado real). Registro `REJECTED` permanece recuperável via
`adoption_pipeline.get()` — nunca desaparece.

## Testes adversariais — evidência
Todos os cenários explicitamente exigidos foram validados
MANUALMENTE antes de formalizados em teste — ver
`docs/TEST_REPORT_V6_1.md` para a evidência ponto a ponto.

## Pendências reais
- 18 dos 25 agentes permanecem `SCAFFOLD` — contrato/registro formal
  existe, execução real não implementada (declarado honestamente, não
  uma lacuna oculta).
- `resolve_agent_for_task()` é uma primeira formalização da
  orquestração corrigida — não cobre toda heurística possível de
  seleção (ex: balanceamento de carga entre múltiplos especialistas
  secundários empatados), mas a prioridade OBRIGATÓRIA (1→2→3→4) está
  implementada e testada.
- Nenhuma integração externa real (mantido). RADAR WEB COMERCIAL
  continua 100% MOCK.
