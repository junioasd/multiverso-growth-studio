# Registry Oficial dos 25 Agentes — V6.1.1 (rodada 2, níveis corrigidos)

Gerada diretamente de `core.agents.default_registry.build_default_agent_registry()`
— nenhum dado inventado. Corrige os níveis de competência oficiais
(item 2 desta rodada) que antes estavam incorretamente rebaixados
(SENTINEL/BRIDGE/NAVIGATOR/FORGE apareciam como `SENIOR`; os 20
agentes SCAFFOLD apareciam todos como `JUNIOR`).

## Matriz completa (competência restaurada)

| Agent ID | Nome | Especialização | Competência | Status Impl. | Agentic (arch/runtime) | ALL-ROUNDER (capaz/runtime) | Modalidades |
|---|---|---|---|---|---|---|---|
| ARGOS | Argos | monitoramento e vigilância contínua de mercado/plataformas | MASTER | SCAFFOLD | True/False | True/False | LOG, WEB_PAGE |
| ATLAS | Atlas | orquestração master | MASTER | ACTIVE | True/True | True/True | AUDIO, CODE, DOCUMENT, IMAGE, LOG, PDF, SCREENSHOT, STRUCTURED_DATA, TABLE, TEXT, VIDEO, WEB_PAGE |
| AURORA | Aurora | direção criativa e inspiração visual | MASTER | SCAFFOLD | True/False | True/False | IMAGE, TEXT |
| BRIDGE | Bridge | conexão técnica | MASTER | PARTIAL | True/False | True/False | — |
| ECHO | Echo | loop de aprendizado — feedback de resultado real vs mercado | MASTER | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, TEXT |
| FORGE | Forge | engenharia, manutenção e reparo | MASTER | ACTIVE | True/True | True/True | CODE, DOCUMENT, LOG, SCREENSHOT |
| FRAME | Frame | produção e análise de vídeo | ESPECIALISTA | SCAFFOLD | True/False | True/False | AUDIO, IMAGE, TEXT, VIDEO |
| JURIS | Juris | compliance e conformidade legal/plataforma | ESPECIALISTA | SCAFFOLD | True/False | True/False | DOCUMENT, TEXT |
| LIBRA | Libra | avaliação de risco x benefício e conformidade | MASTER | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, TEXT |
| MERCURY | Mercury | estratégia comercial, canais, venda multicanal, monetização | MASTER | SCAFFOLD | True/False | True/False | TEXT, WEB_PAGE |
| METRICA | Métrica | analytics — métricas de campanha, funil e canal | MASTER | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, TABLE |
| NAVIGATOR | Navigator | execução operacional multicanal | MASTER | ACTIVE | True/True | True/True | SCREENSHOT, STRUCTURED_DATA, TEXT, WEB_PAGE |
| NEXUS | Nexus | arquitetura de funil, oferta, upsell/downsell/order bump, checkout | MASTER | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, TEXT |
| ORION | Orion | navegação estratégica e mapeamento de oportunidades | MASTER | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, WEB_PAGE |
| PIXEL | Pixel | análise visual/criativa — thumbnails, criativos, landing pages | ESPECIALISTA | SCAFFOLD | True/False | True/False | IMAGE, SCREENSHOT, TEXT |
| PRISMA | Prisma | análise multimodal combinada | ESPECIALISTA | SCAFFOLD | True/False | True/False | AUDIO, IMAGE, TEXT, VIDEO |
| PULSE | Pulse | monitoramento de tendências de mercado | MASTER | SCAFFOLD | True/False | True/False | TEXT, WEB_PAGE |
| RELAY | Relay | comunicações e notificações | ESPECIALISTA | SCAFFOLD | True/False | True/False | TEXT |
| SAGE | Sage | conhecimento e aprendizado — curadoria da Biblioteca Mestre | ESPECIALISTA | SCAFFOLD | True/False | True/False | DOCUMENT, TEXT |
| SCRIPTOR | Scriptor | copywriting e roteiro | ESPECIALISTA | SCAFFOLD | True/False | True/False | DOCUMENT, TEXT |
| SENTINEL | Sentinel | controle de qualidade | MASTER | PARTIAL | True/False | True/False | LOG, STRUCTURED_DATA, TEXT |
| SONORA | Sonora | produção e análise sonora/trilha | ESPECIALISTA | SCAFFOLD | True/False | True/False | AUDIO, TEXT |
| VENTURE | Venture | desenvolvimento de novos negócios e oportunidades | ESPECIALISTA | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, TEXT |
| VERITAS | Veritas | validação de fatos e evidência — fact-checking | ESPECIALISTA | SCAFFOLD | True/False | True/False | DOCUMENT, TEXT |
| VOX | Vox | produção e análise de voz/narração | ESPECIALISTA | SCAFFOLD | True/False | True/False | AUDIO, TEXT |

## Separação competência × disponibilidade (item 2)

```
COMPETENCY_LEVEL     -> o que o agente é OFICIALMENTE reconhecido
                         como capaz de fazer (acordado, nunca
                         autodeclarado)
IMPLEMENTATION_STATUS -> se HÁ, agora, um executor real rodando esse
                         contrato (ACTIVE/PARTIAL/SCAFFOLD/UNAVAILABLE)
```

Exemplo concreto: `NEXUS` tem `competency_level=MASTER` E
`implementation_status=SCAFFOLD` simultaneamente — sem contradição.
Um agente pode ser oficialmente MASTER em competência e ainda não ter
runtime real implementado nesta entrega.

## Evidence Registry / MetricEvidence (itens 3/4)

```
core.evidence.registry.EvidenceRegistry
  - register(EvidenceRecord) -> EvidenceRecord
  - resolve(evidence_id) -> EvidenceRecord | None
  - resolve_status(evidence_id) -> VerificationStatus
      (MISSING quando não registrado; EXPIRED quando valid_until
       passou, mesmo que o registro diga VERIFIED)

core.evidence.metric_evidence.MetricEvidence
  - metric_name, value, unit, source, provenance, observed_at,
    context, measurement_type, sample_window, confidence,
    evidence_ids, verification_status
  - resolve_verification(registry) -> pior status entre TODOS os
    evidence_ids vinculados (nunca "quase verificado")

core.evidence.provenance.ProvenanceRecord
  - source_type, source_reference, collection_method, collector,
    observed_at, verification_status, evidence_ids
  - is_structurally_valid() -> checagem mínima de campos obrigatórios
```

## Quality Gate — relatório estruturado (item 7)

`RadarWebEvidenceGate.evaluate(subject, allow_downgrade=False)` devolve
`GateResult.criteria` sempre com: `source`, `evidence`, `freshness`,
`confidence`, `claim_type`, `fact_evidence_bar` (quando aplicável),
`metric_provenance`, `missing_evidence`, `invalid_evidence`,
`unsupported_claims`, `metric_issues`, `verified_claims`,
`unverified_claims`, `stale_evidence`, `provenance_issues`, e
`claim_downgraded_to` (quando `allow_downgrade=True` e o downgrade
ocorreu).
