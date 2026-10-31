# CHANGELOG — MICROCORREÇÃO DE FECHAMENTO ATLAS PIPELINE 1: DOMAIN POLICY + TEST/REAL REVALIDATION + DURABLE ACQUISITION ANCHOR

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_PIPELINE1_CANONICAL_TRUST_VALIDATION`
(1828 testes). Corrige as 3 falhas estruturais finais reportadas por
auditoria independente, preservando integralmente V1, ATLAS
EXPANSION 1 e todas as correções anteriores do Pipeline 1. Pipeline
2 continua não implementado.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1855  PASS: 1855  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~115-118s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, stderr limpo
compileall: OK
```

**1855 testes** (1828 preservados + **27 líquidos** — Pipeline 1 foi
de 120 para 147 testes).

## Modelagem ANTES de codificar

Domain policy, política de confiança contextual, e âncora durável —
todas convergindo para a MESMA `DiscoverySignal.trust_issues()`,
nunca três validadores separados.

## Falha 1 — lineage válida aceitava valores fora do domínio da métrica

Confirmado: `trend_strength=50.0`, `audience=-100`, `competition=-4.0`
com lineage válida e `trust_issues()` vazio → `READY_FOR_PIPELINE2`.

### Correção

`_SIGNAL_DOMAINS` centralizado em `atlas/discovery/models.py` —
`trend_strength`/`competition`/`risk`/`hook_strength`/`compliance`
em `[0,1]`; `audience`/`estimated_reach`/`production_cost` em
`[0,∞)`. `_domain_issue_for(key, value)` — chamada de UM único
lugar, dentro de `trust_issues(key=...)` — nunca copiada. Um valor
fora do domínio é REJEITADO (excluído do score, ou reprovado no
gate), nunca corrigido via clamp silencioso — o clamp matemático
continua existindo como defesa adicional, nunca como substituto da
validação de domínio.

## Falha 2 — lineage TEST sobrevivia a restart e virava utilizável em modo operacional

Confirmado: `allow_test_sources=True` → `READY`; persistido em
SQLite; nova instância com `allow_test_sources=False` e broker vazio
→ ainda `READY` (o `mode="TEST"` da lineage nunca era reavaliado).

### Correção

Novo `TrustPolicy` (`atlas/discovery/provenance.py`) — construído a
partir da configuração ATUAL do engine (`self.allow_test_sources`),
nunca fixado na criação da lineage. `trust_issues(policy=...)`
reavalia `lineage.mode == "TEST"` contra a política ATUAL a CADA
chamada — gate, ranking, qualquer revalidação pós-restart. O `mode`
gravado na lineage NUNCA muda (a origem é sempre honesta e
identificável); o que muda é se ele é ACEITO, decidido de novo a
cada avaliação.

## Falha 3 — VerifiedLineage não ancorada a uma prova durável autoritativa

Confirmado: trocar `lineage.acquisition_id` para uma string
inexistente, mantendo `value`/`verified_value_digest` intactos →
ainda `READY_FOR_PIPELINE2` (o digest só provava "o valor não mudou
desde a lineage", nunca "esta lineage corresponde a uma aquisição
real").

### Correção

Nova tabela SQLite `discovery_acquisitions` (`core/models/db.py`) —
`DurableAcquisitionRecord`, o registro AUTORITATIVO e SEPARADO do
blob do sinal (nunca token/segredo, só metadados de proveniência +
digest das observações). `OpportunityStore.upsert_acquisition_record()`/
`get_acquisition_record()` (ambos os modos, memória e SQLite).
`VerifiedLineage` ganha `acquisition_observations_digest` — um
retrato do registro durável no momento da verificação.
`trust_issues(durable_record=...)` busca o registro ATUAL (nunca
confia só no que está gravado dentro do próprio sinal) e cruza CADA
campo: `acquisition_id`, `source_id`, `execution_id`, `mode`,
`status`, `data_status`, `observations_digest` — qualquer divergência
é detectada.

## A validação canônica única

`DiscoverySignal.trust_issues(key=None, now=None, policy=None,
durable_record=None)` — agora contempla:

```
proveniência sintática + domínio da métrica + integridade da lineage
+ âncora durável (autoritativa, separada) + política de confiança ATUAL
+ freshness
= CANONICAL TRUST VALIDATION
```

Reutilizada EXATAMENTE nos mesmos pontos: sanitização
(`_verify_signal()`, que também é onde `DurableAcquisitionRecord` é
persistido, ANTES de qualquer sinal ser verificado), Quality Gate
(`evaluate()`, que agora recebe `policy`/`durable_records` via o
`subject`), ranking (`_score()`'s `_usable()`, para qualquer sinal,
essencial ou opcional) — persistência/restart é coberta
automaticamente, já que gate e ranking sempre buscam o registro
durável ATUAL, nunca dependem do broker efêmero.

## Testes adversariais exigidos — todos os cenários cobertos

`tests/test_atlas_pipeline1_discovery.py` — arquivo foi de 120 para
147 testes:
- `DomainPolicyTests` (10): `trend`/`competition`/`risk` abaixo de 0
  e acima de 1 bloqueiam; `audience`/`estimated_reach`/
  `production_cost` negativos bloqueiam; `NaN`/`Infinity` continuam
  bloqueados; limites exatos (`0.0`/`1.0`) continuam funcionando; um
  sinal inválido nunca é corrigido silenciosamente via clamp para
  depois ficar `READY`.
- `TrustPolicyAcrossRestartTests` (5, com SQLite real): `TEST` +
  `allow_test_sources=True` funciona; persistir `TEST` → restart
  operacional nunca `READY`; persistir `TEST` → restart em modo teste
  continua utilizável só como `TEST`; `REAL` permanece `REAL` após
  restart; mudar só a configuração de runtime nunca reescreve o
  `mode` historicamente persistido.
- `DurableAnchorFieldTamperingTests` (11, com SQLite real, "alterando
  individualmente cada campo persistido" como exigido): apagar o
  registro durável, trocar `acquisition_id`/`source_id`/
  `execution_id`/`mode`/`status`/`data_status`/`observations_digest`,
  alterar `signal.value` — todos detectados; restart íntegro continua
  `READY` sem reexecutar aquisição; nenhuma coluna de
  segredo/token/credencial no esquema.

Confirmado sem flakiness rodando a suíte completa 3× seguidas.

## Preservação confirmada explicitamente

Reexecutei os 30 arquivos de teste dedicados de TODAS as rodadas
anteriores — **456 testes, todos passando sem alteração**, incluindo
as 4 correções anteriores do Pipeline 1 e os 95 testes das dez
rodadas de ATLAS EXPANSION 1.

## Arquivos alterados
- `core/models/db.py` — nova tabela `discovery_acquisitions`
- `atlas/discovery/provenance.py` — `DurableAcquisitionRecord`,
  `TrustPolicy` novos; `AcquisitionOutcome.to_durable_record()`
- `atlas/discovery/models.py` — `_SIGNAL_DOMAINS`/`_domain_issue_for()`
  novos; `VerifiedLineage.acquisition_observations_digest` novo;
  `trust_issues()` reescrita com `key`/`policy`/`durable_record`
- `atlas/discovery/opportunity_store.py` — `upsert_acquisition_record()`/
  `get_acquisition_record()` novos (memória + SQLite)
- `atlas/discovery/engine.py` — `scan_source()` persiste o registro
  durável antes de verificar sinais; `_verify_signal()` cruza contra
  ele; `rank_all()` constrói `TrustPolicy` + busca registros duráveis
  por oportunidade (`_durable_records_for()`); `_score()`/`_usable()`
  usam tudo isso para qualquer sinal
- `atlas/discovery/quality_gate.py` — `evaluate()` recebe `policy`/
  `durable_records` do `subject`

## Arquivos alterados (testes)
- `tests/test_atlas_pipeline1_discovery.py` — `_verified_signal()`
  ganha `store=None` (auto-registra o registro durável quando
  fornecido); novo helper `_durable_records_for_signals()` (para
  chamadas diretas a `_score()`); dezenas de testes pré-existentes
  ajustados para o novo requisito de âncora durável (comportamento
  nunca alterado); 27 testes novos

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- `_SIGNAL_DOMAINS` continua uma escolha documentada mas calibrável —
  `compliance` em particular tem semântica definida explicitamente
  nesta correção (grau de conformidade, 0-1), nunca assumida
- Pipeline 2 continua não implementado
