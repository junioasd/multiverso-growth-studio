# Registry Oficial dos 25 Agentes — V6.1.1

Estrutura oficial restaurada (item 2 desta correção). Gerada
diretamente de `core.agents.default_registry.build_default_agent_registry()`
— nenhum dado inventado nesta tabela.

## Matriz completa

| Agent ID | Nome | Especialização | Status | Agentic (arch/runtime) | ALL-ROUNDER (capaz/runtime) | Modalidades |
|---|---|---|---|---|---|---|
| ARGOS | Argos | monitoramento e vigilância contínua de mercado/plataformas | SCAFFOLD | True/False | True/False | LOG, WEB_PAGE |
| ATLAS | Atlas | orquestração master | ACTIVE | True/True | True/True | AUDIO, CODE, DOCUMENT, IMAGE, LOG, PDF, SCREENSHOT, STRUCTURED_DATA, TABLE, TEXT, VIDEO, WEB_PAGE |
| AURORA | Aurora | direção criativa e inspiração visual | SCAFFOLD | True/False | True/False | IMAGE, TEXT |
| BRIDGE | Bridge | conexão técnica | PARTIAL | True/False | True/False | — |
| ECHO | Echo | loop de aprendizado — feedback de resultado real vs mercado | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, TEXT |
| FORGE | Forge | engenharia, manutenção e reparo | ACTIVE | True/True | True/True | CODE, DOCUMENT, LOG, SCREENSHOT |
| FRAME | Frame | produção e análise de vídeo | SCAFFOLD | True/False | True/False | AUDIO, IMAGE, TEXT, VIDEO |
| JURIS | Juris | compliance e conformidade legal/plataforma | SCAFFOLD | True/False | True/False | DOCUMENT, TEXT |
| LIBRA | Libra | avaliação de risco x benefício e conformidade | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, TEXT |
| MERCURY | Mercury | estratégia comercial, canais, venda multicanal, monetização | SCAFFOLD | True/False | True/False | TEXT, WEB_PAGE |
| METRICA | Métrica | analytics — métricas de campanha, funil e canal | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, TABLE |
| NAVIGATOR | Navigator | execução operacional multicanal | ACTIVE | True/True | True/True | SCREENSHOT, STRUCTURED_DATA, TEXT, WEB_PAGE |
| NEXUS | Nexus | arquitetura de funil, oferta, upsell/downsell/order bump, checkout | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, TEXT |
| ORION | Orion | navegação estratégica e mapeamento de oportunidades | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, WEB_PAGE |
| PIXEL | Pixel | análise visual/criativa — thumbnails, criativos, landing pages | SCAFFOLD | True/False | True/False | IMAGE, SCREENSHOT, TEXT |
| PRISMA | Prisma | análise multimodal combinada | SCAFFOLD | True/False | True/False | AUDIO, IMAGE, TEXT, VIDEO |
| PULSE | Pulse | monitoramento de tendências de mercado | SCAFFOLD | True/False | True/False | TEXT, WEB_PAGE |
| RELAY | Relay | comunicações e notificações | SCAFFOLD | True/False | True/False | TEXT |
| SAGE | Sage | conhecimento e aprendizado — curadoria da Biblioteca Mestre | SCAFFOLD | True/False | True/False | DOCUMENT, TEXT |
| SCRIPTOR | Scriptor | copywriting e roteiro | SCAFFOLD | True/False | True/False | DOCUMENT, TEXT |
| SENTINEL | Sentinel | controle de qualidade | PARTIAL | True/False | True/False | LOG, STRUCTURED_DATA, TEXT |
| SONORA | Sonora | produção e análise sonora/trilha | SCAFFOLD | True/False | True/False | AUDIO, TEXT |
| VENTURE | Venture | desenvolvimento de novos negócios e oportunidades | SCAFFOLD | True/False | True/False | STRUCTURED_DATA, TEXT |
| VERITAS | Veritas | validação de fatos e evidência — fact-checking | SCAFFOLD | True/False | True/False | DOCUMENT, TEXT |
| VOX | Vox | produção e análise de voz/narração | SCAFFOLD | True/False | True/False | AUDIO, TEXT |

## Aliases canônicos (item 5)

Cada agente resolve a partir de: `agent_id` oficial (maiúsculo),
`agent_id.lower()`, e para ATLAS/NAVIGATOR/FORGE também o formato
`nome@system` usado historicamente pelo RBAC (`Principal.subject`).

| Canônico | Aliases aceitos |
|---|---|
| ATLAS | `atlas`, `atlas@system` |
| NAVIGATOR | `navigator`, `navigator@system` |
| FORGE | `forge`, `forge@system` |
| (demais 22) | `<nome_oficial>.lower()` |

Alias desconhecido levanta `UnknownAgentAlias` explicitamente — nunca
falha silenciosamente, nunca adivinha.

## Capability Matrix (bootstrap — `AtlasDirector.__post_init__`)

| Agente | Capability | Nível | Provider | Health | Usável |
|---|---|---|---|---|---|
| ATLAS | ORCHESTRATION | MASTER | internal | HEALTHY | ✅ |
| ATLAS | REASONING | MASTER | internal | HEALTHY | ✅ |
| ATLAS | ALL_ROUNDER | MASTER | internal | HEALTHY | ✅ |
| NAVIGATOR | AGENTIC_EXECUTION | SENIOR | internal | HEALTHY | ✅ |
| NAVIGATOR | ALL_ROUNDER | PLENO | internal | HEALTHY | ✅ |
| FORGE | REPAIR | SENIOR | internal | HEALTHY | ✅ |
| FORGE | ALL_ROUNDER | PLENO | internal | HEALTHY | ✅ |
| demais 22 | — | — | MOCK | UNKNOWN | ❌ |

`capability_registry` agora chaveado por nome CANÔNICO (`"ATLAS"`,
`"NAVIGATOR"`, `"FORGE"`) — nunca mais por `principal.subject`
diretamente (item 5, correção da inconsistência relatada).

## Multimodal Matrix (item 6/7)

Ver coluna "Modalidades" na matriz completa acima — cada modalidade é
registrada com `supported=True` (arquitetura reconhece) e
`available=False`/`provider="MOCK"` (nenhum provider real conectado
nesta entrega) — status efetivo sempre `MOCK` ou `UNAVAILABLE`, nunca
`ACTIVE`.

## ALL-ROUNDER Matrix (item 4/9/13)

Todos os 25 têm `all_rounder_capable=True` (arquitetura transversal).
Só ATLAS/NAVIGATOR/FORGE têm `all_rounder_runtime_available=True`
(capacidade `ALL_ROUNDER` registrada e usável no Capability Registry).
`resolve_agent_for_task()` NUNCA seleciona um agente só por essa flag
— exige `can_handle(task_type)` genuíno (via `primary_task_types` ou
`all_rounder_task_types` do contrato) em qualquer um dos 4 tiers de
prioridade (especialista → especialista secundário → ALL-ROUNDER →
escalonamento).
