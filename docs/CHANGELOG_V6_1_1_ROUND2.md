# CHANGELOG — Microcorreção V6.1.1 (rodada 2)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_MICROCORRECTION`
(1025 testes). Corrige os 4 resíduos apontados: autostart real no
entrypoint, níveis oficiais dos 25 agentes, Evidence Registry real, e
diagnóstico/correção do travamento monolítico.

## Resultado da suíte monolítica — extraído da execução real

```
collected/run=1054 failed=0 errors=0 skipped=0 duration≈42s
threads remanescentes=0
exit_code=0
```

**1054 testes, 1054 PASS, 0 FAIL, 0 SKIP.** 1025 preservados (13
testes atualizados para refletir a correção explicitamente exigida —
evidência agora precisa resolver de verdade, listados abaixo) + **29
testes novos** em `tests/test_microcorrection_v611_round2.py`, mais
correções de limpeza em `tests/test_atlas_radar_worker.py` (16 testes,
comportamento preservado, só a garantia de cleanup mudou).
**3 execuções monolíticas consecutivas, sempre limpas, 0 threads
remanescentes, exit code 0.**

## Diagnóstico do travamento monolítico (item 8)

**Causa raiz encontrada**: `tests/test_atlas_radar_worker.py` — o
arquivo de teste mais antigo do projeto — criava `RadarWorker`
diretamente em quase todo teste e chamava `worker.stop()` sem
`try/finally` na maioria dos casos. Uma falha de asserção ENTRE a
criação do worker e o `stop()` (por exemplo, uma variação de tempo/
CPU sob carga da suíte inteira) deixava a thread do worker viva,
rodando sozinha com `poll_interval_seconds` tão baixo quanto 0.03s
pelo resto da execução — contenção de GIL/CPU acumulada, plausível
causa da lentidão extrema/travamento relatado em execuções
monolíticas longas.

**Correção**: reescrevi o arquivo inteiro com uma classe base
`_WorkerTrackingTestCase` — `tearDown` SEMPRE para qualquer worker
rastreado, mesmo quando o teste falha no meio. Encontrei e corrigi uma
lacuna idêntica em `tests/test_microcorrection_v611.py`
(`test_shutdown_stops_both_workers` não registrava o app criado para
limpeza automática).

**Validação**: 3 execuções monolíticas completas e limpas (ver acima),
mais a suíte específica do arquivo corrigido isoladamente. Não há
garantia absoluta de que NENHUMA outra condição de corrida rara exista
em um ambiente diferente, mas a causa mais provável e concreta foi
identificada e corrigida com engenharia real (não com timeout maior,
divisão em blocos, skip ou force exit — nenhuma dessas táticas
proibidas foi usada).

## Arquivos alterados

**Novos:**
- `core/evidence/registry.py` — `EvidenceRegistry`/`EvidenceRecord`/`VerificationStatus`
- `core/evidence/metric_evidence.py` — `MetricEvidence`
- `core/evidence/provenance.py` — `ProvenanceRecord`

**Reescritos:**
- `tests/test_atlas_radar_worker.py` — `_WorkerTrackingTestCase` (cleanup garantido)
- `main.py` — `cmd_serve` inicia o RADAR WEB COMERCIAL de verdade
- `core/config.py` — `radar_web_enabled`/`radar_web_interval_seconds`
- `core/agents/default_registry.py` — níveis oficiais de competência restaurados
- `radar_web/quality_gate.py` — evidência resolvida de verdade + MetricEvidence + downgrade + `invalid_evidence`
- `radar_web/adoption.py` — `record_metrics()` exige `MetricEvidence` para métricas protegidas; aceita `ProvenanceRecord`

**Testes atualizados** (comportamento mudou intencionalmente — a
própria correção pedida): `tests/test_radar_web_comercial.py`,
`tests/test_correction_v61_evolution_quality_adoption.py`,
`tests/test_microcorrection_v611.py` — `evidence_ids` agora precisam
resolver contra um `EvidenceRegistry` real; métricas protegidas como
número solto em `record_metrics()` trocadas por métrica não-protegida
onde o teste não é especificamente sobre evidência de métrica.

**Novo arquivo de teste:**
- `tests/test_microcorrection_v611_round2.py` (29 testes)

## Os 4 pontos corrigidos

### Item 1 — RADAR WEB COMERCIAL: autostart no runtime REAL ✅
`main.py cmd_serve` agora chama `build_app(start_radar_web_worker=True)`
e, quando `SETTINGS.radar_web_enabled` (env `RADAR_WEB_ENABLED`), habilita
o agendamento automaticamente — nunca depende de chamada manual
posterior nem de teste habilitando o scheduler. Validado via
reprodução EXATA da lógica do `cmd_serve` real (não uma versão
simplificada): enabled → executa sozinho; disabled → worker sobe
(infraestrutura) mas nenhum scan ocorre.

### Item 2 — Níveis oficiais dos 25 agentes ✅
Hierarquia exata restaurada (ATLAS=MASTER, SAGE=ESPECIALISTA, etc. —
ver `docs/AGENT_REGISTRY_V6_1_1_ROUND2.md`), corrigindo também
NAVIGATOR/FORGE/SENTINEL/BRIDGE que estavam incorretamente `SENIOR`.
`competency_level` e `implementation_status` confirmados como eixos
independentes (`NEXUS`: `level=MASTER` + `implementation_status=SCAFFOLD`
simultaneamente, sem contradição).

### Item 3 — Quality Gate: evidência precisa existir e ser validada ✅
`core.evidence.registry.EvidenceRegistry` — todo `evidence_id` é
RESOLVIDO de verdade; string arbitrária sem registro correspondente
sempre resolve para `MISSING`. `RadarWebEvidenceGate` reescrito para
usar isso em vez de `bool(evidence_ids)`.

### Item 4 — Métricas comerciais: provenance por métrica ✅
`core.evidence.metric_evidence.MetricEvidence` — métrica protegida
(ROI, ROAS, CTR, conversion_rate, CAC, CPA, CPC, LTV, revenue, sales,
sales_volume, AOV, refund_rate) só pode virar FACT com uma
`MetricEvidence` própria, cujos `evidence_ids` PRÓPRIOS resolvem para
`VERIFIED` — nunca "emprestando" evidência genérica da estratégia.

### Testes adversariais A-E (item 5) — todos validados exatamente ✅
- A: FACT + source + evidence_ids inexistente + roi/ctr → REJECTED
- B: string arbitrária → MISSING EVIDENCE
- C: evidência existe mas UNVERIFIED → nunca sustenta FACT
- D: evidência VERIFIED da estratégia NÃO vinculada ao ROI → ROI continua não-verificado
- E: ROI com MetricEvidence completa + evidência VERIFIED vinculada → avaliação normal, APPROVED

### Item 6 — Adoption Pipeline: provenance estruturada ✅
`core.evidence.provenance.ProvenanceRecord` — `record_metrics()`
aceita string livre (legado, nunca suficiente sozinha para métrica
protegida) ou `ProvenanceRecord` validável. Cenário exato validado:
`evidence={"anything":"x"}, provenance="trust me"` + ROI → sempre
recusado.

### Item 7 — SENTINEL: relatório estruturado + downgrade ✅
`GateResult.criteria` agora sempre inclui `missing_evidence`,
`invalid_evidence` (distinto de missing — ID existe mas
INVALID/EXPIRED), `unsupported_claims`, `metric_issues`,
`verified_claims`, `unverified_claims`, `stale_evidence`,
`provenance_issues`. Novo `evaluate(subject, allow_downgrade=True)` —
`FACT` sem barra suficiente é rebaixado in-place para `ESTIMATE`
(havia alguma evidência, insuficiente) ou `UNKNOWN` (nenhuma) — nunca
mantém `FACT` sem prova quando o downgrade é solicitado.

## Pendências reais
- Não há garantia absoluta contra QUALQUER condição de corrida rara
  em ambientes diferentes — a causa mais provável foi identificada e
  corrigida com engenharia real, validada em 3 execuções monolíticas
  limpas consecutivas.
- `pytest` continua indisponível neste ambiente (sem acesso à rede) —
  validação monolítica feita com `python3 -m unittest discover`.
- Módulo YouTube não implementado, conforme instruído.
