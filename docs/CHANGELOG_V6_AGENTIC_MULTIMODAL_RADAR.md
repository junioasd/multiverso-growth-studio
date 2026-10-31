# CHANGELOG — Expansão V6: Agentic-First, Multimodal, ALL-ROUNDER, RADAR WEB COMERCIAL

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V5_FINALISSIMA`
(851 testes). Nenhuma funcionalidade aprovada foi removida ou
enfraquecida — apenas extensões aditivas (novos módulos, novas
tabelas, novos campos, novos métodos em `AtlasDirector`).

## Contagem de testes — extraída da execução real

```
Ran 917 tests
OK
```

**917 testes, 917 PASS, 0 FAIL.** 851 preservados integralmente + **66
testes novos** em 4 arquivos.

## Novos arquivos de teste
| Arquivo | Testes | Cobertura |
|---|---|---|
| `tests/test_capabilities_and_multimodal.py` | 25 | Capability Registry, multimodal (texto+imagem, screenshot+instrução, documento+tabela, áudio, vídeo, combinação, provider indisponível, confidence reduzido, provenance) |
| `tests/test_radar_web_comercial.py` | 26 | descoberta, classificação, scoring, evidência, adoção, Biblioteca Mestre, persistência, Knowledge Freshness |
| `tests/test_visao_evolutiva_e_agentic.py` | 15 | cenário exato de 3 anos (item 33), missão completa do ATLAS, Owner Approval, bloqueio por falta de autoridade, colaboração ALL-ROUNDER |

## Novos módulos

### Item 1-3 — Arquitetura agentic-first
Formalizada via `core.reasoning.engine.ReasoningEngine` (já existente,
preservado) — ciclo OBSERVE→UNDERSTAND→PLAN→DECOMPOSE→SELECT_TOOLS→
EXECUTE→VERIFY→CRITIQUE→CORRECT→REPLAN→LEARN→REPORT já cobre
integralmente o fluxo-base exigido (OBJECTIVE→...→COMPLETE — COMPLETE
corresponde ao encerramento do ciclo com `results` consolidados).
Nenhuma alegação de AGI real ou consciência é feita em nenhum lugar do
código ou desta documentação.

### Item 2 — ATLAS como IA agêntica master
`AtlasDirector` já concentra planejamento (`ObjectivePlanner`/
`AdvancedPlanner`), delegação (`delegate_mission`), coordenação
(`MissionTracker`), execução via `run_mission_with_reasoning`,
verificação (`missions.verify`), escalonamento
(`request_critical_action`) e consolidação de evidência (traces do
`ReasoningEngine`). Capacidade/autonomia/autoridade permanecem
SEPARADAS: `capability_registry` (o que ATLAS tecnicamente consegue),
`Role.ATLAS` no RBAC (autonomia operacional), Owner Approval (a única
fonte de autoridade para ações críticas — inalterado).

### Item 4 — ALL-ROUNDER (`core/capabilities/registry.py` + `AtlasDirector.request_all_rounder_assistance`)
`Capability.ALL_ROUNDER` registrada para ATLAS/NAVIGATOR/FORGE na
inicialização (concedida pelo bootstrap, nunca autodeclarada).
`request_all_rounder_assistance()` só concede colaboração quando o
agente-alvo genuinamente possui a capacidade registrada e usável —
nunca ultrapassa permissões, nunca contorna Owner Approval.

### Item 5-6 — Raciocínio multimodal + Evidence Layer
`core/multimodal/evidence.py` (`MultimodalEvidence`, `Modality` — 14
tipos exatos exigidos) + `core/multimodal/models.py`
(`MultimodalReasoner`, `MockModalityAnalyzer` — MOCK explícito, nunca
finge análise; `register_analyzer()` é o ponto de integração real
futura). `core/evidence/claims.py` (`ClaimType`: FACT/ESTIMATE/
INFERENCE/HYPOTHESIS/UNKNOWN, reutilizado também pelo RADAR WEB
COMERCIAL).

### Item 7-10, 19 — RADAR WEB COMERCIAL (`radar_web/`)
- `models.py` — `FunnelStrategy`, `FunnelMechanism` (22 mecanismos +
  `OTHER` como escape-hatch), `FunnelMaturity` (8 classificações);
  `with_maturity_enforced()` rebaixa estruturalmente HIGH_SCALE/MATURE
  para UNKNOWN sem evidência FACT + confiança ≥ 0.7 — nunca
  classificado por opinião.
- `engine.py` — `RadarWebComercial` + `StrategySource` (arquitetura
  plugável, mesma disciplina do `atlas.radar.engine` já aprovado);
  `MockStrategySource` nunca inventa nicho/oferta/canal — evidência
  sempre `UNKNOWN`/`confidence=0.0` até uma fonte real ser conectada.
- `scoring.py` — `FunnelIntelligenceScore` (12 dimensões exatas
  exigidas) com `confidence` SEMPRE separado de `overall`.

### Item 11-14, 18 — Visão Evolutiva (`radar_web/evolution.py`)
`StrategyHistoryStore` — APPEND-ONLY, nunca sobrescreve; `AnnualReviewEngine.
run_annual_review()` implementa o ciclo completo (observar → comparar
com o ano anterior → detectar mudança → registrar nova versão).
`compare_years()` permite comparação longitudinal. Persistência via
`db_path` (tabela `strategy_history`, aditiva).

### Item 13 — Knowledge Freshness
Campos `created_at`/`validated_at`/`last_reviewed_at`/`valid_until` em
`FunnelStrategy` + propriedades `freshness_score` (decai linearmente
em 365 dias) e `is_stale` — nunca apresenta conhecimento obsoleto como
atual sem alerta explícito.

### Item 14 — Biblioteca Mestre + Visão Evolutiva
`AtlasDirector.promote_strategy_to_library()` — só promove
conhecimento de um `AdoptionRecord` já `ADOPTED` (nunca pula o teste
antes de adoção); usa `MasterLibrary.add()` já existente (versionamento
preservado, nunca sobrescreve).

### Item 15-16 — ATLAS + RADAR WEB COMERCIAL + NEXUS/MERCURY
`AtlasDirector.consult_radar_web()` — devolve candidatos com evidence/
confidence explícitos; NUNCA aceita automaticamente (nota explícita no
retorno). Integração formal com NEXUS/MERCURY como agentes concretos
fica como pendência declarada (ver seção de pendências) — a
arquitetura (Capability Registry + ReasoningEngine + fluxo RADAR→
ATLAS→...→ECHO) está pronta para recebê-los sem redesenho.

### Item 17 — Teste antes de adoção (`radar_web/adoption.py`)
`AdoptionPipeline` — mesma disciplina de state machine do FORGE/
missões: DISCOVER→ANALYZE→HYPOTHESIS→CONTROLLED_TEST→METRICS→
VALIDATE→ADOPTED/REJECTED/RETEST. Métricas vazias sempre recusadas —
nunca avança sem dado real do teste controlado.

### Item 21 — Evidência vs inferência
`core/evidence/claims.py` — `ClaimType` + `NEVER_FABRICATED_METRICS`
(roi, faturamento, conversion_rate, cac, ltv, sales_volume — nunca
inventados; só entram como FACT vindos de fonte verificável).

### Item 25-26 — Capability Registry + Competência (`core/capabilities/registry.py`)
16 capacidades exatas exigidas; 5 níveis de competência
(JUNIOR/PLENO/SENIOR/ESPECIALISTA/MASTER); `granted_by` — nunca
autodeclarado; `is_usable()` nunca declara disponível sem
`available` + `authorized` + saúde ≠ DOWN simultaneamente.

## Arquivos alterados
- `atlas/director.py` — novos campos/métodos (capability_registry,
  multimodal_reasoner, radar_web, strategy_history, annual_review,
  adoption_pipeline, `consult_radar_web`, `run_annual_strategy_review`,
  `promote_strategy_to_library`, `request_all_rounder_assistance`)
- `core/models/db.py` — nova tabela `strategy_history` (aditiva)

## Pendências reais
- Os "25 agentes principais" citados (NEXUS, MERCURY, SAGE, METRICA,
  ECHO, etc.) NÃO foram implementados como classes de agente
  concretas nesta rodada — apenas ATLAS, NAVIGATOR, FORGE existem como
  executores reais. O Capability Registry e o `ReasoningEngine` foram
  desenhados para acomodá-los sem redesenho, mas sua implementação
  individual fica para uma etapa futura explicitamente autorizada
  (o escopo de implementar 22+ agentes novos com lógica própria
  excede o que é responsável entregar numa única rodada).
- Nenhuma integração externa real de scraping/APIs de mercado —
  RADAR WEB COMERCIAL opera 100% em MOCK, evidência sempre `UNKNOWN`
  até uma fonte real ser conectada (nunca simula capacidade
  inexistente).
- Nenhum provider multimodal real (visão/áudio/vídeo) conectado —
  `MultimodalReasoner` sempre devolve confidence=0.0 sem integração
  real; `register_analyzer()` é o ponto de conexão pronto.
- Quality Gate dedicado do SENTINEL para evidência do RADAR (item 29)
  reaproveita a mesma infraestrutura (`atlas.sentinel.Sentinel` +
  `core.quality.gates`) mas não foi criado um gate ESPECÍFICO nomeado
  para "evidência do Radar" nesta rodada — fica como extensão futura
  natural sobre a infraestrutura já existente.
