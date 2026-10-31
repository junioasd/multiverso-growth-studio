# CHANGELOG — Correção Round 8 (Final)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ROUND7_FINAL`
(1162 testes). Fecha o bootstrap da raiz de autoridade — `Owner` não
pode mais ser autodeclarado por uma string; exige autenticação real
(e-mail+senha via PBKDF2, reaproveitando `OwnerAuthService` já
existente). Sem implementar YOUTUBE RECOMMENDATION & DISTRIBUTION
ENGINE nem OMNICHANNEL CAMPAIGN INTELLIGENCE.

## Resultado da suíte monolítica — extraído da execução real

```
COLLECTED: 1175  PASS: 1175  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~37s (3 execuções: 37.9s / 37.2s / 38.0s)
THREADS FINAIS (dump_resources): 0
EXIT CODE: 0
```

**1175 testes, 1175 PASS, 0 FAIL, 0 SKIP.** 1162 preservados + **13
testes novos** em `tests/test_correction_round8_final.py`. 3 execuções
monolíticas consecutivas, sempre limpas.

## Arquivos alterados

**Novos:**
- `core/evidence/owner_principal.py` — `OwnerPrincipal` +
  `issue_owner_principal()` (itens 1/2/9/10)
- `core/evidence/governance_context.py` — `ApplicationGovernanceContext`
  (itens 5/6/7)

**Reescritos:**
- `core/evidence/authority_registry.py` — `bootstrap_root_authority()`
  agora exige `owner_principal` + `auth_service`, e SEMPRE RE-DERIVA
  um principal fresco a partir do `session_id` original (nunca confia
  nos campos do objeto sozinho); `registry_id` + `canonical_registry_id`
  em `AuthorityGrant` para proteção contra injeção (itens 6/15)
- `tests/_evidence_test_helpers.py` — bootstrap de teste agora usa uma
  sessão Owner REAL (`OwnerAuthService.login()`) em vez de uma string

**Testes atualizados**: todos os testes em
`test_correction_round6/7_final.py` que chamavam
`bootstrap_root_authority(owner_actor_id=...)` migrados para o fluxo
de sessão autenticada real.

**Novo arquivo de teste:**
- `tests/test_correction_round8_final.py` (13 testes)

## Design: por que reaproveitar OwnerAuthService em vez de inventar algo novo

O projeto já possui um serviço de autenticação REAL do Owner
(`core.security.auth.OwnerAuthService`) — login com e-mail+senha
verificados via PBKDF2, sessão assinada por HMAC (`create_session_token`/
`verify_session_token`), revogação de sessão. Em vez de inventar um
"BootstrapToken" paralelo (item 3, explicitamente opcional no pedido),
`OwnerPrincipal` é emitido a partir de uma sessão REAL desse serviço
já existente — mais forte (assinatura criptográfica real, já auditada
em rodadas anteriores) e sem duplicar mecanismo de confiança.

## Os pontos corrigidos

### Itens 1/9/10 — Bootstrap nunca aceita Owner autodeclarado ✅
`bootstrap_root_authority()` mudou de `owner_actor_id: str` para
`owner_principal: OwnerPrincipal` + `auth_service`. Mesmo que o
chamador construa manualmente um `OwnerPrincipal(authenticated=True,
verification_status=VERIFIED)`, o método SEMPRE re-deriva um
principal fresco a partir do `session_id` alegado, chamando
`issue_owner_principal()` de novo contra o `auth_service` — um
`session_id` fabricado nunca passa por essa re-verificação. Validado
com o Teste 10 exato.

### Item 2 — OwnerPrincipal com campos completos ✅
Todos os campos pedidos: `principal_id`, `owner_id`, `authenticated`,
`authentication_source`, `session_id`, `issued_at`, `expires_at`,
`scopes`, `integrity_reference`, `verification_status`.

### Item 4 — Bootstrap uma única vez ✅ (preservado + reforçado)
Continua bloqueando uma segunda tentativa — agora também exigindo
reautenticação real para a PRIMEIRA.

### Itens 5/6/7 — AuthorityRegistry canônica + injection protection ✅
`ApplicationGovernanceContext.bootstrap()` é o caminho recomendado —
monta `AuthorityRegistry`/`VerifierRegistry`/`SourceRegistry`/
`ClaimRegistry` já amarrados entre si desde a construção.
`require_canonical_registry()` recusa (`GovernanceContextError`/
`AUTHORITY_REGISTRY_REJECTED`) qualquer registry que não seja
genuinamente a instância canônica (`registry_id` comparado). Validado
com o Teste 12 exato.

### Item 15 — Authority Grant Origin ✅
`AuthorityGrant` agora registra `canonical_registry_id`,
`issued_by_principal`, `issued_by_grant` — `validate_grant()` recusa
(defesa em profundidade) qualquer grant cujo `canonical_registry_id`
não bata com o `registry_id` do próprio registry.

### Item 16 — Legacy Registry Migration ✅ (resolvido estruturalmente, preservado da rodada anterior)
Como `bootstrap_root_authority()`/`issue_grant()`/`register()` são os
ÚNICOS caminhos de escrita e todos exigem autoridade genuína, não há
mais nenhum caminho pelo qual um registro "legado" sem
`AuthorityGrant`/`OwnerPrincipal` real seja criado.

### Testes adversariais 10, 12, 17 + cenário 18 — todos validados exatamente ✅
- **10**: fake OwnerPrincipal (mesmo com campos "convincentes") →
  `AuthorityDenied`, bootstrap nunca acontece
- **12**: `AuthorityRegistry` própria do atacante → nunca aceita como
  canônica (`AUTHORITY_REGISTRY_REJECTED`)
- **17**: ataque completo (fake registry → fake Owner → grant → tudo
  mais) → bloqueado já no bootstrap, `_root_bootstrapped` permanece
  `False`, zero grants existem
- **18**: cadeia genuína completa (sessão Owner real → `ApplicationGovernanceContext`
  → grants delegados → verifier/source aprovados → claim → evidence →
  provenance → MetricEvidence → Quality Gate → `validate`) → chega a
  `ADOPTED`

## Pendências reais

- **Item 3 (BootstrapToken)**: explicitamente marcado como opcional no
  pedido — optei por reaproveitar `OwnerAuthService` (mecanismo mais
  forte e já existente) em vez de duplicar um sistema de tokens
  paralelo. Documentado como decisão de design, não uma lacuna.
- **Item 8 (persistência entre restarts)**: NÃO implementado. Hoje
  `AuthorityRegistry`/`VerifierRegistry`/`SourceRegistry` são objetos
  em memória — um restart do processo perde o estado de bootstrap e
  permitiria um NOVO bootstrap (`ALREADY_BOOTSTRAPPED` só protege
  dentro do mesmo processo/instância). Persistir isso em SQLite
  seguiria o mesmo padrão já usado por `AuditTrail`/`SessionStore`,
  mas não coube no escopo desta rodada — pendência real, não
  mascarada.
- **Item 14 (integração com Owner Approval)**: não implementado nesta
  rodada — o projeto já possui um mecanismo de escalonamento/aprovação
  (`atlas/escalation.py`) de rodadas muito anteriores, mas a
  integração explícita com emissão de grants de alto privilégio não
  foi conectada.
- **Itens 19-27 (diagnóstico de teardown)**: nenhuma nova causa de
  hang encontrada nesta rodada, além do que já foi diagnosticado
  (PBKDF2, threads daemon) nas rodadas 6/7. `pytest` continua
  indisponível neste ambiente.
- Módulo YouTube e Omnichannel Campaign Intelligence não implementados,
  conforme instruído em todas as rodadas.
