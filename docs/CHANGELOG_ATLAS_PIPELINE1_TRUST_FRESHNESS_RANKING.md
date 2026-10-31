# CHANGELOG — MICROCORREÇÃO FINAL DE PROFUNDIDADE ATLAS PIPELINE 1

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_PIPELINE1_PROVENANCE_EXPIRATION`
(1775 testes). Corrige as 3 falhas estruturais restantes reportadas
por auditoria independente, preservando integralmente V1, ATLAS
EXPANSION 1 e todas as correções anteriores do Pipeline 1. Pipeline
2 continua não implementado.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1798  PASS: 1798  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~122-125s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, stderr limpo
compileall: OK
```

**1798 testes** (1775 preservados + **23 testes novos**).

## Design revisado ANTES de codificar

`docs/DESIGN_ATLAS_PIPELINE1_TRUST_FRESHNESS_RANKING.md` — modela a
fronteira de confiança, a política de freshness e a fórmula de
ranking, escrito antes de qualquer implementação.

## Falha 1 — proveniência ainda autodeclarável

Confirmado com o cenário exato da auditoria: um adapter customizado,
SEM nenhuma chamada externa, devolvendo sinais com `evidence=REAL`,
`claim_type=FACT`, `source="youtube_official_api"`, `confidence=0.99`,
`observed_at`/`valid_until` coerentes — chegava a `READY_FOR_
PIPELINE2`/`DATA_FOUND`.

### Correção — fronteira de confiança explícita (novo módulo)

`atlas/discovery/provenance.py`:
- `SourceRegistry` — fontes CONHECIDAS/CONFIÁVEIS, registradas pela
  infraestrutura (`ContentDiscoveryEngine.__init__`, a partir da
  lista que o CHAMADOR forneceu), nunca autodeclaradas por um
  adapter.
- `SourceExecutionReceipt`/`ReceiptRegistry` — um comprovante EMITIDO
  pelo engine no instante em que decide chamar `discover()` (nunca
  antes, nunca pelo adapter), com um `execution_id` novo a cada
  chamada; só marcado `COMPLETED` depois que a chamada retorna sem
  exceção.

`ContentDiscoveryEngine.scan_source()` é o ÚNICO lugar que emite e
valida receipts: fonte não registrada → indisponível, sem sequer
chamar `discover()`; após a chamada, CADA sinal de CADA oportunidade
é sanitizado — `evidence != UNKNOWN` sem um `receipt_id` que
corresponda a um receipt `COMPLETED`, da MESMA fonte, da MESMA
execução, é REBAIXADO para `UNKNOWN` antes de qualquer persistência.
Nunca remove a oportunidade, nunca fabrica um valor — só torna a
evidência honesta.

`DiscoverySource.discover()` agora recebe `receipt_id` (novo
parâmetro); `is_test_source: bool` (novo atributo de classe) marca
fontes explicitamente MOCK/TESTE — mesmo com receipt tecnicamente
válido, seus sinais são igualmente rebaixados, a menos que o engine
seja explicitamente configurado com `allow_test_sources=True` (uso
exclusivo de teste determinístico, nunca operacional).

Isto NÃO promete prova criptográfica universal — é uma fronteira de
confiança HONESTA, documentada como tal.

## Falha 2 — `valid_until=None` tratado como eternamente válido

Confirmado: `observed_at` de 1 ano atrás, `valid_until=None`,
proveniência sintaticamente completa — ainda `READY_FOR_PIPELINE2`.

### Correção

Nova `DiscoverySignal.is_fresh(now=None, max_age_seconds=
DEFAULT_FRESHNESS_WINDOW_SECONDS)` — `DEFAULT_FRESHNESS_WINDOW_
SECONDS = 24h`, documentada. Quando `valid_until` está definido:
vigente enquanto `now <= valid_until` (inalterado). Quando é `None`
(desconhecido): vigente somente enquanto `now - observed_at <=
max_age_seconds` — NUNCA eterno. `_status_for()`/`_score()` usam
`is_fresh()` (não mais `is_expired()` isoladamente).

## Falha 3 — ranking ignorava risco/custo/evidência, misturava unidades

Confirmado: `estimated_reach=1_000_000` somado diretamente com
`trend_strength=0.7` produzia `score=500000.35`; `risk`/
`production_cost` existiam no modelo mas eram ignorados pelo ranking.

### Correção — normalização documentada + média ponderada

Cada componente é normalizado para `[0,1]` antes de combinar
(`_SCORE_COMPONENT_WEIGHTS`/`_REFERENCE_MAX_REACH`/`_REFERENCE_MAX_COST`,
documentados no topo de `engine.py`): `trend_strength`/`hook_strength`
usados diretamente (já `[0,1]`, com clamp defensivo);
`estimated_reach` normalizado via referência documentada (1M+ → 1.0);
`competition`/`risk`/`production_cost` INVERTIDOS (quanto maior, pior
— `1 - normalizado`). Combinados por MÉDIA PONDERADA, nunca soma
bruta. Componentes AUSENTES são EXCLUÍDOS (nunca um valor favorável
inventado). Ao menos um sinal de "potencial" (`trend_strength`,
`hook_strength` ou `estimated_reach`) precisa estar presente. O
resultado é multiplicado pela confiança MÉDIA dos componentes usados
(evidence_quality afeta o score explicitamente). Faixa final sempre
`[0,1]`, por construção e clamps defensivos.

## Testes adversariais exigidos — todos os 19 cenários cobertos

`tests/test_atlas_pipeline1_discovery.py` — 23 testes novos:
- `TrustBoundaryAdversarialTests` (9): adapter não registrado com
  campos perfeitos, adapter registrado sem referenciar receipt,
  receipt de outra fonte (isolado + fim-a-fim), receipt de outra
  execução, receipt inválido/nunca completado/envelhecido, controle
  positivo (adapter real + receipt válido chega a READY), MOCK/TEST
  nunca aparece como operacional REAL (com e sem
  `allow_test_sources`).
- `FreshnessAdversarialTests` (4): `valid_until=None` + antigo
  bloqueia; muito antigo vira STALE/LOW_EVIDENCE; `valid_until=None`
  mas recente ainda permite READY; score nunca usa sinal sem
  freshness válida.
- `RankingFormulaAdversarialTests` (8): risco alto não ignorado;
  custo alto não ignorado; confiança altera score; concorrência entra
  na decisão; reach nunca somado bruto com trend; score sempre em
  `[0,1]` mesmo em combinação extrema; potencial idêntico com
  risco/custo diferentes nunca pontua igual; dados ausentes nunca
  viram zero favorável.
- Mais 2 testes adicionais próprios (além do exigido): escalas
  absurdas/`NaN`/`Infinity` em qualquer combinação nunca quebram o
  intervalo `[0,1]`; um `receipt_id` "chutado" (UUID nunca emitido
  pela infraestrutura) nunca valida.

Confirmado sem flakiness rodando a suíte completa 3× seguidas.

## Preservação confirmada explicitamente

Reexecutei os 30 arquivos de teste dedicados de TODAS as rodadas
anteriores — **399 testes, todos passando sem alteração**, incluindo
as 3 correções anteriores do Pipeline 1 e os 95 testes das dez
rodadas de ATLAS EXPANSION 1.

## Arquivos novos
- `docs/DESIGN_ATLAS_PIPELINE1_TRUST_FRESHNESS_RANKING.md`
- `atlas/discovery/provenance.py` — `SourceRegistry`,
  `SourceExecutionReceipt`, `ReceiptRegistry`

## Arquivos alterados
- `atlas/discovery/models.py` — `DiscoverySignal.receipt_id`
  (novo campo), `DEFAULT_FRESHNESS_WINDOW_SECONDS`, `is_fresh()`
- `atlas/discovery/sources.py` — `DiscoverySource.discover()` recebe
  `receipt_id`; `is_test_source` (novo atributo de classe)
- `atlas/discovery/opportunity_store.py` — serialização de
  `receipt_id`
- `atlas/discovery/engine.py` — `scan_source()` reescrito (emissão +
  validação + sanitização de receipts); `_score()`/`_status_for()`
  reescritos (normalização, pesos, freshness)
- `atlas/discovery/pipeline.py` — `allow_test_sources` repassado
  explicitamente ao engine

## Arquivos alterados (testes)
- `tests/test_atlas_pipeline1_discovery.py` — 23 testes novos; fontes
  de teste (`_RealSource` e outras) atualizadas para referenciar o
  `receipt_id` corretamente; 2 testes pré-existentes com valores
  numéricos esperados atualizados (a fórmula de score legitimamente
  mudou — comportamento correto, nunca revertido)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- A fronteira de confiança é HONESTA, não criptograficamente
  inquebrável: um adapter malicioso com acesso de introspecção ao
  mesmo processo Python (ex: `sys._getframe()`) poderia, em teoria,
  alcançar o registro interno do engine — isso está além de qualquer
  fronteira de confiança dentro de um único processo, e exigiria
  isolamento real de processo/sandboxing para ser fechado, fora do
  escopo desta correção (declarado explicitamente, nunca escondido)
- `DEFAULT_FRESHNESS_WINDOW_SECONDS` (24h) e as referências de
  normalização (`_REFERENCE_MAX_REACH`/`_REFERENCE_MAX_COST`) são
  escolhas DOCUMENTADAS mas arbitrárias — o responsável pela
  integração pode calibrá-las por tipo de sinal/fonte quando dados
  reais estiverem disponíveis
- Pipeline 2 continua não implementado
