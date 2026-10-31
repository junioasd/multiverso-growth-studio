# DESIGN — ATLAS PIPELINE 1: VALIDAÇÃO CANÔNICA ÚNICA (OUTCOME, LINEAGE, DURABILIDADE)

Escrito ANTES da implementação. Fecha as 3 falhas finais, todas
resolvidas por UMA validação canônica reutilizada em 4 pontos —
nunca 4 verificações diferentes.

## Falha 1 — aquisição existia, mas nada vinculava o sinal ao dado

`SourceExecutionContext.acquire()` devolvia só um `acquisition_id` +
digest — nunca o PAYLOAD em si. Um adapter podia chamar `acquire()`
genuinamente e ainda assim inventar `trend_strength=0.93` sem relação
nenhuma com o que foi obtido. `fetch()` devolvendo `None` ainda
gerava `status=COMPLETED`.

### Corrigido: `AcquisitionOutcome` com observações reais + `data_status`

`AcquisitionTransport.fetch()` agora devolve um `dict` de
OBSERVAÇÕES genuínas (`{campo: valor}`) — ou `None`/`{}` para
"achei nada" (nunca um erro). O broker classifica honestamente:
`data_status = "DATA"` (há observações) ou `"NO_DATA"` (não há) —
`None`/tipo inesperado NUNCA vira `"DATA"`. `AcquisitionOutcome`
expõe `.observations` — o adapter agora TEM dado real para
transformar em sinais.

### Corrigido: lineage verificável, sinal por sinal

Um sinal agora DECLARA `claimed_observation_fields` (quais campos da
aquisição sustentam seu valor). No momento da sanitização
(`scan_source()`, único lugar com acesso à aquisição AO VIVO):
- `FACT`/`REAL`: exige exatamente 1 campo declarado, e o VALOR do
  sinal precisa ser IDÊNTICO ao da observação referenciada — um FACT
  declarando 0.93 quando a observação diz 0.2 é sanitizado para
  `UNKNOWN`.
- `ESTIMATE`/`INFERENCE`: exige ao menos 1 campo declarado e
  EXISTENTE na aquisição — nunca igualdade direta arbitrária (é uma
  estimativa/inferência, não o dado bruto), mas nunca sem lineage
  nenhuma também.
- Verificação bem-sucedida → constrói um `VerifiedLineage` (ver
  abaixo) e o sinal É promovido. Falha → sanitizado para `UNKNOWN`,
  como já era.

## Falha 2 — sinais OPCIONAIS inválidos corrompiam o score

O Quality Gate só validava os 3 sinais ESSENCIAIS
(`trend_strength`/`audience`/`competition`) — sinais OPCIONAIS usados
no ranking (`risk`/`production_cost`/`hook_strength`/`estimated_reach`)
NUNCA passavam por `provenance_issues()`. `risk` com
`confidence=1000` corrompia o score para `272.28`.

### Corrigido: uma validação canônica, chamada para TODO sinal usado

`_usable()` (dentro de `_score()`) agora chama `signal.trust_issues()`
— a MESMA validação canônica usada pelo gate — para QUALQUER sinal
que possa entrar no cálculo, essencial ou opcional. `confidence` fora
de `(0, 1]`, `source` ausente, `claim_type` incoerente, `NaN`/
`Infinity` — tudo já verificado por `trust_issues()` — agora também
exclui sinais opcionais do score, nunca só os essenciais.

## Falha 3 — proveniência não sobrevivia a restart

`acquisition_id` era só uma string — sem o `AcquisitionResult` vivo
no broker (que nunca sobrevive a restart, por design), a string virava
um identificador órfão, mas a evidência já persistida (`evidence=REAL`
etc.) continuava sendo confiada cegamente pelo ranking.

### Corrigido: `VerifiedLineage` — prova DURÁVEL e AUTOCONTIDA

```python
VerifiedLineage:
    source_id, execution_id, acquisition_id, mode, verified_at,
    observation_fields: tuple,
    verified_value_digest: str  # hash de signal.value NO MOMENTO da verificação
```

Persistido JUNTO com o sinal (no mesmo blob JSON da oportunidade) —
NUNCA depende do broker efêmero para ser revalidado depois.
`trust_issues()` (a MESMA validação canônica) verifica, com ou sem
acesso ao broker: `lineage` existe? `hash(signal.value) ==
lineage.verified_value_digest`? Se o valor foi adulterado depois de
persistido (ou se `lineage` nunca existiu — um sinal antigo, nunca
verificado), a checagem falha — `UNKNOWN`, nunca `READY`. Um restart
NUNCA apaga a prova (ela está no próprio registro persistido) nem
transforma ausência de prova em confiança implícita (sem `lineage`,
sempre `UNKNOWN`).

## A validação canônica única

`DiscoverySignal.trust_issues(*, now=None) -> list[str]` — SUPERSET
de `provenance_issues()` (já existente: source/claim_type/confidence/
observed_at/coerência evidence-claim) MAIS a checagem de `lineage`
(presença + integridade do digest). Chamada em EXATAMENTE 4 lugares,
nunca reimplementada:
1. `ContentDiscoveryEngine._sanitize_signal()` — logo após tentar
   construir o `lineage` (a verificação contra a aquisição AO VIVO é
   uma etapa SEPARADA e ANTERIOR — `_verify_against_outcome()` —, mas
   o resultado ainda passa por `trust_issues()` antes de ser aceito,
   nunca um atalho).
2. `ContentOpportunityEvidenceGate.evaluate()`.
3. `ContentDiscoveryEngine._score()`'s `_usable()` — para QUALQUER
   sinal, essencial ou opcional.
4. Implicitamente em qualquer leitura pós-restart — já que `trust_
   issues()` nunca depende de estado vivo do broker, os pontos 2 e 3
   JÁ cobrem "persistência/restart" automaticamente, sem uma quinta
   verificação separada.
