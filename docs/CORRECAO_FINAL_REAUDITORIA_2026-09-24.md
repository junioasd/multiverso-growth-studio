# CORREÇÃO FINAL PÓS-REAUDITORIA — MULTIVERSO GROWTH STUDIO

**Reauditoria:** `docs/evidencias/REAUDITORIA_INDEPENDENTE_GROWTH_STUDIO_2026-09-24.md`, veredito **NÃO APROVADO** por 1 bloqueador (replay de evento legado após migração).
**Base:** `MULTIVERSO_GROWTH_STUDIO_MASTER_FINAL_CORRIGIDO_2026-09-24.zip` (SHA `6203d553…721b`, verificado).
**Status:** correção concluída; **não** aprovado para operação real. A decisão depende de nova auditoria independente.

## 1. Resultado
| | MASTER CORRIGIDO (reauditado) | MASTER FINAL (esta correção) |
|---|---|---|
| Testes | 2.040 | 2.049 (+9) |
| PASS / FAIL / ERROR | 2.038 / 0 / 0 | 2.047 / 0 / 0 |
| SKIP | 2 (Windows real) | 2 (Windows real) |
| RC / ResourceWarning / compileall | 0 / 0 / OK | 0 / 0 / OK |
| Replay legado divergente (1; 999; reembolso 40) | **ACEITO** | **REJEITADO** (`JourneyConflict`) |
| Replay legado equivalente comprovado | aceito | aceito (retry idempotente) |
| Legado sem prova suficiente | aceito (presumido) | **bloqueado** (`LEGACY_EVENT_UNVERIFIABLE`) |

## 2. Reprodução (upgrade REAL, não fabricado)
1. O código do **MASTER anterior** (`9b498594…`, sem `event_fingerprints_json`) cria o banco e processa: pagamento `g_old` de 100 no pedido `legacy_real`; pagamento, entrega e reembolso de 80 no pedido `legacy_ref`.
2. O mesmo arquivo é aberto pela versão sob teste, que roda a migração real.
- **Antes (MASTER CORRIGIDO):** replays com valor 1, valor 999 e reembolso de 40 foram **aceitos**.
- **Depois:** os três são **rejeitados**; o equivalente é aceito; receita, reembolso e vendas ficam inalterados.
- **Scripts:** `docs/evidencias/reauditoria_etapa1_criar_banco_legado.py` e `reauditoria_etapa2_replay_pos_migracao.py`.
- **Saídas:** `reauditoria_reproducao_ANTES.txt` e `reauditoria_reproducao_DEPOIS.txt`.

## 3. Causa-raiz
Em `OrderJourney._check_replay`, um evento presente em `processed_events_json` e ausente de `event_fingerprints_json` (todo evento legado após a migração, que cria a coluna com `{}`) era tratado como **retry idempotente sem prova**. A equivalência do conteúdo era presumida.

## 4. Estratégia de migração adotada
**Verificação determinística sob demanda, com falha fechada.**
1. **A migração continua aditiva e idempotente** (`_ensure_column`) e **não grava fingerprint retroativo**. Nenhum dado histórico é inventado, e o primeiro replay pós-upgrade não define o conteúdo original (os testes provam que o mapa continua `{}` após replays).
2. **Reconstrução só a partir do histórico persistido no processamento original, nunca do replay recebido:**
   - transição e evidência: `history_json` do pedido (exatamente uma entrada com o `event_id`);
   - valor, para PAID e REFUNDED: o lançamento efetivo no livro (`idempotency_records`, chave `<pedido>:revenue` / `<pedido>:refund`), que **precisa conferir** com o evento de venda (`<pedido>:sale` / `<pedido>:refund`).
3. **Decisão:**
   - conteúdo comprovadamente igual → retry idempotente;
   - diferente → `JourneyConflict`;
   - **histórico ausente, ambíguo ou contraditório → `JourneyConflict` com `LEGACY_EVENT_UNVERIFIABLE`, inclusive para replay aparentemente idêntico**, e o evento fica para revisão.
4. **Eventos novos:** a semântica estrita da correção anterior não mudou (fingerprint persistido na transição).

## 5. Arquivo alterado
- **Produção:** somente `commercial/journey.py` (`_check_replay`, `_idem_amount`, `_legacy_fingerprint`).
- **Novo teste:** `tests/test_correcao_reauditoria_migracao_replay.py`. **Nenhum teste existente foi alterado.**

## 6. Testes adicionados (9) — banco com o DDL EXATO do MASTER anterior + migração real
| Exigência da reauditoria | Teste |
|---|---|
| migração não fabrica fingerprint | `test_migration_does_not_fabricate_fingerprints` |
| banco antigo → migração → replay equivalente | `test_exactly_equivalent_legacy_replay_is_idempotent` |
| banco antigo → mesmo ID com valor diferente (1; 999; 100,01) | `test_legacy_replay_with_different_amount_is_conflict` |
| banco antigo → outros campos divergentes (evidência, reembolso, entrega) | `test_legacy_replay_with_other_divergent_fields_is_conflict` |
| sem prova suficiente → fail-closed | `test_unverifiable_legacy_event_fails_closed_even_if_identical` |
| histórico contraditório → fail-closed | `test_contradictory_history_fails_closed` |
| evento novo pós-migração: retry legítimo e divergente bloqueado | `test_new_events_after_migration_keep_strict_semantics` |
| restart + migração executada várias vezes | `test_restart_and_repeated_migration` |
| concorrência pós-migração (8 threads: 4 equivalentes, 4 divergentes) | `test_concurrent_legacy_replays_after_migration` |

Todos verificam a **preservação de receita, pedido e estado financeiro**: nenhuma cobrança ou receita duplicada.

- **Mutação:** 5 de 5 mutantes detectados:
  1. defeito original;
  2. fingerprint fabricado a partir do replay;
  3. fail-open sem prova;
  4. sem conferência cruzada livro × venda;
  5. valor ignorado na reconstrução.
- **Runner estrito:** RC 0.

## 7. Integridade
- As correções A–D da auditoria anterior estão intactas: seus 9 testes estão verdes e seus arquivos de produção não foram tocados, exceto `journey.py`, onde só `_check_replay` mudou.
- Os seis Comandos Mestres estão intactos: suítes verdes na regressão completa.
