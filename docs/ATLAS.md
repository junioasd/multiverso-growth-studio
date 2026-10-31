# ATLAS — Diretor Operacional do MULTIVERSO GROWTH STUDIO

## Hierarquia

```
OWNER (Júnior)
  -> SUPERVISÃO/GOVERNANÇA  (core.governance.facade.GovernedFacade + core.security.rbac)
    -> ATLAS — DIRETOR OPERACIONAL  (pacote atlas/)
      -> demais IAs, motores e agentes (commercial/*, core/production/*, core/jobs/*, ...)
```

ATLAS **não é Owner**. Ele opera através do mesmo `GovernedFacade` usado
por qualquer outro principal do sistema, com um `Principal` de
`role=Role.ATLAS` cujas permissões são fixas em `core/security/rbac.py`
— sem `MANAGE_CREDENTIALS`, `MANAGE_GLOBAL_OPERATIONS`,
`VIEW_GLOBAL_DASHBOARD`, `APPROVE_PUBLICATION` ou `PUBLISH_CONTENT`.

## Arquitetura

```
atlas/
  director.py            AtlasDirector — orquestrador central, exige Principal role=ATLAS
  planning.py             ObjectivePlanner — objetivo do Owner -> plano operacional (checklist)
  opportunity.py            OpportunityEvaluator — envolve MultiGerenciadorComercial (sem duplicar lógica)
  performance.py             PerformanceAnalyst — dados reais via GovernedFacade, evidência REAL/ESTIMATED/FORECAST/UNKNOWN
  quality_supervisor.py       QualitySupervisor — traduz veredito de Quality Gates em PROCEED/BLOCK/ESCALATE
  escalation.py                 EscalationDesk — única saída para ações que exigem autoridade do Owner
  learning.py                    AtlasLearningCoordinator — só grava observações no LearningLoop existente
```

Nenhum arquivo deste pacote toca `core/models/db.py`, `commercial/crm.py`,
`commercial/sales_tracking.py`, `commercial/finance.py`,
`core/jobs/queue.py` ou `core/governance/facade.py` — todo o hardening
aprovado até a V6 (atomicidade, idempotência entre domínios,
concorrência, migração/recuperação) permanece intocado.

## Garantias estruturais de autoridade

1. **Construção**: `AtlasDirector(facade, principal)` levanta
   `AtlasHierarchyViolation` se `principal.role != Role.ATLAS`.
2. **Mutações**: toda operação de negócio passa pela mesma
   `GovernedFacade` — identidade, binding, RBAC, isolamento e replay
   idênticos a qualquer outro principal.
3. **Ações críticas** (`publish_content`, `spend_money`,
   `contract_service`, `modify_credentials`, `connect_external_account`):
   `AtlasDirector.request_critical_action()` SEMPRE escalona ao Owner via
   `EscalationDesk` — nunca executa, mesmo que uma checagem de permissão
   isolada por acaso permitisse.
4. **Escalonamento**: só o Owner resolve (`EscalationDesk.resolve()`
   chama `require_owner()` — a mesma checagem usada em todo o sistema).
5. **Evidência**: `PerformanceAnalyst` só lê dados via
   `GovernedFacade.get_dashboard_view()` — nunca inventa métrica; campos
   sem dado suficiente ficam `"UNKNOWN"` (nunca um número fabricado).
6. **Qualidade**: `QualitySupervisor` nunca trata `UNKNOWN` como
   aprovação por omissão — sempre BLOCK ou ESCALATE.
7. **Aprendizado**: `AtlasLearningCoordinator` só sabe gravar observações
   (`worked`/`failed`/`inconclusive`) — nenhum método capaz de alterar
   RBAC, isolamento, Quality Gates ou schema existe neste pacote.

## Fluxo típico

```
Owner autentica -> Owner.receive_objective(objective, channel_id)
                 -> ATLAS gera OperationalPlan (checklist, sem inventar dados)
                 -> ATLAS.evaluate_opportunities(...) via MultiGerenciadorComercial
                 -> Production Engine produz conteúdo
                 -> ATLAS.supervise_video(...) -> PROCEED | BLOCK | ESCALATE
                 -> ATLAS.analyze_performance(channel_id=...) com dados reais
                 -> ATLAS.record_learning_observation(...)
                 -> ações críticas (publicar, gastar) sempre em
                    ATLAS.request_critical_action(...) -> aguarda o Owner
```

## Limitações desta entrega (ATLAS V1)

- Nenhuma API externa real conectada (Seção 62 / instrução deste lote).
- Classificação de categoria de oferta (produto/infoproduto/afiliado/
  serviço/produto próprio) só é diferente de `"affiliate"` quando o
  próprio catálogo fornecer `metadata["offer_category"]` — nunca inferida
  a partir do nome/conteúdo do produto.
- CAC/LTV/ROI só são calculados quando o chamador fornece os insumos
  reais necessários (`marketing_spend`, `new_customers`, etc.) — nunca
  estimados automaticamente.
