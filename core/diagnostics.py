"""
core.diagnostics
====================
CORREÇÃO (itens 24/25 desta rodada — dump de recursos + shutdown
global): utilitários de diagnóstico para o AMBIENTE DE TESTES/RUNTIME
— nunca usados dentro da lógica de negócio em si. `dump_resources()`
relata o estado de threads/recursos SEM incluir segredos.
`shutdown_all()` é um ponto único e IDEMPOTENTE para encerrar
qualquer worker/scheduler de fundo que um teste possa ter deixado
rodando — nunca usa `os._exit()`/force kill; apenas chama os
`.shutdown()`/`.stop()` já existentes em cada componente.
"""

from __future__ import annotations

import threading


def dump_resources() -> dict:
    """CORREÇÃO (item 24): relata `threading.enumerate()` — nome,
    daemon, alive — sem incluir nenhum dado sensível (só metadados de
    thread). Usado ao final da suíte monolítica para confirmar que
    nenhuma thread relevante ficou órfã."""
    threads = threading.enumerate()
    main = threading.main_thread()
    non_daemon_non_main = [t for t in threads if t is not main and not t.daemon and t.is_alive()]
    return {
        "total_threads": len(threads),
        "threads": [
            {"name": t.name, "daemon": t.daemon, "alive": t.is_alive()}
            for t in threads if t is not main
        ],
        "non_daemon_non_main_alive": [t.name for t in non_daemon_non_main],
        "clean": len(non_daemon_non_main) == 0,
    }


def shutdown_all(*apps) -> dict:
    """CORREÇÃO (item 25): encerramento IDEMPOTENTE de qualquer
    `GrowthStudioApp` (ou objeto com `.shutdown()`) passado —
    projetado para uso em fixtures/finalizers de teste (item 26) que
    querem garantir limpeza mesmo quando não sabem exatamente quais
    apps foram criados. Nunca levanta exceção — um app já encerrado ou
    `None` é simplesmente ignorado; o resultado relata o que foi
    efetivamente encerrado, para auditoria/depuração."""
    results = []
    for app in apps:
        if app is None:
            continue
        try:
            app.shutdown()
            results.append({"app": repr(app), "outcome": "SHUTDOWN"})
        except Exception as exc:  # nunca deixa uma falha de shutdown mascarar as outras
            results.append({"app": repr(app), "outcome": "ERROR", "error": str(exc)})
    return {"shutdown_results": results, "resources_after": dump_resources()}



# ----------------------------------------------------------------------
# COMANDO 6/6 — ENTERPRISE HEALTH (readiness real). `/api/health` era um
# "ok" fixo: rodando ≠ saudável. Lê SOMENTE o que já é persistido e
# devolve CONTAGENS e nomes de módulos — nunca payloads nem segredos.
# ----------------------------------------------------------------------
_TRANSIENT_MARKERS = ("429", "503", "408", "timeout", "timed out", "rate limit", "falha real de rede")


def enterprise_health(db_path: str, *, now: float | None = None, stuck_job_seconds: float = 300.0,
                      stale_payment_seconds: float = 86400.0) -> dict:
    import json as _json
    import sqlite3 as _sqlite3
    import time as _time

    from core.models import db as db_module

    import os as _os

    now = _time.time() if now is None else now
    checks, problems = {}, []

    def issue(level, module, msg):
        problems.append({"level": level, "module": module, "detail": msg})

    # A verificação de saúde NUNCA cria banco/diretório (efeito colateral)
    # nem quebra: banco ausente ou schema incompleto => UNHEALTHY.
    if not db_path or not _os.path.isfile(db_path):
        checks["database"] = {"exists": False, "writable": False}
        issue("UNHEALTHY", "database", "arquivo de banco inexistente")
        return {"status": "UNHEALTHY", "checked_at": now, "checks": checks, "problems": problems}
    required = ("jobs", "owner_approval_requests", "navigator_execution_claims", "navigator_operation_runs",
                "graph_executor_task_leases", "execution_observability", "commercial_orders", "finance_entries")
    try:
        with db_module.connect(db_path) as conn:
            present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except (_sqlite3.Error, OSError) as exc:
        present = set()
        issue("UNHEALTHY", "database", f"banco ilegível ({type(exc).__name__})")
    missing = [t for t in required if t not in present]
    if missing:
        checks["database"] = {"exists": True, "missing_tables": missing}
        if not problems:
            issue("UNHEALTHY", "database", f"schema incompleto: {missing}")
        return {"status": "UNHEALTHY", "checked_at": now, "checks": checks, "problems": problems}

    try:
        with db_module.connect(db_path) as conn:
            ok = conn.execute("PRAGMA quick_check").fetchone()[0]
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _mgs_readiness_probe (x INTEGER)")
            conn.execute("INSERT INTO _mgs_readiness_probe VALUES (1)")
            conn.execute("DELETE FROM _mgs_readiness_probe")
        checks["database"] = {"quick_check": ok, "writable": True}
        if ok != "ok":
            issue("UNHEALTHY", "database", f"integridade: {ok}")
    except (_sqlite3.Error, OSError) as exc:
        checks["database"] = {"writable": False, "error": type(exc).__name__}
        issue("UNHEALTHY", "database", f"banco indisponível ({type(exc).__name__})")
        return {"status": "UNHEALTHY", "checked_at": now, "checks": checks, "problems": problems}

    def rows(sql, params=()):
        with db_module.connect(db_path) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    jobs = rows("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
    stuck = rows("SELECT COUNT(*) AS n FROM jobs WHERE state = 'RUNNING' AND updated_at < ?", (now - stuck_job_seconds,))[0]["n"]
    by_state = {r["state"]: r["n"] for r in jobs}
    checks["jobs"] = {"by_state": by_state, "backlog": by_state.get("QUEUED", 0) + by_state.get("RETRYING", 0),
                      "stuck_running": stuck, "failed": by_state.get("FAILED", 0)}
    if stuck:
        issue("DEGRADED", "jobs/worker", f"{stuck} job(s) RUNNING há mais de {stuck_job_seconds:.0f}s — worker travado/morto")
    pending = rows("SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM owner_approval_requests "
                   "WHERE status = 'PENDING_OWNER_APPROVAL'")[0]
    checks["owner_approvals"] = {"pending": pending["n"],
                                 "oldest_pending_age_s": round(now - pending["oldest"], 1) if pending["oldest"] else None}
    claims = rows("SELECT status, COUNT(*) AS n FROM navigator_execution_claims GROUP BY status")
    unknown = {r["status"]: r["n"] for r in claims}.get("UNKNOWN_OUTCOME", 0)
    checks["execution_claims"] = {r["status"]: r["n"] for r in claims}
    if unknown:
        issue("DEGRADED", "execution_hub", f"{unknown} ação(ões) externa(s) com resultado DESCONHECIDO — reconciliação do Owner")
    runs = rows("SELECT status, steps_state_json FROM navigator_operation_runs")
    step_states = {}
    for r in runs:
        for st in _json.loads(r["steps_state_json"]).values():
            step_states[st["status"]] = step_states.get(st["status"], 0) + 1
    checks["operation_runs"] = {"plans": len(runs), "steps_by_status": step_states}
    if step_states.get("UNKNOWN"):
        issue("DEGRADED", "navigator", f"{step_states['UNKNOWN']} passo(s) de plano com resultado DESCONHECIDO")
    leases = rows("SELECT status, COUNT(*) AS n FROM graph_executor_task_leases GROUP BY status")
    checks["task_leases"] = {r["status"]: r["n"] for r in leases}
    if checks["task_leases"].get("UNKNOWN_OUTCOME"):
        issue("DEGRADED", "orchestration", f"{checks['task_leases']['UNKNOWN_OUTCOME']} tarefa(s) UNKNOWN_OUTCOME")
    integ = {}
    for r in rows("SELECT platform, success, error_reason, executed_at FROM execution_observability ORDER BY executed_at"):
        d = integ.setdefault(r["platform"], {"executions": 0, "failures": 0, "transient": 0, "permanent": 0,
                                             "last_at": None, "last_success": None})
        d["executions"] += 1
        d["last_at"], d["last_success"] = r["executed_at"], bool(r["success"])
        if not r["success"]:
            d["failures"] += 1
            reason = (r["error_reason"] or "").lower()
            d["transient" if any(m in reason for m in _TRANSIENT_MARKERS) else "permanent"] += 1
    checks["integrations"] = integ
    for platform, d in integ.items():
        if d["last_success"] is False:
            issue("DEGRADED", f"integration:{platform}",
                  f"última execução falhou ({d['transient']} transitória(s), {d['permanent']} permanente(s))")
    stale_pay = rows("SELECT COUNT(*) AS n FROM commercial_orders WHERE status = 'PAYMENT_PENDING' AND updated_at < ?",
                     (now - stale_payment_seconds,))[0]["n"]
    orders = rows("SELECT status, COUNT(*) AS n FROM commercial_orders GROUP BY status")
    checks["orders"] = {"by_status": {r["status"]: r["n"] for r in orders}, "stale_payment_pending": stale_pay}
    if stale_pay:
        issue("DEGRADED", "commercial", f"{stale_pay} pedido(s) aguardando pagamento há mais de {stale_payment_seconds:.0f}s")
    cost = rows("SELECT entry_type, SUM(amount) AS total FROM finance_entries "
                "WHERE entry_type IN ('expense','realized_cost','commission','refund') GROUP BY entry_type")
    checks["operational_cost_recorded"] = {r["entry_type"]: round(r["total"], 2) for r in cost}
    try:
        from atlas.digital_twin import DigitalTwin, ReadingStatus

        twin = DigitalTwin(db_path=db_path)
        conflicts = [f"{k[0]}:{k[1]}.{k[2]}" for k in twin.keys()
                     if twin.reading(*k, now=now).status == ReadingStatus.CONFLICT]
        checks["digital_twin"] = {"keys": len(twin.keys()), "conflicts": len(conflicts)}
        if conflicts:
            issue("DEGRADED", "digital_twin", f"{len(conflicts)} atributo(s) em CONFLITO entre fontes")
    except Exception as exc:  # nunca derruba o relatório; registra o módulo com defeito
        issue("DEGRADED", "digital_twin", f"leitura falhou ({type(exc).__name__})")
    status = "UNHEALTHY" if any(p["level"] == "UNHEALTHY" for p in problems) else \
        ("DEGRADED" if problems else "HEALTHY")
    return {"status": status, "checked_at": now, "checks": checks, "problems": problems}
