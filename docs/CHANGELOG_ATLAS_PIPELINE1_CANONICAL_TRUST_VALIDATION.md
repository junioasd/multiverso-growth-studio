# CHANGELOG — MICROCORREÇÃO ARQUITETURAL DE FECHAMENTO ATLAS PIPELINE 1

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_PIPELINE1_ACQUISITION_BOUNDARY`
(1808 testes). Corrige as 3 falhas estruturais finais reportadas por
auditoria independente, preservando integralmente V1, ATLAS
EXPANSION 1 e todas as correções anteriores do Pipeline 1. Pipeline
2 continua não implementado.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1828  PASS: 1828  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~124-125s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, stderr limpo
compileall: OK
```

**1828 testes** (1808 preservados + **20 líquidos** — Pipeline 1 foi
de 100 para 120 testes).

## Design revisado ANTES de codificar

`docs/DESIGN_ATLAS_PIPELINE1_CANONICAL_TRUST_VALIDATION.md` — modela
`AcquisitionOutcome`, lineage durável, e a validação canônica única,
escrito antes de qualquer implementação.

## Falha 1 — aquisição existia, mas os sinais não estavam vinculados ao conteúdo adquirido

Confirmado: um `AcquisitionResult` só carregava um `acquisition_id` +
digest — nunca o payload. Um adapter podia chamar `context.acquire()`
genuinamente e ainda inventar `trend_strength=0.93` sem relação com o
que foi obtido; `fetch()` retornando `None` ainda gerava
`status=COMPLETED`.

### Correção

`AcquisitionOutcome` (substitui `AcquisitionResult`) expõe
`.observations` — as observações REAIS obtidas — e classifica
honestamente `data_status` (`"DATA"`/`"NO_DATA"`; `None`, tipo
inesperado ou dict vazio NUNCA viram `"DATA"`). Novo
`VerifiedLineage` (`atlas/discovery/models.py`) — só construído por
`ContentDiscoveryEngine._verify_signal()`, DEPOIS de confirmar contra
a aquisição AO VIVO: para `FACT`, o valor precisa ser IDÊNTICO à
observação referenciada (exatamente 1 campo); para `ESTIMATE`/
`INFERENCE`, os campos referenciados precisam genuinamente existir
(sem exigir igualdade — é uma estimativa/inferência, não o dado
bruto, conforme instruído explicitamente). `DiscoverySignal` ganha
`claimed_acquisition_id`/`claimed_observation_fields` (declaração do
adapter — intenção, nunca prova).

## Falha 2 — sinais opcionais inválidos corrompiam o score

Confirmado: `risk` com `evidence=REAL`, `claim_type=UNKNOWN`,
`source=None`, `confidence=1000` — `score=220+`, muito além de
`[0,1]` — porque só os 3 sinais ESSENCIAIS passavam pelo Quality
Gate; sinais OPCIONAIS usados no ranking nunca eram validados.

### Correção

`_usable()` (dentro de `_score()`) agora chama `signal.trust_issues()`
— a MESMA validação canônica do gate — para QUALQUER sinal, essencial
ou opcional.

## Falha 3 — proveniência não sobrevivia a restart

Confirmado com SQLite real: uma oportunidade `READY` continuava
`READY` após uma nova instância sem o broker/receipt antigo — o
`acquisition_id` virava um identificador órfão, mas a evidência já
persistida continuava sendo confiada cegamente.

### Correção

`VerifiedLineage` é persistida JUNTO com o sinal (mesmo blob JSON) —
NUNCA depende do broker efêmero para ser revalidada. Carrega
`verified_value_digest` — hash do valor NO MOMENTO da verificação —
detectando adulteração pós-persistência mesmo sem qualquer acesso ao
broker. Testado com SQLite real: lineage sobrevive a um restart
genuíno; corromper o valor persistido diretamente no banco é
detectado (digest não bate mais) e o status deixa de ser `READY`.

## A validação canônica única

`DiscoverySignal.trust_issues()` — SUPERSET de `provenance_issues()`
(proveniência sintática, já existente) MAIS a integridade do
`lineage` — reutilizada EXATAMENTE nos mesmos pontos, nunca
reimplementada: sanitização (`_verify_signal()`, com dupla checagem —
constrói a lineage E revalida via `trust_issues()` antes de aceitar),
Quality Gate, ranking (`_usable()`, para qualquer sinal). Persistência/
restart são cobertos implicitamente, já que `trust_issues()` nunca
depende de estado vivo do broker.

## Testes adversariais exigidos — todos os cenários cobertos

`tests/test_atlas_pipeline1_discovery.py` — arquivo foi de 100 para
120 testes:
- `AcquisitionOutcomeLineageTests` (7): transport retorna `None`
  nunca vira `DATA`/utilizável; payload inválido (não-dict) falha
  fechado; aquisição `NO_DATA` nunca produz oportunidade REAL;
  aquisição `{}` nunca permite adapter inventar FACT; `FACT`
  desconectado da observação (`0.93` declarado quando a observação
  diz `0.2`) bloqueado — cenário exato da auditoria; `FACT`
  corretamente vinculado ao valor adquirido permitido; `INFERENCE`
  com lineage válida existe como `INFERENCE`, nunca promovida a
  `FACT`.
- `OptionalSignalCannotCorruptScoreTests` (6): `risk`/
  `production_cost`/`hook_strength`/`estimated_reach` com
  `claim_type=UNKNOWN`, `source` ausente, `confidence` em
  `{0, -1, 1000, NaN, Infinity}`, timestamps inválidos, e sem
  `lineage` — todos incapazes de manipular o score; score sempre
  comprovadamente em `[0,1]`, inclusive sob combinações malformadas
  simultâneas.
- `DurableProvenanceSurvivesRestartTests` (6, com SQLite real):
  lineage sobrevive a restart; corromper o receipt durável remove o
  status `READY`; `acquisition_id` inexistente após restart vira
  `LOW_EVIDENCE`; receipt de outra fonte/execução continua bloqueado
  (limite arquitetural documentado honestamente — ver nota abaixo);
  restart normal preserva a oportunidade sem reaquisição; freshness
  continua aplicada independente da existência da prova.
- `FinalAdversarialHardeningTests` (1, achado próprio): aquisição
  GENUÍNA + valor CORRETAMENTE vinculado, mas `confidence=1000`
  (proveniência sintática inválida) — a dupla checagem em
  `_verify_signal()` ainda bloqueia.

Confirmado sem flakiness rodando a suíte completa 3× seguidas.

### Limite arquitetural documentado honestamente

`VerifiedLineage.verified_value_digest` protege o VALOR contra
adulteração pós-persistência — mas não há, nem poderia haver sem
reintroduzir dependência do broker efêmero, uma segunda verificação
viva de que `lineage.source_id`/`execution_id` originais eram
genuínos além do que já foi confirmado no momento da aquisição. Um
teste dedicado (`test_receipt_from_another_source_or_execution_still_
blocked_after_restart`) documenta esse limite explicitamente, em vez
de escondê-lo.

## Preservação confirmada explicitamente

Reexecutei os 30 arquivos de teste dedicados de TODAS as rodadas
anteriores — **429 testes, todos passando sem alteração**, incluindo
as 4 correções anteriores do Pipeline 1 e os 95 testes das dez
rodadas de ATLAS EXPANSION 1.

## Arquivos alterados
- `atlas/discovery/provenance.py` — `AcquisitionOutcome` (substitui
  `AcquisitionResult`), classificação `data_status`, `.observations`
- `atlas/discovery/models.py` — `VerifiedLineage` novo;
  `DiscoverySignal.claimed_acquisition_id`/`claimed_observation_fields`/
  `lineage` (substituem `acquisition_id`); `trust_issues()`/
  `has_valid_trust()` novos; `compute_value_digest()` público
- `atlas/discovery/opportunity_store.py` — serialização de `lineage`/
  `claimed_acquisition_id`/`claimed_observation_fields`
- `atlas/discovery/quality_gate.py` — usa `trust_issues()` em vez de
  `provenance_issues()`
- `atlas/discovery/engine.py` — `_verify_signal()` (substitui
  `_sanitize_signal()`) com verificação completa contra observações;
  `_usable()` em `_score()` usa `trust_issues()` para todo sinal

## Arquivos alterados (testes)
- `tests/test_atlas_pipeline1_discovery.py` — helper `_verified_signal()`
  novo (constrói sinais com lineage genuinamente íntegra para testes
  unitários de gate/ranking/freshness); `_RealSource`/`_AcquiringSource`
  atualizadas para usar `context.acquire()` corretamente, com
  observações reais; 33 testes pré-existentes ajustados para o novo
  modelo de sinal (comportamento nunca alterado); 20 testes novos

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- Limite arquitetural do digest de lineage declarado explicitamente
  acima (protege o valor, não uma segunda verificação viva de
  source/execution originais pós-restart)
- `DEFAULT_FRESHNESS_WINDOW_SECONDS`, `_REFERENCE_MAX_REACH`,
  `_REFERENCE_MAX_COST` continuam escolhas documentadas mas
  arbitrárias
- Pipeline 2 continua não implementado
