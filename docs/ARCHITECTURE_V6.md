# Arquitetura V6 — Agentic-First, Multimodal, ALL-ROUNDER, RADAR WEB COMERCIAL

## Lista das IAs — estado real de implementação

| IA | Papel RBAC | Status | Responsabilidade |
|---|---|---|---|
| **ATLAS** | `Role.ATLAS` | ✅ Implementado | Diretor Operacional MASTER, orquestrador central, agêntico, coordenador multiagente |
| **NAVIGATOR** | `Role.NAVIGATOR` | ✅ Implementado | Executor Operacional Multicanal |
| **FORGE** | `Role.FORGE` | ✅ Implementado | Engenharia, Manutenção e Reparo |
| **BRIDGE** | serviço (sem Principal próprio) | ✅ Implementado | Access & Execution Hub — conecta, nunca executa |
| **SENTINEL** | serviço (sem Principal próprio) | ✅ Implementado | Quality Gates / Controle de Qualidade |
| **RADAR WEB COMERCIAL** | serviço (sem Principal próprio) | ✅ Implementado | Descoberta contínua de funis/estratégias — 100% MOCK nesta entrega |
| NEXUS | — | ⏳ Não implementado nesta rodada | Arquitetura de funil/oferta/checkout (pendência declarada) |
| MERCURY | — | ⏳ Não implementado nesta rodada | Estratégia comercial/canais/monetização (pendência declarada) |
| SAGE, METRICA, ECHO e demais | — | ⏳ Não implementados nesta rodada | Ver pendências no changelog |

## Capability Matrix (registrada no bootstrap — `AtlasDirector.__post_init__`)

| Agente | Capability | Nível | Disponível | Provider |
|---|---|---|---|---|
| ATLAS | ORCHESTRATION | MASTER | ✅ | internal |
| ATLAS | REASONING | MASTER | ✅ | internal |
| ATLAS | ALL_ROUNDER | MASTER | ✅ | internal |
| NAVIGATOR | AGENTIC_EXECUTION | SENIOR | ✅ | internal |
| NAVIGATOR | ALL_ROUNDER | PLENO | ✅ | internal |
| FORGE | REPAIR | SENIOR | ✅ | internal |
| FORGE | ALL_ROUNDER | PLENO | ✅ | internal |
| qualquer agente | MULTIMODAL_* | — | ❌ (sem provider real) | MOCK |
| qualquer agente | WEB_RESEARCH | — | ❌ (sem provider real) | MOCK |

Nenhuma capacidade multimodal ou de pesquisa web real está disponível
nesta entrega — declarado explicitamente (`is_available()` sempre
`False` sem registro explícito com provider funcional), nunca
mascarado como funcional.

## Separação Capacidade / Autonomia / Autoridade

```
CAPACIDADE  -> core.capabilities.registry.CapabilityRegistry
               (o que o agente TECNICAMENTE consegue fazer)
AUTONOMIA   -> core.security.rbac.Role + Permission
               (o que o agente PODE fazer sem supervisão passo a passo)
AUTORIDADE  -> Owner Approval (atlas.escalation.EscalationDesk)
               (quem de fato AUTORIZA uma ação crítica)
```

Alta capacidade NUNCA implica autoridade irrestrita — mesmo um agente
com `Capability.ALL_ROUNDER` no nível MASTER continua barrado por
`CRITICAL_ACTION_TYPES`/`CRITICAL_NAVIGATOR_ACTIONS` sempre que a ação
for crítica, inalterado por esta expansão.

## Fluxo agentic-first (item 1)

```
OBJECTIVE
  -> ATLAS.delegate_mission() / receive_objective()
  -> ReasoningEngine.run():
       OBSERVE -> UNDERSTAND -> PLAN -> DECOMPOSE -> SELECT_TOOLS
       -> EXECUTE -> VERIFY -> CRITIQUE -> [REPLAN -> REEXECUTE]*
       -> LEARN -> REPORT
  -> MissionTracker: COMPLETED -> VERIFIED (ou FAILED/ESCALATED)
  = COMPLETE
```

## RADAR + ATLAS + comercial (item 16)

```
RADAR WEB COMERCIAL (radar_web.engine.RadarWebComercial)
  -> ATLAS (consult_radar_web — nunca aceita automaticamente)
  -> [MERCURY/NEXUS — pendência declarada, integração futura]
  -> PLANO (AdvancedPlanner)
  -> SENTINEL (Quality Gate)
  -> NAVIGATOR (execução real)
  -> MÉTRICA (commercial/sales_tracking.py já existente)
  -> aprendizado (WorkingMemory.propose_learning/validate_learning)
```

## Princípios preservados
Todos os princípios estruturais das rodadas anteriores permanecem
intocados: menor privilégio, Owner Approval nunca contornável,
idempotência completa, evidência obrigatória antes de VERIFIED, nunca
inventar métricas/ROI/conversão, nunca declarar sucesso sem prova.
