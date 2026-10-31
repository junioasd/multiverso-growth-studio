# CHANGELOG — Microcorreção V6.1.1

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_CORRECTION`
(991 testes). Corrige os pontos residuais apontados, sem implementar
o módulo YouTube nem nenhuma outra expansão nova.

## Contagem de testes — extraída da execução real

```
COLLECTED/PASS: 1025, FAIL: 0, ERROR: 0
Threads remanescentes após a suíte completa: 0
```

**1025 testes, 1025 PASS, 0 FAIL.** 992 preservados (8 testes
atualizados para refletir mudanças de comportamento explicitamente
exigidas — listados abaixo) + **33 testes novos** em
`tests/test_microcorrection_v611.py`. Suíte completa (monolítica,
`python3 -m unittest discover`) confirmada estável em 3 execuções
consecutivas, **sem nenhuma thread órfã** ao final (verificado
programaticamente via `threading.enumerate()`).

> **Nota sobre o item 16 (pytest)**: este ambiente de execução não
> tem acesso à rede para instalar `pytest` (tentativa registrada:
> `pip install pytest` falhou por ausência de repositório
> acessível). A "execução monolítica única" foi realizada com
> `python3 -m unittest discover -s tests` — o executor de testes
> real disponível neste ambiente — com contagem COLLECTED/PASS/FAIL
> extraída programaticamente (ver acima) e verificação explícita de
> ausência de threads órfãs. Pendência real declarada na seção
> correspondente.

## Arquivos alterados

**Reescritos:**
- `core/agents/base.py` — `agentic_architecture_supported`/
  `agentic_runtime_available`, `all_rounder_capable`/
  `all_rounder_runtime_available`, `aliases` + `resolve_canonical_id()`,
  `MultimodalCapabilityEntry` (supported/available/provider/status)
- `core/agents/default_registry.py` — os 25 agentes OFICIAIS restaurados
  (nunca mais os nomes inventados de rodadas anteriores); multimodal
  por domínio; `ActiveAgentContract` com `primary_task_types` separado
  de `all_rounder_task_types`
- `radar_web/quality_gate.py` — corrige a brecha "FACT substitui
  evidência"; adiciona `missing_evidence`/`unsupported_claims`/
  `metric_issues`
- `atlas/director.py` — `resolve_agent_alias()`, capability_registry
  agora chaveado por nome canônico (nunca mais `principal.subject`),
  `resolve_agent_for_task()` corrigido (ALL-ROUNDER nunca escolhido só
  por `all_rounder=True`)
- `api/bootstrap.py` — `start_radar_web_worker` (autostart real,
  independente do `start_radar_worker` do RADAR de oportunidades)
- `core/evidence/claims.py` — lista de métricas protegidas expandida

**Testes atualizados** (comportamento mudou intencionalmente — a
própria correção pedida):
- `tests/test_correction_v61_agentic_agents_radar.py` — nome de campo
  `name`→`display_name`; "PRISM"→"PRISMA" (nome oficial); tier
  ALL_ROUNDER corrigido (2 testes); + `setUp`/`tearDown` com
  rastreamento de workers (item 14)
- `tests/test_visao_evolutiva_e_agentic.py` — alias inválido agora
  levanta `UnknownAgentAlias` em vez de devolver `False`
- `tests/test_radar_web_comercial.py` +
  `tests/test_correction_v61_evolution_quality_adoption.py` —
  `_good_strategy()` usa `observed_at=time.time()` (não mais `0`/1970,
  que agora corretamente dispara staleness com a barra FACT mais
  rigorosa)

**Novo arquivo de teste:**
- `tests/test_microcorrection_v611.py` (33 testes)

## Os pontos corrigidos

### Item 1 — RADAR WEB COMERCIAL: autostart real no runtime ✅
`build_app(start_radar_web_worker=True)` — parâmetro INDEPENDENTE de
`start_radar_worker` (RADAR de oportunidades). Validado
end-to-end: worker inicia junto com a app, `last_run`/`status`/
`discoveries` mudam sozinhos após o intervalo, sem nenhuma chamada
manual. `shutdown()` encerra ambos os workers.

### Item 2 — 25 agentes: estrutura oficial restaurada ✅
Os 25 nomes exatos (ATLAS, ORION, SAGE, PRISMA, LIBRA, MERCURY,
VENTURE, NEXUS, RELAY, ARGOS, SCRIPTOR, VERITAS, AURORA, FRAME, VOX,
SONORA, PIXEL, PULSE, BRIDGE, SENTINEL, JURIS, METRICA, ECHO,
NAVIGATOR, FORGE) — nenhum substituído silenciosamente. Cada um com
registro canônico único: `agent_id`/`display_name`/`role`/
`specialization`/`level`/`implementation_status`/`agentic_enabled`/
`all_rounder`/`multimodal_capabilities`/`capabilities`/`tools`/
`permissions`/`authority`/`health`.

### Item 3/4 — Agentic-first e ALL-ROUNDER transversais ✅
`agentic_architecture_supported` (todos os 25) separado de
`agentic_runtime_available` (só ATLAS/NAVIGATOR/FORGE). Mesma
separação para `all_rounder_capable`/`all_rounder_runtime_available`.

### Item 5 — Identidade canônica dos agentes ✅
`AgentRegistry.resolve_canonical_id()` — `'NAVIGATOR'`, `'navigator'`,
`'navigator@system'` resolvem para o mesmo agente. Alias inválido
levanta `UnknownAgentAlias` explicitamente (nunca falso silencioso).
`AtlasDirector.resolve_agent_alias()` sempre audita a resolução.
`capability_registry` agora chaveado por nome canônico em vez de
`principal.subject` — elimina a inconsistência relatada.

### Item 6/7 — Multimodal realista ✅
ATLAS: 12 modalidades arquiteturalmente suportadas (nunca
`multimodal_capabilities=[]`), todas `MOCK`/`UNAVAILABLE` — nunca
`ACTIVE` sem provider real. Por agente conforme domínio: FORGE
(CODE/LOG/SCREENSHOT/DOCUMENT), NAVIGATOR (WEB_PAGE/SCREENSHOT/TEXT/
STRUCTURED_DATA), PIXEL (IMAGE/SCREENSHOT/TEXT), FRAME (VIDEO/IMAGE/
AUDIO/TEXT), etc.

### Item 8 — Quality Gate: FACT exige evidência ✅
**Bug real corrigido**: `has_evidence = bool(evidence_ids) or
claim_type == FACT` permitia que `FACT` sozinho substituísse
evidência. Agora `has_evidence` depende SOMENTE de `evidence_ids`;
`FACT` aumenta a barra (source+evidence_ids+freshness+confidence≥0.7),
nunca a substitui. Validado com o cenário EXATO da auditoria:
`claim_type=FACT, source="https://example.com", evidence_ids=[],
roi=999999` → sempre `REJECTED`, nunca `APPROVED`.

### Item 9 — Métricas comerciais protegidas ✅
Lista expandida: ROI, CONVERSION_RATE, CAC, LTV, REVENUE, SALES,
SALES_VOLUME, CTR, CPC, CPA, ROAS, AOV, REFUND_RATE.

### Item 10 — SENTINEL: campos de diagnóstico ✅
`GateResult.criteria` agora sempre inclui `missing_evidence`,
`unsupported_claims`, `metric_issues`.

### Item 11 — Pipeline de adoção ✅ (preservado)
Fluxo DISCOVERY→...→ADOPT/RETEST/REJECT já corrigido na rodada
anterior — confirmado intacto. Métrica sem `provenance` é sempre
recusada (`ValueError`) antes mesmo de avançar — mais estrito que
apenas marcar UNVERIFIED, e nunca permite ADOPT só por presença de
número.

### Item 12 — Capability Registry ✅ (preservado)
`CapabilityHealth` (HEALTHY/DEGRADED/UNKNOWN/UNHEALTHY/DISABLED) e
`record_health_check()` já corrigidos na rodada anterior — confirmado
intacto pelos testes.

### Item 13 — resolve_agent_for_task corrigido ✅
**Bug real corrigido**: o tier ALL_ROUNDER escolhia o primeiro agente
com `Capability.ALL_ROUNDER` usável SEM checar se ele genuinamente
sabia lidar com a tarefa — uma tarefa completamente desconhecida caía
automaticamente em ATLAS/NAVIGATOR/FORGE. Agora `can_handle()` é
SEMPRE exigido (via `primary_task_types` ou `all_rounder_task_types`
do contrato) — tarefa que ninguém sabe tratar sempre escala
(`ESCALATION`), nunca cai por padrão em quem só tem
`all_rounder=True`.

### Item 14 — Isolamento entre testes / suíte monolítica ✅
Toda classe de teste que cria `RadarWebWorker` agora usa `setUp`/
`tearDown` com rastreamento explícito — worker é parado mesmo se o
teste falhar no meio. Verificado programaticamente: 0 threads
remanescentes após a suíte completa (1025 testes) em execução única.

## Pendências reais
- `pytest` não pôde ser instalado neste ambiente (sem acesso à rede)
  — a execução monolítica foi validada com `python3 -m unittest
  discover`, o executor disponível, com contagem exata e verificação
  de threads órfãs.
- 20 dos 25 agentes permanecem `SCAFFOLD` (declarado honestamente,
  inalterado desde a rodada anterior).
- Módulo YouTube Recommendation & Distribution Engine explicitamente
  NÃO implementado nesta rodada, conforme instruído — fica para a
  próxima etapa após aprovação desta correção.
