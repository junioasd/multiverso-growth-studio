# CHANGELOG — Correção Final Round 9: Continuidade da Governança/Owner após Restart

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ROUND9_GATE_ZERO`
(1195 testes). Corrige a falha residual confirmada: após um restart
real, a raiz de autoridade ficava marcada como "já bootstrapada" mas
**sem nenhum grant, permissão ou decisão crítica utilizável** — o
sistema não conseguia realmente continuar operando.

## Causa raiz (reproduzida antes de corrigir)

A rodada anterior (Round 9 — GATE ZERO) só persistia o **fato** do
bootstrap raiz ter ocorrido (`authority_root_bootstrap`). Os
`AuthorityGrant`s em si (permissões concedidas), as **revogações**
(decisões críticas) e os registros de `VerifierRegistry`/
`SourceRegistry` (fontes/verificadores aprovados) viviam **somente em
memória**. Além disso, `AuthorityRegistry.registry_id` era gerado do
zero a cada `__init__()` — mesmo restaurando o bootstrap do banco, uma
nova instância tinha um `registry_id` DIFERENTE do original, então
qualquer grant que sobrevivesse (não sobrevivia) teria seu
`canonical_registry_id` invalidado de qualquer forma.

Reproduzido exatamente:
```
ANTES do restart -- grant valido? True
APOS restart -- grant original ainda resolve? None
APOS restart -- grant ainda valido? False
APOS restart -- registry_id mudou? authreg_01e105a3e44e vs authreg_4ff9f0945678
```

## Resultado real após a correção

```
RUN1: COLLECTED=1206 PASS=1206 FAIL=0 ERROR=0 SKIP=0 DURATION=45.08s
      EXIT_CODE=0  THREADS_FINAIS=0  CLEAN=True

RUN2: COLLECTED=1206 PASS=1206 FAIL=0 ERROR=0 SKIP=0 DURATION=43.14s
      EXIT_CODE=0  THREADS_FINAIS=0  CLEAN=True

ResourceWarnings (captura explícita + gc.collect() duplo): 0
```

**1206 testes, 1206 PASS, 0 FAIL, 0 SKIP.** 1195 preservados + **11
testes novos** em `tests/test_correction_final_round9_continuity.py`.
Ambas execuções monolíticas em processos Python separados, ambas
terminaram naturalmente.

## Arquivos alterados

- `core/models/db.py` — 3 novas tabelas (`authority_grants`,
  `authority_registered_verifiers`, `authority_registered_sources`) +
  coluna `canonical_registry_id` em `authority_root_bootstrap`
  (migração aditiva via `_ensure_column`, nunca perde dado existente)
- `core/evidence/authority_registry.py` — `registry_id` agora
  RESTAURADO do banco (estável entre restarts) em vez de gerado do
  zero; `_load_persisted_grants()`/`_persist_grant()`; `revoke_grant()`/
  `expire_grant()` persistem a mudança de status; nova propriedade
  pública `is_root_bootstrapped`
- `core/evidence/verifier_registry.py` — `_load_persisted_verifiers()`/
  `_persist_verifier()`, `db_path` opcional
- `core/evidence/source_registry.py` — mesmo padrão para sources
- `core/evidence/governance_context.py` — novo `restore_for_app()`
  (caminho dedicado para restaurar, nunca tenta re-bootstrap);
  `context_id` agora usa `authority_registry.registry_id` (estável
  entre bootstrap e restore — antes gerava um UUID novo a cada vez,
  quebrando a continuidade do próprio `context_id`)
- `api/bootstrap.py` — `GrowthStudioApp.restore_governance_context()`

**Nenhuma funcionalidade aprovada alterada** — todas as mudanças são
aditivas (novos parâmetros opcionais `db_path`, novos métodos); o
comportamento em memória (sem `db_path`) permanece idêntico.

## O que passou a persistir e restaurar corretamente

1. **`AuthorityGrant`s** — concessão, escopos, nível, expiração,
   status. Um grant emitido antes do restart continua válido depois.
2. **Revogações** — uma decisão de `revoke_grant()` sobrevive a
   restart; nunca "volta atrás" silenciosamente.
3. **Registros de `VerifierRegistry`/`SourceRegistry`** — um
   verificador/fonte aprovado continua autorizado/sustentando FACT
   após restart, sem precisar ser re-registrado.
4. **`registry_id`** — estável entre restarts (restaurado do banco),
   preservando a validade de `canonical_registry_id` nos grants
   persistidos.
5. **API limpa de restauração** — `restore_governance_context()`
   exige a MESMA autenticação real de `bootstrap_governance_context()`
   (nunca um bypass silencioso), mas reconstrói o contexto do estado
   persistido em vez de tentar (e falhar) um novo bootstrap.

## Testes específicos de restart/persistência (item explicitamente pedido)

- `test_grant_survives_restart`
- `test_registry_id_stable_across_restart`
- `test_revocation_survives_restart` (decisão crítica preservada)
- `test_verifier_registration_survives_restart`
- `test_source_registration_survives_restart`
- `test_source_revoked_grant_after_restart_stops_sustaining_fact`
  (revogação + restart combinados)
- `test_owner_b_denied_after_restart_exact_scenario` (cenário exato:
  Owner A bootstrap → restart → Owner B negado — preservado)
- `test_restore_without_prior_bootstrap_denied` (`NOT_BOOTSTRAPPED`)
- `test_full_app_restore_cycle` (ponta a ponta via `GrowthStudioApp`
  real, incluindo `context_id` estável)
- `test_restore_denied_with_wrong_credentials`
- `test_repeated_bootstrap_restore_cycles_no_leak` (5 ciclos de
  grant/revoke + 3 "restarts" simulados, captura explícita de
  `ResourceWarning`)

## Bugs reais encontrados e corrigidos durante esta própria correção

Ao escrever os testes desta correção, encontrei e corrigi:
1. Um `setUp()` de teste faltando `db_module.init_db()`.
2. **Inconsistência de `context_id`**: `bootstrap_for_app()` gerava um
   UUID novo (`ctx_...`) a cada chamada, enquanto o novo
   `restore_for_app()` usava `authority_registry.registry_id`
   (`authreg_...`) — os dois nunca coincidiam entre o bootstrap
   original e uma restauração posterior. Corrigido fazendo
   `bootstrap_for_app()` também usar `authority_registry.registry_id`
   como `context_id` — agora ambos os fluxos produzem o MESMO
   identificador estável.

## Pendências reais

- `ClaimRegistry` ainda não é persistido (claims precisam ser
  re-registradas após restart) — não fazia parte do escopo desta
  correção pontual (que focou em autoridade/permissões/verifier/
  source, conforme solicitado), mas é uma extensão natural do mesmo
  padrão já implementado.
- `pytest` continua indisponível neste ambiente (sem acesso à rede).
- Módulo YouTube e Omnichannel Campaign Intelligence não implementados,
  conforme instruído em todas as rodadas.
