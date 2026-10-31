# DESIGN — ATLAS PIPELINE 1: INTELIGÊNCIA E DESCOBERTA

Escrito ANTES da implementação, conforme exigido. Cobre exatamente
os pontos pedidos: estados, falhas, rate limits, duplicidade,
restart, concorrência, idempotência e recuperação.

## Escopo

Descoberta e análise de sinais reais de mercado (tendências, temas,
formatos, ganchos, concorrência, público, ofertas) em YouTube,
Shorts, TikTok, Instagram e demais canais — produzindo uma fila
priorizada de oportunidades de CONTEÚDO (não comerciais/afiliados —
isso já existe em `atlas.radar`), pronta para alimentar um futuro
Pipeline 2 (produção de conteúdo — FORA de escopo aqui).

## O que é reaproveitado (nunca duplicado)

- `core.evidence.claims.ClaimType` (FACT/ESTIMATE/INFERENCE/
  HYPOTHESIS/UNKNOWN) — a distinção obrigatória exigida pelo pedido,
  já existente e já reutilizada por `core.multimodal`/`radar_web`.
- `atlas.radar.models.EvidenceLevel` (REAL/ESTIMATED/INFERRED/
  UNKNOWN) — classificação por SINAL individual, já com a disciplina
  "MOCK nunca vira REAL/ESTIMATED/INFERRED" comprovada em produção.
- `core.quality.gates.QualityGate`/`GateResult`/`Verdict` — mesma
  base de `OpportunityEvidenceGate`/`VideoQualityGate`.
- `core.adapters.base.AdapterMode`/`BaseAdapter` +
  `core.config.SETTINGS.provider_key()` — mesmo padrão REAL-só-com-
  credencial-configurada usado por TODOS os adapters existentes.
- `atlas.orchestration.graph_executor.GraphExecutor` — motor de
  execução (exclusão por lease, retry, TTL, heartbeat, recuperação de
  crash, Quality Gate) — a descoberta em si roda COMO tarefas deste
  motor, nunca um scheduler paralelo novo.
- `core.security.audit.AuditTrail` (`self.facade.audit`) — mesmo
  barramento de auditoria de todo o resto do sistema.
- `atlas.escalation.EscalationDesk` — mesmo mecanismo de Owner
  Approval, para o caso (raro, mas suportado) de uma fonte de
  descoberta exigir aprovação explícita (ex: uma API paga nova).

## O que é NOVO nesta entrega

- `atlas/discovery/models.py` — `Platform`, `DiscoverySignal`
  (evidence + claim_type + source + observed_at + confidence +
  valid_until), `ContentOpportunity` (tema, formato, gancho,
  plataforma, sinais), `RankedContentOpportunity`.
- `atlas/discovery/sources.py` — `DiscoverySource` (interface),
  fontes MOCK por plataforma (nenhuma chamada de API real nesta
  entrega — nenhuma credencial de descoberta está configurada no
  ambiente; a disciplina de evidência já garante que isso nunca vira
  REAL/ESTIMATED/INFERRED por engano).
- `atlas/discovery/quality_gate.py` — `ContentOpportunityEvidenceGate`.
- `atlas/discovery/engine.py` — `ContentDiscoveryEngine` (scan + rank).
- `atlas/discovery/opportunity_store.py` — fila PERSISTENTE (SQLite)
  de oportunidades — a peça que falta em `atlas.radar` (cujo
  `_tracked` é só em memória, não sobrevive a restart).
- `atlas/discovery/pipeline.py` — `DiscoveryPipeline`, o orquestrador
  que constrói um `AdvancedPlan` e delega a execução ao
  `GraphExecutor`.

## Estados

### Ciclo de vida de uma oportunidade (na fila persistente)

```
DISCOVERED -> RANKED -> (READY_FOR_PIPELINE2 | LOW_EVIDENCE | STALE)
```

- `DISCOVERED`: acabou de ser inserida/atualizada por uma fonte, sem
  ranking ainda.
- `RANKED`: passou pela avaliação do `ContentOpportunityEvidenceGate`
  e recebeu score/confiança.
- `READY_FOR_PIPELINE2`: gate `APPROVED`, score numérico disponível.
- `LOW_EVIDENCE`: gate `REVIEW_REQUIRED`/`BLOCKED` — nunca removida
  da fila, apenas marcada — decisão de usar ou não fica com quem
  consome a fila (honesto: ausência de evidência não é descartada
  silenciosamente, é reportada).
- `STALE`: `valid_until` de todos os sinais essenciais já expirou —
  continua na fila, mas sinalizada para re-descoberta.

### Ciclo de vida de uma execução (via GraphExecutor — já existente)

Reaproveitado sem alteração: `ACQUIRED → (OK|ERROR|TIMED_OUT_UNKNOWN_
OUTCOME) → COMPLETED/VERIFIED | FAILED | RETRYING | UNKNOWN_OUTCOME`.
A tarefa de descoberta em si é o `task_runner`; seu "outcome" é um
resumo (contagem descoberta/atualizada, erros de fonte) — nunca as
oportunidades inteiras (que vivem na fila persistente, não no lease).

## Falhas — cada uma com resposta explícita

| Falha | Resposta |
|---|---|
| Fonte indisponível (exceção/timeout na chamada) | Sinal `UNKNOWN`, nota explícita, a tarefa NÃO falha por isso — outras fontes continuam; resumo reporta a fonte como indisponível |
| Todas as fontes indisponíveis | Sinal geral `evidence_quality=UNKNOWN`, gate marca `LOW_EVIDENCE`/`REVIEW_REQUIRED` — NUNCA `READY_FOR_PIPELINE2` |
| Dado conflitante entre fontes (ex: duas fontes discordam do mesmo sinal) | Ambos os valores preservados com sua fonte própria (nunca um sobrescreve o outro silenciosamente); `has_contradictory_signals` sinaliza ao gate |
| Rate limit da fonte | Fonte devolve indisponibilidade (mesma via de "fonte indisponível") — nunca inventa dado para "compensar"; a re-tentativa é governada pelo `max_retries` da `PlanTask`/`JobQueue`, nunca um loop próprio |
| Timeout da tarefa inteira | Mecanismo já existente do `GraphExecutor` (`timeout_per_task` + heartbeat + TTL) — reaproveitado sem alteração |

## Rate limits

Nenhuma fonte real está configurada nesta entrega (nenhuma
credencial de API de descoberta no ambiente) — rate limit real não é
testável aqui. A INTERFACE já prevê: cada `DiscoverySource.discover()`
pode levantar (ou devolver um resultado marcando) indisponibilidade
sem derrubar o restante do scan — o mesmo padrão de
`GenericMockOpportunitySource`. Quando uma fonte real for conectada
(fora de escopo desta entrega), ela é responsável por respeitar os
limites da própria API — o pipeline nunca insiste além do
`max_retries` configurado por tarefa.

## Duplicidade — identidade estável, nunca um ID aleatório

`ContentOpportunity.opportunity_id` (aleatório, só para rastreio
interno da execução) NUNCA é a chave de deduplicação. A chave real é
`identity_key` — uma função DETERMINÍSTICA de
`(platform, theme, format, hook normalizado)`. `OpportunityStore.
upsert()` é condicionado a essa chave: a MESMA oportunidade
descoberta de novo (mesmo tema/formato/gancho/plataforma) atualiza a
linha existente (mescla sinais, atualiza `updated_at`), nunca cria
uma segunda linha.

## Restart

A fila (`OpportunityStore`) é SQLite, sobrevive a restart por
construção (mesmo padrão de toda a persistência já estabelecida no
projeto). A execução do PIPELINE em si roda sob o `GraphExecutor` —
herda toda a recuperação de crash já construída e testada em rodadas
anteriores (lease + TTL + heartbeat + fencing generation) sem
NENHUMA lógica nova de restart necessária aqui.

## Concorrência / Idempotência

Duas instâncias tentando escanear a MESMA fonte para a MESMA
`mission_id` — o `GraphExecutor` já garante exclusão real (lease
atômico); a SEGUNDA instância nunca reexecuta a fonte. Duas
instâncias fazendo `upsert()` na MESMA oportunidade (`identity_key`)
CONCORRENTEMENTE (cenário distinto — não é sobre a mesma execução do
pipeline, é sobre o armazenamento em si, que pode ser tocado por
execuções INDEPENDENTES/futuras) — `upsert()` usa
`INSERT ... ON CONFLICT (identity_key) DO UPDATE` (atômico no
próprio SQLite, nunca um read-then-write vulnerável a corrida).

## Recuperação

Uma tarefa de descoberta que falha definitivamente (fonte
genuinamente incompatível, erro de programação) marca a fonte como
indisponível NA fila (nota explícita), nunca interrompe o restante do
plano — as demais fontes (grupo paralelo) continuam. Recuperação de
crash real do PROCESSO (não apenas da fonte) já é responsabilidade do
`GraphExecutor` (herdada, não reimplementada).

## Preservado sem alteração

Quality Gates (framework `core.quality.gates`), Owner Approval
(`EscalationDesk`, disponível via o parâmetro já existente
`requires_owner_approval` de `execute_plan()`), auditoria
(`AuditTrail`), fail-closed (nenhum caminho novo aprova por omissão —
ausência de evidência gera `REVIEW_REQUIRED`/`LOW_EVIDENCE`, nunca
`READY_FOR_PIPELINE2`).
