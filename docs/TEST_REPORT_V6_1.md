# Relatório de Testes — Correção V6.1

## Resultado agregado

```
Ran 991 tests
OK
```
991 testes, 991 PASS, 0 FAIL — 3 execuções consecutivas estáveis,
mais validação independente a partir do ZIP extraído.

## Evidência dos testes adversariais exigidos

### AGENTIC (item 1)
- FORGE mission via NAVIGATOR sem delegação → `BLOCKED` (nunca `VERIFIED`) — validado manualmente e em `test_forge_mission_via_navigator_without_delegation_blocked`
- delegação válida → `VERIFIED` — `test_valid_delegation_allows_execution`
- executor incompatível → `InvalidExecutorError` — `test_resolve_authorized_executor_invalid`
- evidência do executor real registrada no navigator_execution_log — `test_evidence_of_real_executor_recorded`

### 25 AGENTES (item 2)
- todos os 25 registrados — `test_all_25_agents_registered`
- specialization/capabilities/agentic/multimodal/all-rounder/implementation_status presentes para todos — `test_specialization_never_erased_by_all_rounder` e demais
- SCAFFOLD nunca finge execução real (`AgentNotImplemented`) — `test_scaffold_agent_never_fakes_execution`

### RADAR CONTÍNUO (item 3)
- auto-run sem chamada manual — `test_automatic_execution_without_manual_call`
- dois ciclos — `test_two_cycles`
- disable/re-enable — `test_disable_stops_execution`/`test_re_enable_resumes`
- restart preserva agendamento — `test_restart_preserves_schedule`
- concorrência (3 workers simultâneos, `attempts=1` sempre) — `test_concurrency_multiple_workers_no_duplicate`
- falha temporária não mata o worker — `test_temporary_failure_does_not_kill_worker`
- recovery — `test_recovery_after_failure_recorded`
- shutdown sem thread órfã — `test_shutdown_no_orphan_thread`
- execução manual não duplica a automática — `test_manual_execution_while_automatic_active_never_duplicates` (corrigiu um bug real do `JobQueue` em modo memória)

### VISÃO EVOLUTIVA (item 4)
- ID estável entre anos — `test_same_strategy_three_years_same_id_increasing_version`
- version crescente (1,2,3) — mesmo teste
- revisão repetida no mesmo ano exige motivo — `test_repeated_same_year_review_requires_explicit_reason`
- nunca `[1,1]` silencioso — `test_never_1_1_silently`
- histórico preservado — `test_history_preserved_never_erased` (rodada anterior, ainda passando)

### QUALITY GATE (item 5)
- ROI inventado sem source → `REJECTED` — `test_fabricated_roi_without_source_rejected`, validado manualmente com `{"roi": 999999}` exato
- ESTIMATE não vira FACT — `test_estimate_with_partial_evidence_stays_estimate`
- estratégia bem fundamentada → `APPROVED` — `test_well_evidenced_strategy_approved`
- evidência obsoleta sinalizada — `test_stale_evidence_flagged`

### CAPABILITY REGISTRY (item 7)
- UNKNOWN health → nunca usável — `test_unknown_health_never_treated_as_usable_even_with_available_authorized` (cenário exato do bug relatado)
- HEALTHY → usável — `test_healthy_is_usable`
- UNHEALTHY/DISABLED → sempre bloqueado — `test_unhealthy_always_blocked`/`test_disabled_always_blocked`
- provider ausente → `UNAVAILABLE` — `test_unavailable_never_reports_active_or_mock`

## Validação end-to-end (bootstrap real)
Fluxo completo testado via `api.bootstrap.build_app()`: 25 agentes
registrados, `delegation_registry` funcional, `radar_web_scheduler`/
`radar_web_worker` integrados, shutdown limpo sem erro mesmo sem o
worker ter sido iniciado.
