# OPERATIONAL 1.0.1 CHANGELOG — `1.0.1-OPERATIONAL-CANDIDATE`

- **Base:** cópia operacional (bundle do Comando Operacional 1, branch `operacao` == tag `v1.0.0`, commit `82a20eed`).
- **Predecessor:** `932292a9835fa8ca3b42515cfb356f01e96853c3be7659ae46ff37e800bb42fe` (v1.0.0, TECHNICALLY_APPROVED, **inalterada**).
- **Escopo:** exclusivamente os bloqueadores do `OPERATIONAL_READINESS_DAY0`.

## Adicionado — `core/operations/`
- `kill_switch.py`: emergency stop global.
  - persistente em SQLite e lido a cada verificação; fail-closed (estado ilegível ou banco ausente bloqueia, sem criar arquivo);
  - só o Owner altera, com motivo obrigatório; histórico em `operational_kill_switch_events` + auditoria (`kill_switch_enabled` / `kill_switch_disabled`);
  - responde `BLOCKED_BY_GLOBAL_KILL_SWITCH`.
- `worker.py`: ciclo do worker (heartbeat, `recover_stuck_jobs`, drenagem sem duplicação, auditoria, kill switch).
- `scheduler.py`: janelas reivindicadas de forma atômica em `scheduler_ticks` (sem duplicação e sem overlap), intervalos configuráveis; tarefas externas viram `SKIPPED_KILL_SWITCH` e a recuperação continua.
- `logging_setup.py`: arquivo rotativo (tamanho + retenção), timestamp, nível, componente, `correlation_id`; redação em duas camadas (filtro + formatador, incluindo tracebacks) dos valores de segredos do ambiente e de padrões (Bearer, token=, password=, api_key=, secret=).
- `cost_guard.py`:
  - registro em `ai_api_usage` (provedor, serviço, operação, tokens, unidades, custo ou `COST_UNKNOWN`, moeda, job/projeto/campanha, sucesso);
  - limites em `ai_api_limits` (por chamada, dia, projeto, campanha, total), definidos só pelo Owner;
  - sem limite, `PAID_PROVIDER_NOT_AUTHORIZED`; estimativa desconhecida, `COST_UNKNOWN`.
- `backup.py`: API de backup do SQLite, nome com timestamp UTC, **nunca sobrescreve**, `.sha256`, `integrity_check`, retenção, `backup_report.jsonl`, `LOCAL_BACKUP_ONLY`.
- `config_check.py`: `DB_PATH` (ausente, temporário ou dentro do código exige `REQUIRES_OWNER_CONFIGURATION`); segredos (placeholder ou fraco rejeitados); credencial do Owner; TLS (`PUBLIC_EXPOSURE_NOT_READY`); cookie Secure por ambiente.
- `readiness.py`: `TECHNICALLY_READY` / `OPERATIONALLY_READY` / `EXTERNAL_INTEGRATION_PENDING`, somente leitura.

## Alterado — produção
- `bridge/execution/navigator_real.py`: kill switch **antes** de claim, aprovação e despacho (cobre retry, fallback e navegador).
- `bridge/execution/platform_adapters.py`: kill switch na chamada direta de API.
- `bridge/execution/webhooks.py`: kill switch na entrega de webhook.
- `navigator/executor.py`: kill switch antes de cada mecanismo (executor legado).
- `navigator/planning.py`: kill switch no `OperationRunner` (sem consumir tentativa) e em `process_advance_jobs`.
- `radar_web/intelligence.py`: kill switch em `process_observation_jobs`.
- `atlas/director.py`: kill switch em `radar_observe_page`.
- `core/agents/runtime/http_generation_provider.py`: kill switch + `CostGuard.authorize` antes de chamada paga.
- `bridge/execution/vault.py`: em produção, chave mestra ausente gera **erro**, nunca chave efêmera silenciosa.
- `api/server.py`: cookie `Secure` por ambiente; `/api/health/operational`.
- `core/models/db.py`: 6 tabelas aditivas; `remove_db_file` desfaz a configuração global que apontava para o banco removido.
- `main.py`: comandos `worker`, `scheduler`, `backup`, `killswitch` (status/activate/deactivate; senha via getpass, sem eco), `readiness`. Logging persistente quando `LOG_DIR` está definido. **Somente os processos `main.py` configuram o banco global** do kill switch e da guarda de custo (`build_app` permanece puro).

## Não feito, por instrução
Nenhuma integração externa conectada; nenhuma publicação, cobrança, anúncio ou gasto de crédito.
