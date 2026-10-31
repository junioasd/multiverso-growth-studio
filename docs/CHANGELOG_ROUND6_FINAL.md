# CHANGELOG — Correção Round 6 (Final)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ROUND5_FINAL`
(1131 testes). Fecha a raiz de confiança do sistema comercial —
nenhum componente pode mais fabricar seu próprio verifier, source,
claim, evidence ou provenance e usar essa cadeia artificial para virar
FACT/ADOPTED. Sem implementar YOUTUBE RECOMMENDATION & DISTRIBUTION
ENGINE nem OMNICHANNEL CAMPAIGN INTELLIGENCE.

## Resultado da suíte monolítica — extraído da execução real

```
COLLECTED: 1147  PASS: 1147  FAIL: 0  ERROR: 0  SKIP: 0  XFAIL: 0
DURATION: ~36s (3 execuções: 35.4s / 35.3s / 36.8s)
THREADS FINAIS: 0
WORKERS FINAIS: 0
EXIT CODE: 0
WARNINGS: nenhum (ResourceWarning) capturado
```

**1147 testes, 1147 PASS, 0 FAIL, 0 SKIP.** 1131 preservados + **16
testes novos** em `tests/test_correction_round6_final.py`. 3 execuções
monolíticas consecutivas, sempre limpas. **Duração caiu de ~43s para
~36s** (redução real de ~16%) graças à otimização do item 24-26 (ver
abaixo).

## Arquivos alterados

**Reescritos:**
- `core/evidence/verifier_registry.py` — `register()` é agora o ÚNICO
  método público de escrita, sempre exige `actor_id`/`authority_level`/
  `registration_reason`; não existe mais caminho de baixo nível
  separado de uma "policy" opcional
- `core/evidence/source_registry.py` — mesmo padrão; `requested_trust_class`/
  `requested_verification_status` separados dos campos efetivos
  (`field(init=False)`); `TRUSTED_OFFICIAL` exige `OWNER`/`GOVERNANCE`
- `radar_web/models.py` — `FunnelStrategy.primary_claim_id`/`claim_ids`
- `radar_web/quality_gate.py` — FACT exige `primary_claim_id`
  resolvendo no `ClaimRegistry` para a MESMA estratégia; `strategy.source`
  precisa resolver no `SourceRegistry`
- `core/evidence/metric_evidence_validator.py` — checa
  `PROVENANCE_STRATEGY_MISMATCH`/`CLAIM_BINDING_ERROR` da `ProvenanceRecord`
- `core/evidence/provenance.py` — `related_claim_id`/`scope` adicionados
- `core/security/auth.py` — `hash_password()` aceita `iterations`
  opcional (produção nunca usa, continua 260.000 — inalterado)

**Novos:**
- `tests/_auth_test_helpers.py` — `fast_hash_password()` (1.000
  iterações, só para fixtures de teste — nunca produção)
- `tests/test_correction_round6_final.py` (16 testes)

**Testes atualizados**: ~26 testes que usavam `claim_type=FACT` sem
`primary_claim_id` (comportamento antigo incorreto) corrigidos com o
novo helper `register_test_claim()`; ~40 pontos de chamada de
`hash_password()` em testes trocados por `fast_hash_password()`
(itens 24-26).

## Os pontos corrigidos

### Itens 1/2/3 — VerifierRegistry: registro direto bloqueado ✅
Antes, `VerifierRegistrationPolicy` era uma camada OPCIONAL — chamar
`VerifierRegistry.register()` diretamente contornava a governança.
Agora `register()` **é** a porta de governança — método único,
sempre exige autoridade real. Validado: `register(verifier_id="self",
authorized=True, ...)` chamado diretamente, sem contexto de
autoridade, é sempre recusado.

### Itens 4/5/6 — SourceRegistry: mesmo padrão ✅
`register()` único e sempre protegido; `trust_class`/
`verification_status` efetivos nunca vêm do que foi pedido — só do
que a autoridade aprovou. `TRUSTED_OFFICIAL` exige especificamente
`OWNER`/`GOVERNANCE` (nunca `ATLAS_ADMIN` sozinho).

### Itens 7-10 — Claim binding obrigatório para FACT ✅
`FunnelStrategy.primary_claim_id` — `claim_type=FACT` sem isso é
sempre `BLOCKED`. Quando presente, precisa resolver no
`ClaimRegistry` E pertencer à MESMA estratégia.

### Itens 11/12 — ProvenanceRecord strategy/claim mismatch ✅
`related_strategy_id`/`related_claim_id` de uma `ProvenanceRecord`
precisam bater com o que está sendo avaliado — `PROVENANCE_STRATEGY_MISMATCH`/
`CLAIM_BINDING_ERROR` quando não batem (exceto `scope=BENCHMARK/GLOBAL/
CROSS_STRATEGY` explícito).

### Itens 13/14 — strategy.source resolve no SourceRegistry ✅
Para `claim_type=FACT`, `strategy.source` precisa resolver de
verdade — nunca basta ser uma string não vazia.

### Itens 15-23 — Cadeia completa + testes adversariais ✅
Validados os 6 testes adversariais (18-22) e o cenário 23
(genuinamente válido, com governança real em cada elo — verifier,
source, claim, evidence, provenance — chegando a `ADOPTED`).

## Diagnóstico da variação de tempo na regressão monolítica (itens 24-27)

**Achado real, quantificado**: `hash_password()` usa PBKDF2-HMAC-SHA256
com 260.000 iterações (valor OWASP correto para produção). Medido
neste ambiente: **~73ms por chamada**. Encontrados **40 pontos de
chamada** em arquivos de teste (a maioria como fixture —
`owner_password_hash=hash_password(...)` para `build_app()`/
`OwnerAuthService`). Esse custo é **genuinamente ligado à velocidade
da CPU** — numa máquina com desempenho single-core
significativamente menor (comum em runners de CI compartilhados/
limitados), a MESMA operação pode custar várias vezes mais, e a
soma de dezenas dessas chamadas (mais o overhead de qualquer
contenção de GIL/threads concorrentes nos testes de worker) é uma
explicação real e quantificável — não uma suposição sem evidência —
para parte da variação de tempo relatada.

**Ação corretiva real tomada** (não apenas diagnóstico): adicionado
parâmetro opcional `iterations` a `hash_password()` — produção nunca
o usa, continua com 260.000 inalterado (nenhuma segurança real
enfraquecida). Testes que só precisam exercitar o FLUXO de
autenticação (não a força criptográfica) agora usam
`fast_hash_password()` (1.000 iterações) via `tests/_auth_test_helpers.py`.
**Resultado medido nesta rodada**: duração da suíte completa caiu de
~43s para ~36s (redução real de ~16%, consistente em 3 execuções).
Não modificamos nenhum teste que testa a própria função
`hash_password`/`verify_password` (mantidos com a força real, em
`PasswordHashingTests`).

**Top 5 testes mais lentos** (após a otimização):
1. `test_multiple_build_start_shutdown_cycles` — 3.0s (3 ciclos completos, esperado)
2. `test_backoff_increases_wait_after_consecutive_failures` — 1.5s (backoff exponencial proposital)
3. `test_logout_revocation_persists_across_new_server_instance` — 1.4s
4. `test_multiple_workers_same_db_never_duplicate_scan` — 1.3s (concorrência real, 4 workers)
5. `test_concurrency_multiple_workers_no_duplicate` — 1.0s

Nenhum candidato a "hang" identificado — todos têm justificativa
clara (concorrência real, múltiplos ciclos, backoff intencional).

## Pendências reais
- Não foi possível reproduzir os 120-240s+ relatados pela auditoria
  externa neste ambiente. A hipótese do custo de PBKDF2 é real e
  quantificada, e a mitigação aplicada reduziu genuinamente o tempo
  medido (~16%) — mas não há garantia de que explique 100% da
  variação relatada em uma máquina desconhecida e possivelmente
  muito mais lenta/carregada.
- `pytest` continua indisponível neste ambiente (sem acesso à rede).
- Módulo YouTube e Omnichannel Campaign Intelligence não implementados,
  conforme instruído em todas as rodadas.
