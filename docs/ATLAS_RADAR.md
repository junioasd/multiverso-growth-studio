# ATLAS+RADAR — Radar Permanente de Oportunidades Comerciais e Desempenho Multidimensional

Correção incremental sobre o ATLAS V1, preservando integralmente todo o
hardening aprovado até a V6.

## 1. Radar Permanente de Oportunidades Comerciais

Capacidade NATIVA do `AtlasDirector` (`atlas.radar`), não um módulo
separado desacoplado — mas com arquitetura própria, sem acoplamento
desnecessário ao resto do sistema.

```
atlas/radar/
  models.py        ChannelType (26 valores), OfferType (8 valores),
                     EvidenceLevel (REAL/ESTIMATED/INFERRED/UNKNOWN),
                     OpportunitySignal, CommercialOpportunity, RankedOpportunity
  sources.py         OpportunitySource (interface) + GenericMockOpportunitySource
                       + AffiliateMarketplaceOpportunitySource (reaproveita
                       core.adapters.affiliate.adapters, já existente)
                       + build_default_sources() — 30 fontes registradas
  quality_gate.py      OpportunityEvidenceGate (subclasse de
                         core.quality.gates.QualityGate — mesmo framework
                         usado por VideoQualityGate/CommercialQualityGate)
  engine.py              CommercialOpportunityRadar — scan()/rank()/
                           tracked_opportunities()
```

### Cobertura de canais
YouTube, Instagram, Facebook, TikTok, outras redes sociais, marketplace,
e-commerce, loja própria, site, blog, portal, landing page, página de
venda, comparador, busca orgânica, comunidade, fórum, plataforma de
creators, plataformas de afiliados (Hotmart/Monetizze/Eduzz/Kiwify/
Braip via os adapters já existentes), plataforma de serviços, B2B,
e-mail, newsletter, canal de conteúdo, parceiro comercial, distribuidor
— mais `ChannelType.OTHER` como escape-hatch para canais futuros.

### Classificação de evidência — regra única e estrutural
A evidência de CADA sinal é decidida pelo **modo do adapter** (`AdapterMode.REAL`
vs `MOCK`), nunca por confiar no conteúdo retornado:

```python
def _evidence_for_mode(mode: AdapterMode) -> EvidenceLevel:
    return EvidenceLevel.REAL if mode == AdapterMode.REAL else EvidenceLevel.UNKNOWN
```

Mesmo o catálogo mock de afiliados (que internamente tem preços/comissões
de exemplo) é reclassificado como `UNKNOWN` pelo radar — nenhum número
plausível é apresentado como evidência real. **Nenhuma integração
externa real foi implementada neste lote** (instrução explícita) —
100% das oportunidades descobertas hoje são `UNKNOWN` até uma
integração real ser configurada.

### Ranking
`rank()` só atribui um score numérico quando o Quality Gate aprova
(`Verdict.APPROVED`) E há sinais com evidência não-UNKNOWN — caso
contrário, `score="UNKNOWN"`. Nunca publica, cadastra conta, gasta,
contrata ou conecta — isso é exclusivo de
`AtlasDirector.request_critical_action()`.

## 2. Desempenho comercial multidimensional

`atlas.performance.MultiDimensionalPerformanceAnalyst` (nova classe,
`atlas.performance.PerformanceAnalyst` original preservado intacto para
compatibilidade total com o ATLAS V1).

Dimensões: canal, produto, campanha, funil, plataforma, fonte de
tráfego — via novas colunas aditivas em `sales_events`
(`product_id`, `campaign_id`, `funnel_id`, `platform`, `traffic_source`,
migração retrocompatível em `core/models/db.py`).

- `AtlasDirector.analyze_performance_multidimensional(...)` — análise
  filtrada por qualquer combinação de dimensões.
- `AtlasDirector.breakdown_performance(group_by=..., channel_id=...)` —
  cruzamento (ex: produto x canal, campanha x canal).
- Isolamento SEMPRE validado primeiro via
  `GovernedFacade.get_dashboard_view()` — os filtros adicionais nunca
  ampliam o conjunto de canais já autorizado ao principal.

## 3. Persistência de aprovações do Owner

`atlas.escalation.EscalationDesk` reescrito com persistência real
(tabela `owner_approval_requests`, aditiva). Estados:
`PENDING_OWNER_APPROVAL` / `APPROVED` / `REJECTED` / `EXPIRED` /
`CANCELLED`. Reaproveita o MESMO padrão de transação atômica
(`BEGIN IMMEDIATE`/commit-dentro-do-try/rollback+close) já corrigido nas
rodadas anteriores de hardening — nenhuma máquina de transação nova.

**Compatibilidade retroativa total**: `EscalationItem.resolved`/
`.resolution`/`.resolved_by` continuam funcionando (agora como
properties derivadas do novo `status`); `resolve()` continua existindo.

Idempotência: índice único parcial em `idempotency_key`; decisão usa
UPDATE condicionado a `status='PENDING_OWNER_APPROVAL'` — retry com a
MESMA decisão é idempotente, com decisão DIFERENTE levanta
`AlreadyDecided`.

## Limitações desta entrega

- Nenhuma integração externa real (instrução explícita).
- 30 fontes de descoberta cobrem as categorias pedidas; novas
  plataformas específicas exigem um `OpportunitySource` dedicado (a
  arquitetura já suporta isso sem alterar `atlas.radar.engine`).
- `expire_stale()` precisa ser chamado explicitamente (não há um job
  agendado automático nesta entrega).

---

## Correção final (2 pontos residuais)

### 1. Execução recorrente do Radar
`atlas/radar/scheduler.py` — `RadarScheduler` acopla o Radar à
infraestrutura de jobs JÁ existente (`core.jobs.queue.JobQueue`), sem
agendador novo e sem serviço pago:

- **Ativar/desativar**: `AtlasDirector.enable_radar_schedule(interval_seconds=...)`
  / `disable_radar_schedule()`.
- **Status**: `AtlasDirector.radar_schedule_status()` — retorna
  `enabled`, `interval_seconds`, `last_run_at`, `last_run_status`,
  `next_run_at`.
- **Execução**: `AtlasDirector.run_scheduled_radar_scan(niche=...)` —
  roda o scan SE o ciclo estiver devido (idempotente por ciclo, nunca
  duplica).
- **Prevenção de concorrência**: cada ciclo devido tem uma
  `idempotency_key` determinística (`radar_scan:<bucket>`); a reserva
  de execução usa o NOVO método `JobQueue.try_start()` (aditivo,
  `core/jobs/queue.py`), que — diferente de `start()` — só retorna um
  Job quando a PRÓPRIA chamada conseguiu reservar o slot, nunca quando
  apenas OBSERVA que outra chamada já está rodando. Um bug real foi
  encontrado e corrigido durante a validação: o código inicial usava
  `start()` (ambíguo) e permitia que uma segunda chamada, ao observar
  o job já `RUNNING` por outra instância, tentasse "completá-lo" de
  novo — corrigido antes da entrega, validado em 20 execuções
  concorrentes consecutivas sem exceção.
- **Restart**: estado do agendamento (`radar_schedule`, tabela
  aditiva) e os `Job`s do radar usam o MESMO `db_path` do resto do
  sistema.
- **Nunca ação crítica**: `RadarScheduler` não tem nenhuma referência a
  `EscalationDesk` — estruturalmente incapaz de escalonar ou executar
  algo crítico.

### 2. Fingerprint das aprovações críticas do Owner
`atlas/escalation.py` — o fingerprint de idempotência agora cobre a
identidade IMUTÁVEL completa (`operation`, `origin`, `reason`, `risk`,
`context`, `evidence`, `expires_at`), reutilizando
`stable_fingerprint()` (já canonicaliza chaves de dict recursivamente
via `json.dumps(sort_keys=True)`). Antes, só `(operation, reason, risk,
actor)` entravam no cálculo — `context`/`evidence` diferentes com a
mesma chave não geravam conflito (ex: `spend_money amount=100` depois
`amount=9999` passava despercebido).

### Testes de regressão adicionados (correção final)
`tests/test_atlas_radar_scheduler.py` (20: ativar/desativar, intervalo,
status, concorrência real com threads, restart, idempotência por
ciclo, nunca ação crítica, evidência MOCK preservada),
`tests/test_atlas_approval_fingerprint.py` (16: todos os cenários
adversariais pedidos — amount, context, evidence, risk, reason,
expires_at, canonicalização de ordem de chaves, comportamento após
restart, concorrência real) — 36 novos testes.

---

## Microcorreção final — Worker/loop autônomo do Radar no runtime

Até a V2, `RadarScheduler` já fazia tudo certo (persistência,
intervalo, `last_run`/`next_run`, concorrência, idempotência), mas só
executava quando alguém chamava `run_scheduled_radar_scan()`
manualmente. Não havia processo de fundo.

### `atlas/radar/worker.py` — `RadarWorker`
Thread stdlib (sem dependência de serviço pago) que chama
`RadarScheduler.run_if_due()` periodicamente — a MESMA operação já
existente, já atômica/idempotente. Usa `threading.Event.wait(timeout=...)`
para dormir entre verificações (nunca `time.sleep()` em loop apertado —
zero busy loop, zero consumo de CPU à toa). `start()`/`stop()` são
idempotentes; `stop()` faz `join()` com timeout, nunca deixa thread
órfã.

**Resiliência a erro**: qualquer exceção de `run_if_due()` é capturada
— o loop nunca morre. Falhas consecutivas dobram o tempo de espera
(`poll_interval_seconds * 2^falhas`, limitado por `max_backoff_seconds`)
até o próximo sucesso zerar o contador.

### Integração com o runtime
- `AtlasDirector.radar_worker` — construído (nunca iniciado
  automaticamente) em `__post_init__`; `start_radar_worker()`/
  `stop_radar_worker()` iniciam/param explicitamente, com auditoria.
- `api/bootstrap.build_app(..., start_radar_worker=True)` — novo
  parâmetro opcional (padrão `False`, para não afetar nenhum teste
  existente que constrói o app sem querer threads de fundo).
- `main.py cmd_serve` — passa `start_radar_worker=True`: o worker
  começa a rodar automaticamente junto com o servidor real, e
  `GrowthStudioApp.shutdown()` é chamado no encerramento (inclusive
  via `Ctrl+C`), garantindo `stop()` limpo.

### Nunca ação crítica
`RadarWorker` e `RadarScheduler` não têm NENHUMA referência a
`EscalationDesk` — estruturalmente incapazes de escalonar ou executar
publicação/gasto/contratação/conexão de conta. Todas as ações críticas
continuam exclusivamente em `AtlasDirector.request_critical_action()`.

### Testes de regressão adicionados
`tests/test_atlas_radar_worker.py` (16): execução automática sem
chamada manual, `last_run`/`next_run` mudando sozinhos, execução
repetida em múltiplos ciclos, disable/enable, restart real preservando
configuração sem duplicar execução, concorrência real com 4 workers no
mesmo banco (nunca mais de 1 tentativa por ciclo), erro temporário não
mata o worker (com backoff mensurável), shutdown limpo sem thread órfã
(inclusive via `GrowthStudioApp.shutdown()`), nunca cria escalonamento
automaticamente. Confirmado estável em 3 execuções consecutivas.
