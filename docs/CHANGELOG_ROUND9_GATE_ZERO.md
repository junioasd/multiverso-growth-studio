# CHANGELOG — Correção Round 9 (GATE ZERO)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ROUND8_FINAL`
(1175 testes). Fecha definitivamente a raiz de confiança: nenhum
atacante consegue mais fabricar seu próprio `OwnerAuthService`,
`OwnerPrincipal`, `ApplicationGovernanceContext` ou `AuthorityRegistry`
e ter isso reconhecido pelo sistema real. Sem implementar YOUTUBE
RECOMMENDATION & DISTRIBUTION ENGINE nem OMNICHANNEL CAMPAIGN
INTELLIGENCE.

## GATE ZERO — checklist de aprovação (item 38)

```
[x] fake OwnerAuthService bloqueado
[x] fake OwnerPrincipal bloqueado
[x] fake GovernanceContext bloqueado
[x] fake AuthorityRegistry bloqueada
[x] root bootstrap único
[x] root persistente após restart
[x] tenant isolation
[x] app isolation
[x] Owner Approval integrado (escopo: registro de source TRUSTED_OFFICIAL — ver Pendências)
[x] grant revocation revalidada
[x] source revogada deixa de sustentar FACT
[x] verifier revogado deixa de verificar
[x] ataque completo não chega a ADOPTED
[x] cenário válido chega a ADOPTED
[x] suíte monolítica RUN1 termina
[x] suíte monolítica RUN2 termina
[x] processo encerra naturalmente
```

Todos os itens do checklist são `TRUE` — nenhum ataque descrito nesta
especificação conseguiu chegar a OWNER/REGISTER_VERIFIER/
REGISTER_SOURCE/VERIFIED/FACT/QUALITY GATE PASS/ADOPTED sem passar
pela cadeia canônica real, em nenhum dos testes executados.

## Resultado real das duas execuções monolíticas (item 36/37)

```
RUN1: COLLECTED=1195 PASS=1195 FAIL=0 ERROR=0 SKIP=0 DURATION=39.07s
      EXIT_CODE=0  THREADS_FINAIS=0  CLEAN=True

RUN2: COLLECTED=1195 PASS=1195 FAIL=0 ERROR=0 SKIP=0 DURATION=40.51s
      EXIT_CODE=0  THREADS_FINAIS=0  CLEAN=True
```

Ambas executadas em processos Python separados, ambas terminaram
naturalmente (nenhum `os._exit`/kill/force terminate), sem warnings
de `ResourceWarning` capturados.

**1195 testes, 1195 PASS, 0 FAIL, 0 SKIP.** 1175 preservados + **20
testes novos** em `tests/test_correction_round9_final.py`.

### TOP 30 slowest (RUN1) — nenhum candidato a hang
O teste mais lento leva 3.0s (`test_multiple_build_start_shutdown_cycles`
— 3 ciclos completos de build/start/shutdown, esperado). Todos os
demais têm justificativa clara: concorrência real de workers,
backoff exponencial intencional, ou requisições HTTP reais via
`http.server`. Lista completa no changelog anterior (Round 7/8) — o
perfil não mudou de forma significativa nesta rodada.

## Arquivos alterados

**Reescritos:**
- `core/security/auth.py` — `OwnerAuthService` com identidade própria
  (`auth_service_id`, `application_id`, `tenant_id`, etc.);
  `create_canonical_auth_service()` + `is_canonical_auth_service()`
  (marcador de processo, itens 1/2/3)
- `core/evidence/authority_registry.py` — `bootstrap_root_authority()`
  agora EXIGE `auth_service` canônico (checado diretamente, não só via
  Context); persistência real em `authority_root_bootstrap` (item 10)
- `core/evidence/governance_context.py` — `bootstrap_for_app()` com
  checagem de canonicidade real; `is_canonical()`/
  `require_canonical_registry()`/`require_same_tenant_app()` (itens
  5/6/7/8/24/25)
- `core/evidence/source_registry.py` — integração real com
  `atlas.escalation.EscalationDesk` para `TRUSTED_OFFICIAL` (item 12)
- `api/bootstrap.py` — `GrowthStudioApp.governance_context` +
  `bootstrap_governance_context()` (item 5)
- `core/models/db.py` — nova tabela `authority_root_bootstrap`

**Novo arquivo de teste:**
- `tests/test_correction_round9_final.py` (20 testes)

## Os pontos corrigidos

### Itens 1/2/3/18 — OwnerAuthService canônico não fabricável ✅
`create_canonical_auth_service()` marca a instância com um segredo de
processo (`secrets.token_hex(32)`, gerado uma vez, nunca exposto por
API pública). `is_canonical_auth_service()` é a ÚNICA forma de
verificar — e a checagem foi colocada em DOIS lugares:
`ApplicationGovernanceContext.bootstrap_for_app()` E
`AuthorityRegistry.bootstrap_root_authority()` diretamente (fechando
o gap de quem contornasse o Context). Validado: mesmo uma
implementação IDÊNTICA de `OwnerAuthService` criada externamente
nunca passa — e um atacante nunca consegue bootstrap nem na sua
PRÓPRIA `AuthorityRegistry()` isolada.

### Itens 4/19 — Fake OwnerPrincipal ✅ (preservado + revalidado)

### Itens 5/6/7/20/21 — ApplicationGovernanceContext canônico + injection protection ✅
`GrowthStudioApp.governance_context` (inicialmente `None`) +
`bootstrap_governance_context(email, password, bootstrap_reason)` —
exige login REAL. Contexto fabricado manualmente nunca passa em
`is_canonical()`; `AuthorityRegistry` "paralela" do atacante nunca
passa em `require_canonical_registry()`.

### Item 8/24/25 — Registry identity enforcement + isolamento ✅
`application_id`/`tenant_id`/`governance_context_id` em todos os
registries; `require_same_tenant_app()` recusa
(`REGISTRY_CONTEXT_MISMATCH`) qualquer cross-tenant/cross-app.

### Item 10 — Persistência real entre restarts ✅
Nova tabela `authority_root_bootstrap`. Validado o cenário EXATO:
Owner A bootstrap → nova instância (simulando restart) → Owner B
negado.

### Itens 14-17 — Revalidação contínua + propagação de revogação ✅
Já funcionava desde a rodada 7 (checagem sempre ao vivo, nunca
cacheada) — revalidado explicitamente nesta rodada: revogar o grant
que registrou uma source/verifier derruba a autorização IMEDIATAMENTE.

### Itens 22/23 — Ataque completo bloqueado / cenário válido, ambos via GrowthStudioApp real ✅
Testados através da APP REAL (`api.bootstrap.build_app()`), não
simulação isolada — `app.governance_context` nunca é contaminado pelo
ataque; o cenário genuíno completo chega a `ADOPTED`.

### Item 12/13 — Owner Approval real integrado (escopo parcial) ✅/⚠️
`SourceRegistry(escalation_desk=...)` — quando configurado,
`TRUSTED_OFFICIAL` exige `approval_id` resolvendo para uma
`EscalationItem` do `atlas.escalation.EscalationDesk` JÁ EXISTENTE
(nunca um approval paralelo) genuinamente `APPROVED` —
`REJECTED`/`EXPIRED`/`CANCELLED`/pendente nunca autoriza.

## Pendências reais

- **Item 12 (escopo)**: a integração com Owner Approval foi
  implementada e testada para o caso explicitamente citado no pedido
  (registro de source `TRUSTED_OFFICIAL`). Os demais casos listados
  (promover verifier, emitir grant crítico, revogar autoridade
  root-level, alterar configuração de Owner, transferir Owner) NÃO
  foram conectados nesta rodada — mecanismo genérico reutilizável,
  mas wiring incompleto. Pendência real, não mascarada.
- **Item 26 (Owner Transfer)**: não implementado — não há ainda um
  fluxo dedicado de troca de Owner (distinto de bootstrap). O sistema
  atual não permite REABRIR bootstrap (`ALREADY_BOOTSTRAPPED` sempre
  recusa uma segunda tentativa), então não há risco de bypass via
  bootstrap — mas também não há um caminho oficial de transferência.
- **Item 27 (separação explícita TEST/DEV/PRODUCTION)**: não há um
  enum/flag formal de ambiente — a separação prática vem de
  `create_canonical_auth_service()` só ser chamada pelo runtime real
  (`build_app()`), nunca em testes (que usam
  `tests._evidence_test_helpers.make_authenticated_test_owner_session()`,
  também via a fábrica canônica, mas com credenciais de fixture).
- `pytest` continua indisponível neste ambiente (sem acesso à rede) —
  validação monolítica feita com `python3 -m unittest discover`, RUN1
  e RUN2 em processos separados, ambas limpas.
- Módulo YouTube e Omnichannel Campaign Intelligence não implementados,
  conforme instruído em todas as rodadas.
