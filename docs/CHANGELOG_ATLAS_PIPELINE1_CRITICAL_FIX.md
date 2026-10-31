# CHANGELOG — MICROCORREÇÃO OBRIGATÓRIA ATLAS PIPELINE 1

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_PIPELINE1_DISCOVERY`
(1741 testes). Corrige as 2 falhas críticas reais reportadas por
auditoria independente, preservando integralmente V1 e a ATLAS
EXPANSION 1 aprovadas.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1756  PASS: 1756  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~114-118s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, stderr limpo
compileall: OK
```

**1756 testes** (1741 preservados + **15 testes novos**).

## Falha crítica 1 — credencial fabricava evidência REAL/FACT

Confirmado com o cenário EXATO da auditoria (`YOUTUBE_API_KEY=dummy-key`,
nenhuma chamada real): `GenericMockDiscoverySource` classificava o
MODO do adapter (`REAL` sempre que uma credencial estava presente no
ambiente) como se fosse EVIDÊNCIA do dado retornado — produzindo uma
oportunidade fabricada com `value="UNKNOWN"` mas
`evidence=REAL`/`claim_type=FACT`, que chegava a
`READY_FOR_PIPELINE2`.

### Correção — 3 camadas, defesa em profundidade

**1. Na fonte** (`atlas/discovery/sources.py`): `GenericMockDiscoverySource`
não tem NENHUMA integração de API real implementada nesta base de
código. Reescrita para NUNCA depender do modo do adapter para
classificar evidência — sem credencial, devolve lista vazia (MOCK
genuíno); COM credencial, mas sem chamada real por trás, levanta
`SourceUnavailable` explícita ("NOT_IMPLEMENTED") — nunca fabrica uma
oportunidade, em NENHUMA configuração.

**2. No Quality Gate** (`atlas/discovery/quality_gate.py`): nova
`_is_valid_evidence_value()` — um sinal só é evidência suficiente se
`evidence != UNKNOWN` **E** o `value` for um número genuíno e finito
— nunca a string `"UNKNOWN"`, `None`, vazio, `NaN` ou `Infinity`
(achados de teste adversarial próprio — `isinstance(x, float)` é
`True` para ambos em Python). Esta checagem é INDEPENDENTE do campo
`evidence` declarado — protege contra qualquer fonte, existente ou
futura, correta ou defeituosa.

**3. No ranking** (`atlas/discovery/engine.py`): `_status_for()`
agora recebe `score` como parâmetro e exige que seja um número
finito (nunca `bool`, `NaN`, `Infinity` ou a string `"UNKNOWN"`) —
`READY_FOR_PIPELINE2` exige gate `APPROVED` **e** score numérico
válido, nunca um dos dois isoladamente. `_score()` também reforçado
para nunca incluir `NaN`/`Infinity` no cálculo da média.

### Bug latente adicional encontrado ao endurecer `_status_for`

`all()` sobre um gerador VAZIO devolve `True` vacuamente em Python —
a checagem de `STALE` original (`all(sinal.is_expired() for sinal in
essenciais_com_evidencia)`) tratava incorretamente "nenhum sinal com
evidência conhecida" como "tudo expirou". Corrigido: `STALE` agora
exige explicitamente que existam sinais com evidência conhecida E que
todos tenham expirado — nunca a ausência de evidência confundida com
expiração.

## Falha crítica 2 — ausência de dados registrada como "success"

Confirmado: com zero fontes, a auditoria registrava
`discovery_pipeline_finished -> success`, mascarando a ausência total
de dado.

### Correção

Novo campo `DiscoveryPipelineReport.data_status` — DISTINTO de
`exec_technical_success` (a execução técnica do `GraphExecutor`
terminou sem falha, conceito JÁ existente e agora exposto
separadamente). `data_status` é sempre um de:
- `NO_DATA` — zero fontes registradas, ou fontes rodaram mas nada foi
  descoberto (ex: apenas MOCK sem credencial)
- `ALL_SOURCES_UNAVAILABLE` — toda fonte registrada reportou
  indisponibilidade
- `DEGRADED` — algum dado foi descoberto, mas nenhum chegou a
  `READY_FOR_PIPELINE2`
- `PARTIAL` — dado utilizável encontrado, mas alguma fonte falhou —
  o resultado real é preservado e usável, rotulado honestamente
- `DATA_FOUND` — dado genuíno e utilizável, sem nenhuma fonte
  indisponível

A auditoria (`AuditTrail.record`) agora usa `report.data_status`
como `outcome` — NUNCA mais "success" genérico. `details` inclui
`exec_technical_success` separadamente, para quem precisar
distinguir os dois conceitos.

## Testes adversariais exigidos — todos cobertos

`tests/test_atlas_pipeline1_discovery.py` — 15 testes novos em 2
classes:
- `CredentialNeverFabricatesEvidenceTests` (8 testes): dummy
  credential nunca gera REAL/FACT (nível fonte e nível pipeline
  completo, cenário exato da auditoria), dummy credential para TODAS
  as plataformas, `UNKNOWN`/`None`/vazio/`NaN`/`Infinity`/`bool`
  forçados como `REAL` nunca passam o gate, `score="UNKNOWN"` e
  `score=NaN/Infinity` nunca viram `READY_FOR_PIPELINE2`.
- `DataStatusNeverMasksAbsenceOfDataTests` (7 testes): zero fontes,
  todas indisponíveis, apenas MOCK sem credencial (com isolamento
  explícito de variáveis de ambiente, nunca dependente do ambiente
  "por acaso" estar limpo), uma fonte real + uma indisponível
  preserva resultado parcial honesto, mais os controles positivos
  `DATA_FOUND`/`DEGRADED`.

Confirmado sem flakiness rodando a suíte completa 5× seguidas.

## Preservação confirmada explicitamente

Reexecutei os 30 arquivos de teste dedicados de TODAS as rodadas
anteriores — **357 testes, todos passando sem alteração**, incluindo
os 33 testes da entrega original do Pipeline 1 e os 95 testes das
dez rodadas de ATLAS EXPANSION 1.

## Arquivos alterados
- `atlas/discovery/sources.py` — `GenericMockDiscoverySource`
  reescrita: nunca fabrica evidência REAL/FACT, credencial presente
  sem implementação real vira `SourceUnavailable`
- `atlas/discovery/quality_gate.py` — `_is_valid_evidence_value()`
  nova, valor inválido (incluindo `NaN`/`Infinity`) nunca passa
  independente do campo `evidence`
- `atlas/discovery/engine.py` — `_status_for()` exige score numérico
  finito; bug latente do `all()` vacuo corrigido; `_score()`
  protegido contra `NaN`/`Infinity`
- `atlas/discovery/pipeline.py` — novo `data_status` +
  `exec_technical_success` no `DiscoveryPipelineReport`; auditoria
  nunca mais registra "success" genérico

## Arquivos alterados (testes)
- `tests/test_atlas_pipeline1_discovery.py` — 15 testes novos

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- Validação de RANGE semântico dos valores (ex: `trend_strength`
  fora de 0-1, valores negativos onde não fazem sentido) não foi
  implementada nesta correção — fora do escopo reportado pela
  auditoria (que tratou de tipo/placeholder, não de faixa semântica);
  registrado como possível refinamento futuro
- Nenhuma fonte real (API oficial) está conectada nesta entrega —
  permanece assim deliberadamente; Pipeline 2 continua não
  implementado
