# OPERATIONAL 1.0.1 TEST REPORT

## Regressão completa
Comando: `python -W error::ResourceWarning -m unittest discover -s tests -t . -v` (log em `regressao_completa_1.0.1.txt`).

| Testes | PASS | FAIL | ERROR | SKIP | RC | ResourceWarning | compileall |
|---|---|---|---|---|---|---|---|
| 2.073 | 2.071 | 0 | 0 | 2 | 0 | 0 | OK |

- **Skips (2):** `test_real_windows_*`, que exigem Windows real e foram aprovados em Windows real no Comando 1.
- **Evolução:** 2.049 (v1.0.0) + 24 novos. **Nenhum teste existente foi alterado ou removido.**
- **Runner estrito** (`tests/_helpers/strict_resource_runner.py tests.test_operacional_1_0_1`): RC 0.

## Testes adicionados — `tests/test_operacional_1_0_1.py` (24)
| # exigido | Teste |
|---|---|
| 1 | `test_01_blocks_navigator_real_even_with_approval`, `test_01b_navigator_level_block_happens_before_dispatch_and_covers_browser` |
| 2 | `test_02_blocks_operation_runner_without_consuming_attempts` |
| 3, 9 | `test_03_09_worker_leaves_pending_job_untouched` |
| 4 | `test_04_scheduler_skips_external_tasks_but_recovers` |
| 5 | `test_05_persists_across_restart_and_processes` (processo `main.py killswitch status`) |
| 6, 7 | `test_06_07_only_owner_with_reason_and_audited` |
| 8 | `test_08_retry_fallback_direct_api_webhook_and_paid_provider_cannot_bypass` |
| fail-closed | `test_unreadable_state_fails_closed_without_creating_db` |
| 10 | `test_10_worker_recovers_job_after_crash` |
| 11 | `test_11_two_workers_never_process_same_job_twice` |
| 12, 13 | `test_12_13_scheduler_no_duplicate_tick_and_restart` (inclui `main.py scheduler --once` e `main.py worker --once`) |
| 14 | `test_14_logs_persist_rotate_and_survive_restart` |
| 15 | `test_15_secrets_never_reach_log_files` (MGS_VAULT_MASTER_KEY, SESSION_SECRET, OAuth, API keys, senha do Owner, Bearer, traceback) |
| 16 | `test_16_usage_recorded_with_all_fields` |
| 17 | `test_17_paid_provider_without_limit_is_blocked` (0 chamadas saem) |
| 18 | `test_18_unknown_cost_is_never_invented` |
| 19–21 | `test_19_21_cli_backup_hash_no_overwrite_retention_restore` (processo `main.py backup`), `test_20b_same_timestamp_never_overwrites` |
| 22 | `test_22_db_path_validation` |
| 23 | `test_23_placeholder_and_weak_secrets_rejected` |
| 24 | `test_24_missing_key_never_silently_ephemeral_in_production` |
| 25 | `test_25_cookie_secure_in_production` |
| 26 | `test_26_readiness_reflects_states` (inclui prova de somente leitura) |
| 27, 28 | `tests.test_correcao_auditoria_independente` e `tests.test_correcao_reauditoria_migracao_replay`, verdes na regressão |
| 29 | regressão completa acima |

## Mutação — 16 de 16 detectados
Mutantes: trava removida no NavigatorReal, na API direta, no webhook, no OperationRunner, no worker e no scheduler; fail-open; qualquer papel altera o kill switch; provedor sem limite liberado; custo inventado; backup sobrescreve; redação desligada; chave efêmera em produção; cookie sem Secure; readiness ignora o worker; scheduler duplica janela.

**Transparência — 4 mutantes passaram cegos na primeira rodada:**
- navegador: lacuna real de cobertura, fechada com `test_01b`;
- colisão de timestamp: fechada com `test_20b`;
- redação: defesa em duas camadas, com mutante refeito sobre `redact`;
- OperationRunner: mutante mal construído, refeito.

## Defeitos encontrados e corrigidos durante a construção
1. **`build_app` configurava estado global:** banco órfão entre testes gerava bloqueio em cascata (20 falhas na 1ª regressão). A configuração passou para os processos `main.py`.
2. **A consulta ao kill switch criava banco vazio** quando o arquivo não existia; agora não cria (fail-closed).
3. Variável errada em `remove_db_file`.
4. Bloco do kill switch inserido fora do lugar em `_request`.
5. Arquivo aberto sem fechamento em `readiness.py`.
