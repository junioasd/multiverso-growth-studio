# CHANGELOG — Correção Round 4 (Final)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_FINAL`
(1080 testes). Fecha definitivamente as brechas de confiança em
evidência/proveniência/verificador comercial, sem implementar YOUTUBE
RECOMMENDATION & DISTRIBUTION ENGINE nem OMNICHANNEL CAMPAIGN
INTELLIGENCE.

## Resultado da suíte monolítica — extraído da execução real

```
COLLECTED: 1106  PASS: 1106  FAIL: 0  ERROR: 0  SKIP: 0
DURATION TOTAL: ~45s (3 execuções consecutivas: 45.2s / 46.1s / 45.6s)
threads_remaining: 0
exit_code: 0
```

**1106 testes, 1106 PASS, 0 FAIL, 0 SKIP, 0 XFAIL.** 1080 preservados
+ **26 testes novos** em `tests/test_correction_round4_final.py`. 3
execuções monolíticas consecutivas, sempre limpas.

## Arquivos alterados

**Novos:**
- `core/evidence/verifier_registry.py` — `VerifierRegistry`/
  `VerifierRegistration`/`TrustLevel` (item 2)
- `core/evidence/source_registry.py` — `SourceRegistry`/`SourceType`
  (item 3)
- `core/evidence/metric_evidence_validator.py` — `MetricEvidenceValidator`
  canônico único (item 11)
- `tests/_evidence_test_helpers.py` — helper compartilhado de teste
  (verificador de teste explicitamente autorizado — nunca usado em
  produção)

**Reescritos:**
- `core/evidence/registry.py` — `EvidenceRecord.source_type` +
  `related_claim_id`; `VERIFIED` agora exige POLÍTICA REAL (estrutura
  + `source_type` aceitável + `verifier` REGISTRADO e AUTORIZADO via
  `VerifierRegistry`, nunca aceito só por não ser vazio);
  `CanonicalMetricRegistry` formalizado como classe (CPM/MARGIN/PROFIT
  adicionados)
- `core/evidence/provenance.py` — `verification_status` não-declarativo
  (`grant_verification()` é o único caminho para `VERIFIED`, exige
  `verifier` autorizado)
- `radar_web/quality_gate.py` — usa `MetricEvidenceValidator`
  centralizado (nunca mais lógica de validação própria e divergente)
- `radar_web/adoption.py` — `_validate_protected_metrics()` usa o
  MESMO `MetricEvidenceValidator`
- `core/evidence/claims.py` — CPM/MARGIN/PROFIT adicionados às
  métricas protegidas
- `tests/test_atlas_radar_worker.py`,
  `tests/test_correction_v61_agentic_agents_radar.py` — deadlines de
  polling reduzidos de 5.0s para 3.0s (item 17 — redução de risco de
  tempo acumulado)

**Testes atualizados** (comportamento mudou intencionalmente — a
própria correção pedida): todos os arquivos que construíam evidência
"VERIFIED" agora usam um `VerifierRegistry` de teste com um
verificador EXPLICITAMENTE autorizado (`tests/_evidence_test_helpers.py`)
e `source_type="OFFICIAL"`; `MetricEvidence` "completas" agora incluem
uma `ProvenanceRecord` genuinamente `VERIFIED` (antes opcional, agora
obrigatória — item 4).

**Novo arquivo de teste:**
- `tests/test_correction_round4_final.py` (26 testes)

## Bug real encontrado e corrigido durante esta rodada

Ao integrar o `MetricEvidenceValidator` centralizado, um teste de
regressão (`test_tampered_metrics_after_gate_still_caught_at_validate`)
revelou que o validador não gerava uma mensagem de `issue` quando a
falha vinha SÓ do status da evidência (sem um erro de binding
específico) — a lista de `issues` ficava vazia mesmo com a métrica
reprovada, fazendo o chamador (`record_metrics()`/`validate()`)
pensar erroneamente que tudo estava correto. Corrigido: agora qualquer
status diferente de `VERIFIED` sempre gera uma mensagem explícita,
mesmo sem binding_error. Validado com o cenário exato: evidência
revogada (`INVALID`) DEPOIS de `record_metrics()` aceitar, ainda
corretamente pega em `validate()`.

## Os 14 pontos de evidência/proveniência corrigidos

### Item 1 — EvidenceRecord VERIFIED não pode ser autodeclarado ✅
Campos não vazios (mesmo arbitrários como `source="totally-fake-source"`)
NUNCA bastam. `is_structurally_sufficient_for_verified()` agora exige
POLÍTICA REAL: estrutura + `source_type` aceitável + `verifier`
REGISTRADO e AUTORIZADO em `VerifierRegistry` (vazio por padrão).

### Item 2 — Verifier Registry / Trust Policy ✅
`VerifierRegistry` com `verifier_id`/`verifier_type`/`authorized`/
`capabilities`/`verification_methods`/`health`/`trust_level`/`scope`.
`verifier="self"` (ou qualquer nome nunca registrado) sempre recusado
— registro VAZIO por padrão.

### Item 3 — Source Validation ✅
`SourceType` (OFFICIAL/FIRST_PARTY/PLATFORM_API/OWN_OPERATION_DATA/
PUBLIC_WEB/BENCHMARK/USER_PROVIDED/MOCK/UNKNOWN) — MOCK/UNKNOWN NUNCA
sustentam FACT, mesmo com verifier autorizado.

### Item 4 — ProvenanceRecord obrigatória para FACT ✅
`MetricEvidenceValidator` exige `MetricEvidence.provenance_record`
presente E genuinamente `VERIFIED` (via `grant_verification()`) —
ausente ou inválida = sempre `UNVERIFIED`.

### Item 5 — ProvenanceRecord: validação completa ✅
Todos os campos mínimos exigidos + `verification_status` considerado
(`INVALID` nunca é válida; só `VERIFIED` sustenta FACT).

### Item 6 — Provenance verification não-declarativa ✅
Mesma disciplina do `EvidenceRecord` — `requested_verification_status`
separado de `verification_status` efetivo; `grant_verification()` é o
único caminho, exige `VerifierRegistry` real.

### Item 7 — Binding TRIPLO obrigatório ✅
Chave solicitada ↔ `MetricEvidence.metric_name` ↔
`EvidenceRecord.related_metric_name` — os três precisam bater após
canonicalização. Testes D e E exatos validados (mismatch em qualquer
um dos três vértices → `METRIC_BINDING_ERROR`).

### Item 8 — Canonical Metric Registry ✅
`CanonicalMetricRegistry` formalizado como classe; CPM/MARGIN/PROFIT
adicionados aos aliases.

### Item 9 — Binding de claim ✅
`EvidenceRecord.related_claim_id` adicionado (junto de
`related_strategy_id`/`related_metric_name` já existentes).

### Item 10 — Binding de estratégia ✅ (preservado)
Confirmado intacto: evidência de A nunca comprova B, exceto
`GLOBAL`/`BENCHMARK`/`CROSS_STRATEGY` explícito.

### Item 11 — MetricEvidenceValidator centralizado ✅
Validador ÚNICO usado por Quality Gate E `record_metrics()`/`validate()`
do pipeline de adoção — mesma instância de lógica, nunca divergente
(o bug real encontrado nesta rodada é evidência direta do valor desta
centralização).

### Item 12 — Métricas protegidas expandidas ✅
CPM, MARGIN, PROFIT adicionados à lista protegida.

### Item 13 — Quality Gate nunca confia em status pré-preenchido ✅
Todo o fluxo sempre RESOLVE de novo contra `EvidenceRegistry`/
`VerifierRegistry` — nunca lê `verification_status="VERIFIED"` como
prova suficiente por si só.

### Item 14 — Adoption Pipeline: bloqueio final ✅ (preservado + reforçado)
`validate(decision="ADOPTED")` revalida Quality Gate + métricas
críticas (agora via `MetricEvidenceValidator`) antes de aceitar —
qualquer falha → `AdoptionValidationFailed`, nunca `ADOPTED`.

## Os 10 testes adversariais (item 15) — todos validados exatamente

A-J: todos os cenários exatos do pedido testados manualmente E
formalizados em `tests/test_correction_round4_final.py`, incluindo o
TESTE I (ataque completo: ROI falso + source falsa + provenance falsa
+ verifier falso + evidência "aparentemente" VERIFIED) — bloqueado no
Quality Gate, nunca chega a `ADOPTED`; e o TESTE J (cenário
genuinamente válido) — segue normalmente até `ADOPTED`.

## Diagnóstico da regressão monolítica (itens 16-19)

**Medição real feita nesta rodada**: instrumentei a suíte com
timing por teste. Resultado: duração total ~45s, os 20 testes mais
lentos somam ~22s (50% do tempo total), nenhum teste individual
excede 3.3s. **Não foi possível reproduzir um travamento ou tempo
próximo de 180s neste ambiente**, mesmo com medição cuidadosa.

**Ação real tomada, não apenas diagnóstico**: identifiquei que os
loops de polling com deadline somam até 66s de tempo ADICIONAL no
PIOR CASO teórico (se todos expirassem sem sucesso) — um risco
quantificável e real em ambientes mais lentos/carregados. Reduzi os
deadlines mais longos (5.0s → 3.0s, 3 ocorrências) para diminuir esse
risco acumulado, sem reduzir a margem de segurança normal (testes
continuam passando confortavelmente em frações de segundo no caminho
feliz).

**Top 5 testes mais lentos** (medidos nesta rodada):
1. `test_multiple_build_start_shutdown_cycles` — 3.24s (3 ciclos completos de build/start/shutdown, esperado)
2. `test_backoff_increases_wait_after_consecutive_failures` — 1.50s (backoff exponencial proposital)
3. `test_multiple_workers_same_db_never_duplicate_scan` — 1.24s (concorrência real com 4 workers)
4. `test_logout_revocation_persists_across_new_server_instance` — 1.11s
5. `test_autostart_real_end_to_end` — 1.09s

Nenhum candidato a "hang" genuíno identificado — todos os testes mais
lentos têm justificativa clara (concorrência real, backoff
intencional, múltiplos ciclos).

## Pendências reais
- Não foi possível reproduzir os >180s relatados pela auditoria
  externa neste ambiente, mesmo com instrumentação detalhada. É
  possível que o ambiente da auditoria seja significativamente mais
  lento/carregado (CPU compartilhada, contenção de I/O). A mitigação
  real feita (redução de deadlines) reduz o pior-caso teórico em ~6s;
  não elimina 100% a possibilidade de uma máquina MUITO mais lenta
  ainda ultrapassar 180s, mas reduz o risco de forma genuína.
- `pytest` continua indisponível neste ambiente (sem acesso à rede).
- Módulo YouTube e Omnichannel Campaign Intelligence não implementados,
  conforme instruído.
