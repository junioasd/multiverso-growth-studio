# CHANGELOG — MICROCORREÇÃO ARQUITETURAL FINAL ATLAS PIPELINE 1

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_PIPELINE1_TRUST_FRESHNESS_RANKING`
(1798 testes). Corrige as 3 falhas estruturais restantes reportadas
por auditoria independente, preservando integralmente V1, ATLAS
EXPANSION 1 e todas as correções anteriores do Pipeline 1. Pipeline
2 continua não implementado.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1808  PASS: 1808  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~121-123s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, stderr limpo
compileall: OK
```

**1808 testes** (1798 preservados + **10 líquidos** — Pipeline 1 foi
de 90 para 100 testes; ver detalhes abaixo).

## Design revisado ANTES de codificar

`docs/DESIGN_ATLAS_PIPELINE1_ACQUISITION_BOUNDARY.md`.

## Falha 1 — o receipt ainda não provava aquisição

Confirmado com o cenário exato da auditoria: um adapter customizado
recebia um `receipt_id` genuíno (emitido ANTES da chamada, completado
só porque `discover()` retornou) e simplesmente o copiava para sinais
inventados — `technical_success=True`, `data_status=DATA_FOUND`,
`READY_FOR_PIPELINE2`, `score=0.9174`.

### Correção — camada de TRANSPORTE controlada

Reescrevi `atlas/discovery/provenance.py` por completo: o adapter
NUNCA mais recebe um comprovante pronto. Recebe um
`SourceExecutionContext` que só permite SOLICITAR uma aquisição
através de `context.acquire()` — que consulta o `AcquisitionTransport`
GENUINAMENTE REGISTRADO no `SourceAcquisitionBroker` para aquele
`source_id` (nunca autodeclarado pelo adapter, nunca derivado da
simples presença em `sources=[...]`). Só DEPOIS de `transport.fetch()`
realmente acontecer (ou levantar) um `AcquisitionResult` verificável é
construído — pelo broker, nunca pelo adapter.

`ContentDiscoveryEngine` registra, por padrão, um
`NotImplementedTransport` (SEMPRE levanta, honestamente) para CADA
fonte fornecida — nenhuma integração é inventada só para fazer um
teste passar; o modo operacional honesto continua UNAVAILABLE/
NOT_IMPLEMENTED até uma infraestrutura futura registrar um transporte
REAL genuíno. `scan_source()` sanitiza (rebaixa para `UNKNOWN`)
qualquer sinal cujo `acquisition_id` não corresponda a um
`AcquisitionResult` válido (mesma fonte, mesma execução, `COMPLETED`,
`mode="REAL"` — ou `"TEST"` só com `allow_test_sources=True`) ANTES
de persistir.

Threat model declarado honestamente no design doc e no código: esta
arquitetura impede um adapter defeituoso/customizado de fabricar
prova de aquisição só devolvendo sinais — NÃO promete isolamento
contra código Python hostil com acesso arbitrário ao mesmo processo.

## Falha 2 — `valid_until` malformado derrubava o ranking

Confirmado: `valid_until="tomorrow"` → `TypeError: '<=' not
supported between instances of 'float' and 'str'` dentro de
`rank_all()`.

### Correção

Nova `_is_valid_timestamp()` em `models.py` — valida tipo/finitude
ANTES de qualquer comparação. `is_fresh()`/`is_expired()` nunca mais
levantam para `string`/`bool`/`None` fora de lugar/`NaN`/`Infinity`
— sempre retornam `False`/tratam como expirado, fail-closed. Também
valida `valid_until >= observed_at` quando ambos são timestamps
genuínos (uma vigência terminando antes de ter sido observada é
incoerente). `rank_all()` agora envolve CADA oportunidade
individualmente em tratamento de exceção — um dado genuinamente
malformado (além do que as validações de tipo cobrem) nunca derruba
o ranking das demais.

## Falha 3 — `evidence_quality` existia mas era ignorado

Removido de `DISCOVERY_SIGNAL_KEYS` — a representação CANÔNICA de
qualidade de evidência já existe e já é usada de verdade: a confiança
MÉDIA (`confidence`) dos sinais que efetivamente contribuem para o
score (multiplicando o resultado final, desde a correção anterior).
Um campo `evidence_quality` separado e autodeclarado pela fonte
reintroduziria exatamente o tipo de autodeclaração não verificável
que a fronteira de aquisição existe para eliminar.

## Testes adversariais exigidos — todos cobertos

`tests/test_atlas_pipeline1_discovery.py` — arquivo passou de 90 para
100 testes (a classe `TrustBoundaryAdversarialTests` da rodada
anterior foi INTEIRAMENTE substituída pela nova arquitetura de
broker, mais 4 testes adversariais adicionais próprios):
- **Proveniência (8 cenários)**: fonte customizada no construtor
  nunca vira confiável automaticamente; fonte que recebe o context
  mas nunca chama o broker nunca chega a READY; adapter que só
  devolve sinais inventados nunca chega a READY; reutilização de
  `acquisition_id` de outra fonte/execução nunca passa; aquisição
  fake/TEST nunca vira REAL em modo operacional; fonte operacional
  aprovada + broker controlado + aquisição concluída + sinais
  coerentes chega a READY; dummy credential preservado
  (`ALL_SOURCES_UNAVAILABLE`).
- **Vigência malformada (nova classe dedicada)**: TODOS os tipos
  explicitamente listados (string, `bool`, `NaN`, `+Inf`, `-Inf`,
  negativo, anterior a `observed_at`, `None` com dado antigo, valor
  válido futuro) testados individual e conjuntamente — nenhum
  derruba `rank_all()`; uma oportunidade malformada nunca bloqueia o
  ranking das demais.
- **`evidence_quality` (nova classe dedicada)**: campo confirmado
  removido do contrato; qualidade baixa vs. alta (via confiança)
  produz diferença observável de prioridade, com tudo o mais
  idêntico — cenário exato da auditoria.
- **4 testes adversariais adicionais próprios**, ao tentar quebrar a
  própria correção: adapter "esperto" que captura a falha de
  aquisição e tenta forjar um resultado substituto (ainda bloqueado);
  payload de transporte com chaves de dicionário de tipos mistos
  (nunca comparáveis) nunca derruba o cálculo do digest; payload com
  referência circular idem; múltiplas chamadas a `context.acquire()`
  na mesma `discover()` — uso legítimo, cada uma genuinamente válida.

Confirmado sem flakiness rodando a suíte completa 3× seguidas.

## Preservação confirmada explicitamente

Reexecutei os 30 arquivos de teste dedicados de TODAS as rodadas
anteriores — **409 testes, todos passando sem alteração**, incluindo
as 3 correções anteriores do Pipeline 1 e os 95 testes das dez
rodadas de ATLAS EXPANSION 1.

## Arquivos alterados
- `atlas/discovery/provenance.py` — reescrito por completo:
  `AcquisitionResult`, `AcquisitionTransport`,
  `NotImplementedTransport`, `SourceAcquisitionBroker`,
  `SourceExecutionContext` (substituem `SourceExecutionReceipt`/
  `ReceiptRegistry`/`SourceRegistry`)
- `atlas/discovery/models.py` — `DiscoverySignal.receipt_id`
  renomeado para `acquisition_id`; `_is_valid_timestamp()` nova;
  `is_fresh()`/`is_expired()` totalmente defensivos;
  `evidence_quality` removido de `DISCOVERY_SIGNAL_KEYS`
- `atlas/discovery/sources.py` — `discover()` recebe `context`
  (nunca mais `receipt_id`); `is_test_source` removido (trust vem do
  transporte, nunca de um atributo autodeclarado)
- `atlas/discovery/opportunity_store.py` — serialização de
  `acquisition_id` (com compatibilidade retroativa de leitura para
  `receipt_id` legado)
- `atlas/discovery/engine.py` — `scan_source()`/`_sanitize_signal()`
  reescritos para a fronteira de aquisição; `rank_all()` defensivo
  por oportunidade individual
- `atlas/discovery/pipeline.py` — `broker` repassado explicitamente

## Arquivos alterados (testes)
- `tests/test_atlas_pipeline1_discovery.py` — reescrita substancial:
  helpers de broker/transporte de teste centralizados
  (`_broker_with_test_transport_for`, `_engine_with_trusted_sources`,
  `_pipeline_with_trusted_sources`); `_RealSource` e demais fontes de
  teste atualizadas para usar `context.acquire()` corretamente;
  `TrustBoundaryAdversarialTests` inteiramente reescrita; 2 novas
  classes (`MalformedTemporalNeverCrashesRankingTests`,
  `EvidenceQualityCanonicalRepresentationTests`) mais
  `AcquisitionBoundaryFurtherAdversarialTests`

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- Threat model deliberadamente limitado, declarado no design doc e
  no código: nenhuma proteção contra código Python hostil com acesso
  de introspecção ao mesmo processo — exigiria isolamento real de
  processo, fora de escopo
- `DEFAULT_FRESHNESS_WINDOW_SECONDS`, `_REFERENCE_MAX_REACH`,
  `_REFERENCE_MAX_COST` continuam escolhas documentadas mas
  arbitrárias — calibráveis quando dados reais existirem
- Pipeline 2 continua não implementado
