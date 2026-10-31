# CHANGELOG — ATLAS PIPELINE 1: INTELIGÊNCIA E DESCOBERTA

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1_INPROGRESS_HEARTBEAT`
(1708 testes). Primeira entrega do PIPELINE 1, preservando
integralmente V1 e a ATLAS EXPANSION 1 aprovadas.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1741  PASS: 1741  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~115-119s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, stderr limpo de exceções de thread
compileall: OK
```

**1741 testes** (1708 preservados + **33 testes novos**).

## Escopo entregue

Descoberta e análise de sinais de mercado (tendências, temas,
formatos, ganchos, concorrência, público, ofertas) em YouTube,
Shorts, TikTok, Instagram — produzindo uma fila PERSISTENTE e
priorizada de oportunidades de conteúdo, pronta para um futuro
Pipeline 2 (produção — explicitamente NÃO implementado aqui).

## Design revisado ANTES de codificar

`docs/DESIGN_ATLAS_PIPELINE1_DISCOVERY.md` — estados, falhas, rate
limits, duplicidade, restart, concorrência, idempotência e
recuperação, todos cobertos explicitamente antes da implementação.

## O que foi reaproveitado (nunca duplicado)

- `core.evidence.claims.ClaimType` (FACT/ESTIMATE/INFERENCE/
  HYPOTHESIS/UNKNOWN) — já existente, já usado por
  `core.multimodal`/`radar_web`.
- `atlas.radar.models.EvidenceLevel` (REAL/ESTIMATED/INFERRED/
  UNKNOWN) — mesma disciplina "MOCK nunca vira evidência real"
  comprovada em produção.
- `core.quality.gates.QualityGate`/`GateResult`/`Verdict` — mesma
  base de `OpportunityEvidenceGate`/`VideoQualityGate`.
- `core.adapters.base.AdapterMode` + `core.config.SETTINGS.
  provider_key()` — mesmo padrão REAL-só-com-credencial de todo
  adapter existente.
- `atlas.orchestration.graph_executor.GraphExecutor` (ATLAS
  EXPANSION 1) — a descoberta roda COMO tarefas deste motor: exclusão
  por lease, retry, TTL, heartbeat, recuperação de crash real,
  Quality Gate — tudo herdado, nada reimplementado.
- `core.security.audit.AuditTrail`, `atlas.escalation.EscalationDesk`
  — mesmas instâncias reais já usadas pelo resto do `AtlasDirector`.

## Módulos novos (`atlas/discovery/`)

- `models.py` — `Platform`, `ContentFormat`, `DiscoverySignal` (valor
  + evidência + tipo de claim + fonte + data + confiança + vigência,
  todos obrigatórios), `ContentOpportunity` (com `identity_key`
  determinística — nunca o `opportunity_id` aleatório), `RankedContentOpportunity`.
- `sources.py` — uma fonte por plataforma pedida; `SourceUnavailable`
  para indisponibilidade explícita; nenhuma fabrica evidência sem
  credencial real configurada.
- `quality_gate.py` — `ContentOpportunityEvidenceGate`: nunca aprova
  por omissão de evidência essencial (`trend_strength`/`audience`/
  `competition`); bloqueia por política incompatível/risco alto;
  exige revisão por dado contraditório.
- `engine.py` — `ContentDiscoveryEngine`: `scan_source()`/`scan_all()`
  (uma fonte indisponível nunca derruba as demais) e `rank_all()`
  (aplica o gate, persiste o ranking de volta na fila).
- `opportunity_store.py` — fila PERSISTENTE (SQLite) com `upsert()`
  atômico por `identity_key` — a peça que faltava em `atlas.radar`
  (cujo rastreamento é só em memória).
- `pipeline.py` — `DiscoveryPipeline`: constrói um `AdvancedPlan` com
  uma tarefa paralela por fonte + uma tarefa de ranking dependente,
  delega ao `GraphExecutor` — nenhum motor de execução paralelo novo.

Conectado ao `AtlasDirector` como `self.discovery_pipeline`,
compartilhando as MESMAS instâncias reais de `MissionTracker`/
`JobQueue`/`EscalationDesk`/`AuditTrail` (confirmado por teste).

## Achado adversarial real — corrigido durante o desenvolvimento

Ao testar deliberadamente para quebrar a solução, encontrei: duas
fontes DIFERENTES atualizando a MESMA oportunidade (mesma
`identity_key`) com sinais DIFERENTES — o `upsert()` da segunda fonte
**apagava** o sinal já persistido pela primeira (substituição
completa do `signals_json`, não uma mesclagem). Corrigido com
leitura-modificação-escrita sob controle de versão OTIMISTA (mesmo
padrão já validado em `graph_executor.py`'s reclaim de lease) — laço
limitado, nunca recursão irrestrita. Confirmado sob concorrência REAL
de 5 threads × 10 iterações: todos os sinais distintos preservados,
nenhuma duplicação.

## Testes adversariais exigidos — todos cobertos

`tests/test_atlas_pipeline1_discovery.py` (33 testes), em 3 níveis
(motor puro, fila persistente, pipeline completo integrado ao
GraphExecutor):
- **Fonte indisponível**: `SourceUnavailable` explícita e exceção
  genérica — nenhuma derruba o scan; outras fontes continuam.
- **Dados conflitantes**: sinalizados ao gate, nunca resolvidos
  silenciosamente.
- **Duplicados**: `upsert()` nunca cria uma segunda linha para a
  mesma `identity_key`, mesmo sob variação de formatação.
- **Timeout**: mecanismo já existente do `GraphExecutor`
  (`timeout_per_task` + heartbeat + TTL) reaproveitado sem alteração;
  a fila nunca fica corrompida.
- **Restart**: fila sobrevive a "reinício" (nova instância, mesmo
  `db_path`); `GraphExecutor` nunca reexecuta uma tarefa já
  `VERIFIED` com o mesmo `mission_id`.
- **Duas instâncias**: 3 threads concorrentes na MESMA `mission_id` —
  a fonte é chamada exatamente 1 vez, a fila termina com exatamente 1
  item.
- **Evidência insuficiente**: nunca vira `READY_FOR_PIPELINE2` — fica
  honestamente `LOW_EVIDENCE`, `score="UNKNOWN"`.
- **Atualização de oportunidade já existente**: o valor mais recente
  prevalece POR SINAL, `opportunity_id`/`discovered_at` originais
  preservados.
- Mais: veredito `BLOCKED` do gate, gate estrutural da tarefa de
  ranking, Owner Approval preservado (`WAITING_OWNER` confirmado),
  auditoria conectada (eventos `discovery_pipeline_started`/
  `finished` confirmados), zero oportunidades nunca tratado como
  sucesso.
- Confirmado sem flakiness rodando a suíte completa 5× seguidas.

## Correção de limpeza de teste

Um teste de timeout deixava uma thread de fundo (ainda dormindo os
2s do delay simulado) viva após `tearDown()` já ter apagado o banco
— gerando uma exceção secundária inofensiva mas suja em stderr.
Corrigido aguardando `wait_for_inflight_completion()` antes da
limpeza — mesma disciplina já estabelecida em toda a suíte de testes
do `GraphExecutor`.

## Preservação confirmada explicitamente

Reexecutei os 30 arquivos de teste dedicados de TODAS as rodadas
anteriores — **342 testes, todos passando sem alteração**, incluindo
os 95 testes das dez rodadas anteriores de ATLAS EXPANSION 1.

## Arquivos novos
- `docs/DESIGN_ATLAS_PIPELINE1_DISCOVERY.md`
- `atlas/discovery/__init__.py`
- `atlas/discovery/models.py`
- `atlas/discovery/sources.py`
- `atlas/discovery/quality_gate.py`
- `atlas/discovery/engine.py`
- `atlas/discovery/opportunity_store.py`
- `atlas/discovery/pipeline.py`
- `tests/test_atlas_pipeline1_discovery.py` (33 testes)

## Arquivos alterados
- `core/models/db.py` — nova tabela `discovery_opportunities`
  (schema aditivo)
- `atlas/director.py` — `self.discovery_pipeline` conectado às
  mesmas instâncias reais já existentes

## Explicitamente NÃO implementado nesta entrega
- Produção de conteúdo (Pipeline 2)
- Publicação
- Integrações reais de API (nenhuma credencial de descoberta está
  configurada no ambiente; a interface já está pronta para receber
  uma quando disponível, seguindo a MESMA disciplina de evidência já
  estabelecida)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- Nenhuma fonte real (API oficial) está conectada nesta entrega —
  todas operam em modo MOCK, devolvendo lista vazia (nunca dado
  fabricado); conectar uma integração real é um trabalho futuro que
  não exige mudança nenhuma em `atlas.discovery.engine`/`pipeline`
  (só um novo `DiscoverySource` registrado)
