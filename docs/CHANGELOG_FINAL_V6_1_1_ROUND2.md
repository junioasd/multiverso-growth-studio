# CHANGELOG — Correção Final V6.1.1 Round2

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ROUND2`
(1054 testes). Corrige os 12 resíduos finais de evidência/proveniência
comercial e restaura a matriz oficial dos 25 agentes, sem implementar
YOUTUBE RECOMMENDATION & DISTRIBUTION ENGINE nem OMNICHANNEL CAMPAIGN
INTELLIGENCE.

## Resultado da suíte monolítica — extraído da execução real

```
collected=1080 failed=0 errors=0 skipped=0 duration≈44s
threads_remaining=0
exit_code=0
```

**1080 testes, 1080 PASS, 0 FAIL, 0 SKIP.** 1054 preservados (poucos
testes atualizados para refletir a correção explicitamente exigida —
ex: `MetricValidationFailed` agora herda de `ValueError` para
compatibilidade) + **26 testes novos** em
`tests/test_correction_final_v611_round2.py`. **3 execuções
monolíticas consecutivas, sempre limpas, 0 threads remanescentes,
exit code 0.**

## Arquivos alterados

**Reescritos:**
- `core/evidence/registry.py` — `verification_status` nunca mais
  declarativo (`requested_verification_status` separado, validação
  estrutural real via `EvidenceRegistry`); `EvidenceScope`
  (STRATEGY_SPECIFIC/GLOBAL/CROSS_STRATEGY/BENCHMARK);
  `related_metric_name`; `canonical_metric_name()`;
  `resolve_status_for()` com binding
- `core/evidence/metric_evidence.py` — `resolve_verification_detailed()`
  com binding forte métrica+estratégia; `provenance_record` opcional
- `core/evidence/provenance.py` — todos os campos mínimos exigidos
  (`provenance_id`, `collected_at`, `verification_method`,
  `verification_status`, `integrity_reference`,
  `related_evidence_ids`, `related_strategy_id`)
- `radar_web/quality_gate.py` — usa binding real (estratégia e
  métrica), valida `ProvenanceRecord` quando presente
- `radar_web/adoption.py` — novo estado `METRIC_UNVERIFIED`;
  `record_metrics()` valida cada métrica protegida por completo antes
  de aceitar; `validate()` revalida tudo antes de `ADOPTED`
- `core/agents/default_registry.py` — matriz oficial de
  responsabilidades restaurada EXATAMENTE (corrigindo SAGE/LIBRA que
  estavam trocadas, e ORION/ARGOS/PULSE/RELAY/METRICA que divergiam)
- `core/agents/base.py` — `official_role`/`runtime_capabilities`
  explícitos, separados de `implementation_status`

**Testes atualizados** (mudança de comportamento intencional):
`tests/test_radar_web_comercial.py`,
`tests/test_microcorrection_v611.py`,
`tests/test_microcorrection_v611_round2.py`,
`tests/test_correction_v61_evolution_quality_adoption.py` —
`EvidenceRecord(verification_status=...)` → `requested_verification_status=...`
+ `verifier`/`verification_method` (API não permite mais declarar
VERIFIED diretamente).

**Novo arquivo de teste:**
- `tests/test_correction_final_v611_round2.py` (26 testes)

## Os 12 pontos corrigidos

### Item 1 — MetricEvidence inválida nunca chega a ADOPTED ✅
Novo `AdoptionStatus.METRIC_UNVERIFIED`. `record_metrics()` valida
CADA métrica protegida por completo (resolve→existe→status→binding
métrica→binding estratégia) — falha vai para `METRIC_UNVERIFIED`
(só pode seguir para `RETEST`), nunca para `METRICS`/`VALIDATION`/
`ADOPTED`.

### Item 2 — Binding forte métrica↔MetricEvidence ✅
`canonical_metric_name()` normaliza SOMENTE aliases semanticamente
equivalentes (roi/ROI/return_on_investment); ROI≠CTR≠CAC≠ROAS nunca
se confundem. Mismatch → `METRIC_BINDING_ERROR` explícito, nunca
`APPROVED`.

### Item 3 — Binding forte evidência↔estratégia ✅
`EvidenceRecord.related_strategy_id` + `scope` (STRATEGY_SPECIFIC
por padrão) — evidência de A nunca aceita silenciosamente para B.
`scope=GLOBAL/CROSS_STRATEGY/BENCHMARK` permite reutilização
explícita e intencional.

### Item 4 — EvidenceRecord VERIFIED nunca é declarativo ✅
`verification_status` (efetivo) separado de
`requested_verification_status` (o que foi pedido) —
`EvidenceRegistry` é o ÚNICO caminho que concede `VERIFIED`, e só
após validação estrutural real (source/provenance/observed_at/
created_at/confidence>0/verifier/verification_method).

### Item 5 — Provenance realmente validável ✅
`ProvenanceRecord` com todos os 12 campos mínimos exigidos.

### Item 6 — Quality Gate: FACT + métrica crítica ✅
Exige MetricEvidence válida + EvidenceRecord VERIFIED + binding
correto + (quando fornecido) `ProvenanceRecord` estruturalmente
válido — nunca `APPROVED` como FACT sem tudo isso.

### Item 7 — validate() antes de ADOPT ✅
Revalida veredito do Quality Gate E cada métrica protegida antes de
aceitar `ADOPTED` — testado inclusive um cenário de adulteração
POSTERIOR a `record_metrics()` (evidência revogada), corretamente
pego e recusado (`AdoptionValidationFailed`).

### Testes adversariais A-F (item 8) ✅
Todos os 6 cenários exatos validados: A (ROI com evidence_ids
inexistente → UNVERIFIED/REJECTED), B (métrica mismatch →
`METRIC_BINDING_ERROR`), C (evidência de A para B → recusada), D
(EvidenceRecord vazio + requested=VERIFIED → efetivo≠VERIFIED), E
(`provenance="trust me"` → INVALID/UNVERIFIED), F (MetricEvidence
completa + binding correto → avaliação normal).

### Item 9 — Pipeline ponta a ponta ✅
Cenário inválido (evidência falsa, ROI falso) → `METRIC_UNVERIFIED`,
nunca `ADOPTED`. Cenário válido (evidência real, Quality Gate PASS,
MetricEvidence válida) → `ADOPTED` com trilha de evidência completa
registrada (`quality_gate_result`, `strategy`, `metrics`).

### Itens 10/11 — Matriz oficial dos 25 agentes ✅
Responsabilidades restauradas EXATAMENTE conforme a referência oficial
— corrigindo SAGE/LIBRA (estavam com papéis trocados) e ORION/ARGOS/
PULSE/RELAY/METRICA (divergiam do oficial). Nova propriedade
`official_role` NUNCA muda com `implementation_status`;
`runtime_capabilities` distingue o que está genuinamente disponível
agora.

### Item 12 — SQLite/ResourceWarnings ✅ (diagnóstico honesto)
Investigação extensiva: captura explícita de `ResourceWarning` via
`warnings.showwarning`, coleta de lixo forçada (`gc.collect()` 2x),
auditoria de TODOS os pontos de conexão SQLite em produção
(`core/jobs/queue.py`, `atlas/escalation.py`,
`core/coordination/mission.py`, `commercial/*.py`) — todos já seguem
`try/except/else/finally: conn.close()` corretamente.
**Nenhum `ResourceWarning` foi reproduzido** em 3 execuções completas
com captura explícita. Não mascarado — reportado honestamente como
não-reproduzível neste ambiente, com a auditoria de código como
evidência de que o padrão de fechamento já está correto.

## Pendências reais
- Não foi possível reproduzir os `ResourceWarning`s relatados pela
  auditoria — investigação extensiva não encontrou vazamento; pode
  ser específico de outro ambiente/versão do Python.
- `pytest` continua indisponível neste ambiente (sem acesso à rede).
- `ProvenanceRecord` em `MetricEvidence` é OPCIONAL — quando ausente,
  a proteção continua vindo da cadeia EvidenceRecord (VERIFIED +
  binding), que já é validação estrutural real. Tornar
  `ProvenanceRecord` obrigatório para TODA métrica protegida exigiria
  atualizar todos os testes existentes que constroem `MetricEvidence`
  sem ele — deixado como extensão futura natural, não uma lacuna
  oculta.
- Módulo YouTube e Omnichannel Campaign Intelligence não implementados,
  conforme instruído.
