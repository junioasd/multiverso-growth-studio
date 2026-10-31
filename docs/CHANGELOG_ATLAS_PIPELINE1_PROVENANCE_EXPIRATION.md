# CHANGELOG — MICROCORREÇÃO OBRIGATÓRIA ATLAS PIPELINE 1: PROVENIÊNCIA E VIGÊNCIA

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_PIPELINE1_CRITICAL_FIX`
(1756 testes). Corrige as 2 falhas adicionais de profundidade
reportadas por auditoria independente, preservando integralmente V1,
ATLAS EXPANSION 1 e as duas correções críticas anteriores do
Pipeline 1. Pipeline 2 continua não implementado.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1775  PASS: 1775  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~120-122s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, stderr limpo
compileall: OK
```

**1775 testes** (1756 preservados + **19 testes novos**).

## Falha 1 — Evidence Gate confiava cegamente no `evidence` declarado

Confirmado com o cenário exato da auditoria: uma fonte devolvendo
`trend_strength=0.8, audience=1000, competition=0.2`, todos com
`EvidenceLevel.REAL`, mas `claim_type=UNKNOWN`, `source=None`,
`confidence=0` — chegava a `READY_FOR_PIPELINE2` com `score=0.8`.

### Correção — mecanismo explícito e reutilizável de proveniência

Novo `DiscoverySignal.provenance_issues()` /
`DiscoverySignal.has_valid_provenance()` em
`atlas/discovery/models.py` — o contrato que QUALQUER fonte (futura,
real, existente) precisa satisfazer para que um sinal seja aceito
como evidência suficiente. Verifica, TODOS obrigatórios quando
`evidence != UNKNOWN`:
- `source` não vazio (identifica QUEM afirma isso);
- `claim_type` diferente de `UNKNOWN`;
- `confidence` numérica, finita, no intervalo `(0.0, 1.0]` — nunca
  zero, negativa, `NaN` ou `Infinity`;
- `observed_at` um timestamp válido, positivo, finito, não no futuro
  (com tolerância de 5s para relógios dessincronizados);
- combinação `EvidenceLevel`/`ClaimType` semanticamente coerente —
  `FACT` só é coerente com `REAL`; `REAL` nunca é coerente com
  `HYPOTHESIS`.

O `ContentOpportunityEvidenceGate` agora exige
`provenance_issues()` vazio, ALÉM do valor já ser um número válido —
nome de classe da fonte NUNCA é usado como prova; tudo é verificado a
partir dos campos DECLARADOS no próprio sinal. Ausência de
proveniência nunca aprova por omissão — as razões específicas (fonte
ausente, `claim_type` incoerente, confiança inválida, etc.) são
reportadas explicitamente nas justificativas do gate.

## Falha 2 — vigência parcialmente expirada ainda passava

Confirmado: `trend_strength` expirado, `audience`/`competition`
ainda válidos — resultado incorreto `READY_FOR_PIPELINE2`.

### Correção

`_status_for()` (antes: `all()` — só bloqueava se TODOS os sinais
essenciais estivessem expirados) agora usa `any()` — QUALQUER sinal
essencial com evidência conhecida que esteja expirado bloqueia
`READY_FOR_PIPELINE2`, mesmo que os demais ainda estejam válidos
(resulta em `STALE`, ou `LOW_EVIDENCE` quando o score também fica
inutilizável — ambos nunca `READY`, conforme a semântica permitida
pela auditoria). `_score()` também reforçado: um sinal EXPIRADO nunca
entra no cálculo — `now` é computado uma única vez por passada de
ranking e usado consistentemente entre `_score()`/`_status_for()`.

## Testes adversariais exigidos — todos cobertos

`tests/test_atlas_pipeline1_discovery.py` — 19 testes novos em 3
classes:
- `ProvenanceRequiredForApprovalTests` (11 testes): `REAL` +
  `source=None`/vazio nunca passa; `REAL` + `claim_type=UNKNOWN`
  nunca passa; `REAL` + `confidence=0`/negativa/`>1` nunca passa;
  `observed_at` inválido (string, `None`, zero, negativo, futuro
  distante) nunca passa; combinações incoerentes (`ESTIMATED`+`FACT`,
  `REAL`+`HYPOTHESIS`) nunca passam; sinais mistos (1 válido, 2 sem
  proveniência) ainda bloqueiam; fonte customizada que autodeclara
  `REAL` sem proveniência nunca gera `READY_FOR_PIPELINE2`; controle
  positivo com proveniência completa ainda chega a `READY`.
- `ExpirationBlocksReadyTests` (5 testes): 1 dos 3 sinais essenciais
  expirado bloqueia `READY`; confirmado individualmente para CADA um
  dos 3; `trend_strength` expirado excluído do cálculo do score
  (testado diretamente); `STALE` alcançável quando o score ainda é
  calculável via outro sinal; controle positivo com tudo válido e
  vigente ainda chega a `READY`.
- `Pipeline1PreviousFixesStillPreservedTests` (3 testes): dummy
  credential ainda resulta em `ALL_SOURCES_UNAVAILABLE`; zero fontes
  ainda resulta em `NO_DATA`.

Testado adversarialmente ALÉM do exigido: limites exatos de
`confidence` (`1.0` aprova, `1.0001` rejeita), `source` só com
espaços em branco, `observed_at` como string.

Confirmado sem flakiness rodando a suíte completa 5× seguidas.

## Ajuste em 2 testes pré-existentes (comportamento correto, nunca alterado)

`test_sufficient_evidence_becomes_ready_for_pipeline2` e
`test_prioritized_queue_orders_ready_before_low_evidence`
construíam sinais com `evidence` definido mas sem `claim_type`/
`confidence` — passavam ANTES da exigência de proveniência existir.
Atualizados para fornecer proveniência completa e coerente,
continuando a validar exatamente o mesmo comportamento (evidência
suficiente → `READY_FOR_PIPELINE2`).

## Preservação confirmada explicitamente

Reexecutei os 30 arquivos de teste dedicados de TODAS as rodadas
anteriores — **376 testes, todos passando** (com os 2 ajustes acima,
comportamento nunca alterado), incluindo as 2 correções críticas
anteriores do Pipeline 1 e os 95 testes das dez rodadas de ATLAS
EXPANSION 1.

## Arquivos alterados
- `atlas/discovery/models.py` — `DiscoverySignal.provenance_issues()`/
  `has_valid_provenance()` novos; `_is_coherent_evidence_claim_pair()`
  nova; `_INCOHERENT_EVIDENCE_CLAIM_PAIRS` nova
- `atlas/discovery/quality_gate.py` — essencial-signal check agora
  também exige `provenance_issues()` vazio
- `atlas/discovery/engine.py` — `_status_for()`/`_score()` recebem
  `now` consistente; `any()` em vez de `all()` para expiração;
  `_score()` exclui sinais expirados

## Arquivos alterados (testes)
- `tests/test_atlas_pipeline1_discovery.py` — 19 testes novos, 2
  testes pré-existentes ajustados para fornecer proveniência completa

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- Limitação arquitetural inerente, declarada honestamente: o
  mecanismo de proveniência eleva substancialmente a barra para uma
  fonte mal-intencionada ou defeituosa (exige um registro de
  proveniência completo e coerente, não apenas um enum), mas não
  pode CRIPTOGRAFICAMENTE provar que um `value` numérico é genuíno —
  isso exigiria uma implementação real de API (fora de escopo). O
  mecanismo é a base reutilizável que futuros adapters reais
  populam corretamente.
- `valid_until=None` continua significando "sem prazo conhecido,
  nunca expira" (comportamento pré-existente, não alterado nesta
  rodada — a auditoria pediu que vigência JÁ DEFINIDA fosse
  respeitada, não que toda oportunidade exija uma vigência)
- Pipeline 2 continua não implementado
