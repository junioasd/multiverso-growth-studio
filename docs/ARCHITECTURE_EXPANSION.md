# Arquitetura — MULTIVERSO GROWTH STUDIO (pós-expansão)

## Hierarquia

```
OWNER (Júnior)
  -> SUPERVISÃO/GOVERNANÇA (GovernedFacade + RBAC)
    -> ATLAS — Diretor Operacional MASTER (orquestrador principal)
      -> NAVIGATOR — Executor Operacional Multicanal
      -> FORGE — Engenharia, Manutenção e Reparo
      -> BRIDGE — Access & Execution Hub (conexão técnica, nunca executa)
      -> SENTINEL — Quality Gates / Controle de Qualidade
      -> RADAR — Descoberta permanente de oportunidades comerciais
      -> demais motores (commercial/*, core/production/*, core/jobs/*)
```

BRIDGE conecta. NAVIGATOR executa. ATLAS dirige. FORGE mantém. SENTINEL
controla qualidade. Nenhuma dessas responsabilidades se mistura.

## Lista das IAs principais

| IA | Papel RBAC | Responsabilidade | Autoridade crítica |
|---|---|---|---|
| ATLAS | `Role.ATLAS` | Planeja, decide, coordena, supervisiona | Nenhuma — sempre escalona ao Owner |
| NAVIGATOR | `Role.NAVIGATOR` | Executa operacionalmente nas plataformas | Nenhuma — ações críticas sempre escalonadas |
| FORGE | `Role.FORGE` | Diagnostica e corrige problemas técnicos | Nenhuma — `apply_fix` exige Owner/ATLAS explícito |

BRIDGE e SENTINEL não são "IAs" com Principal próprio nesta entrega —
são serviços de suporte usados por ATLAS/NAVIGATOR (BridgeHub, Sentinel).

## Matriz de responsabilidades

| Capacidade | ATLAS | NAVIGATOR | FORGE | BRIDGE | SENTINEL |
|---|---|---|---|---|---|
| Planejar/decidir | ✅ | ❌ | ❌ | ❌ | ❌ |
| Executar em plataforma externa | ❌ | ✅ | ❌ | ❌ | ❌ |
| Diagnosticar/corrigir bug | ❌ | ❌ | ✅ | ❌ | ❌ |
| Gerenciar credenciais (referência) | ❌ | ❌ | ❌ | ✅ | ❌ |
| Avaliar Quality Gate | ✅ (via Sentinel) | ❌ | ❌ | ❌ | ✅ |
| Aprovar ação crítica | ❌ (só Owner) | ❌ | ❌ | ❌ | ❌ |
| Publicar/gastar/contratar | ❌ | ❌ | ❌ | ❌ | ❌ |

## Fluxo típico ponta a ponta

```
Owner define objetivo
  -> ATLAS.receive_objective() -> plano operacional
  -> ATLAS.delegate_mission(agent="NAVIGATOR", ...) -> MissionTracker (QUEUED)
  -> NAVIGATOR.execute(...) -> ExecutionLog + (se crítico) EscalationDesk
  -> ATLAS.update_mission_status(..., RUNNING/COMPLETED/FAILED)
  -> SENTINEL.check(...) -> PASS/FAIL/BLOCK/WARNING
  -> se FAIL/BLOCK persistente -> FORGE.detect() -> pipeline DETECT..RELEASE
  -> ATLAS.record_opportunity_outcome / record_learning_observation
  -> aprendizado só vira conhecimento reutilizável após validação
     (WorkingMemory.validate_learning / MasterLibrary.validate)
```

## Princípios estruturais preservados

1. Toda IA subordinada opera com seu PRÓPRIO Principal (RBAC de menor
   privilégio) — nunca "como" ATLAS.
2. Toda mutação de negócio passa pela MESMA `GovernedFacade`.
3. Ação crítica NUNCA executada sozinha — sempre `EscalationDesk`
   (persistido, idempotente, nunca aprovado por reinício).
4. Nenhuma IA declara sucesso sem evidência (`ReasoningTrace.mark_verified`).
5. Autocorreção tem limite explícito — nunca loop infinito; escalona.
6. Aprendizado só vira conhecimento após validação explícita.
7. Todo o hardening V1-V6 e ATLAS+RADAR permanece intocado.
