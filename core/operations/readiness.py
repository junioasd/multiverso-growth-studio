"""
core.operations.readiness — READINESS OPERACIONAL (v1.0.1).
Separa: TECHNICALLY_READY (o sistema funciona: enterprise_health não UNHEALTHY),
OPERATIONALLY_READY (configuração/processos de operação presentes) e
EXTERNAL_INTEGRATION_PENDING (credenciais de integrações ainda ausentes — NÃO é
bug interno). Somente leitura: não cria banco, não altera estado.
"""
from __future__ import annotations

import datetime
import json
import os
import time

from core.operations import config_check, kill_switch, logging_setup

_PROVIDER_ENV = ("YOUTUBE_API_KEY", "YOUTUBE_OAUTH_CLIENT_ID", "TIKTOK_API_KEY", "META_API_KEY", "HOTMART_CLIENT_ID",
                 "MONETIZZE_API_KEY", "EDUZZ_API_KEY", "KIWIFY_API_KEY", "BRAIP_API_KEY", "PAYMENT_PROVIDER_KEY",
                 "ANALYTICS_PROVIDER_KEY", "VIDEO_PROVIDER_SEEDANCE_KEY", "VIDEO_PROVIDER_KLING_KEY",
                 "VIDEO_PROVIDER_VEO_KEY", "VIDEO_PROVIDER_SORA_KEY")


def operational_readiness(db_path: str, *, env=None, now: float | None = None, worker_max_age: float = 180.0,
                          scheduler_max_age: float = 180.0, backup_max_age: float = 36 * 3600.0) -> dict:
    from core.diagnostics import enterprise_health

    env = env or os.environ
    now = time.time() if now is None else now
    tech = enterprise_health(db_path, now=now)
    ops = {}
    if tech["status"] == "UNHEALTHY":
        ops["database"] = {"status": "NOT_READY", "detail": "banco indisponível/incompleto"}
    else:
        ks = kill_switch.state(db_path)
        ops["kill_switch"] = {"status": "READY" if ks["state"] in ("ENABLED", "DISABLED") else "NOT_READY",
                              "detail": f"estado={ks['state']}"}
        from core.operations.scheduler import Scheduler
        from core.operations.worker import last_heartbeat
        hb = last_heartbeat(db_path)
        ops["worker"] = {"status": "READY" if hb and now - hb <= worker_max_age else "NOT_RUNNING",
                         "detail": f"último heartbeat há {round(now - hb, 1)}s" if hb else "worker nunca executou"}
        sl = Scheduler(db_path).last_activity()
        ops["scheduler"] = {"status": "READY" if sl and now - sl <= scheduler_max_age else "NOT_RUNNING",
                            "detail": f"última janela há {round(now - sl, 1)}s" if sl else "scheduler nunca executou"}
        from core.operations.cost_guard import CostGuard
        from core.models import db as db_module
        with db_module.connect(db_path) as conn:
            limited = {r[0] for r in conn.execute("SELECT DISTINCT provider FROM ai_api_limits")}
        ops["cost_guard"] = {"status": "READY", "detail": f"ativo; provedores com limite: {sorted(limited) or 'nenhum'} "
                                                          f"(sem limite => PAID_PROVIDER_NOT_AUTHORIZED)"}
    log_dir = env.get("LOG_DIR")
    ops["logging"] = {"status": "READY" if logging_setup.is_configured() or (log_dir and os.path.isdir(log_dir)) else "NOT_CONFIGURED",
                      "detail": f"LOG_DIR={log_dir}" if log_dir else "LOG_DIR não definido"}
    bdir = env.get("BACKUP_DIR")
    last = None
    if bdir and os.path.isfile(os.path.join(bdir, "backup_report.jsonl")):
        with open(os.path.join(bdir, "backup_report.jsonl"), encoding="utf-8") as fh:
            lines = fh.read().strip().splitlines()
        last = json.loads(lines[-1]) if lines else None
    if not bdir:
        ops["backup"] = {"status": "NOT_CONFIGURED", "detail": "BACKUP_DIR não definido"}
    elif not last or last.get("status") != "SUCCESS":
        ops["backup"] = {"status": "NOT_READY", "detail": "nenhum backup bem-sucedido registrado"}
    else:
        # A existência de um backup antigo não comprova recuperação atual.
        # Usa o timestamp UTC gravado pelo módulo de backup (sem alterar o banco).
        try:
            backup_at = datetime.datetime.strptime(
                str(last.get("started_at", "")), "%Y%m%dT%H%M%S%fZ"
            ).replace(tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            backup_at = None
        age = now - backup_at if backup_at is not None else None
        if age is None or age < -300:
            ops["backup"] = {"status": "NOT_READY", "detail": "timestamp do último backup inválido"}
        elif age > backup_max_age:
            ops["backup"] = {"status": "NOT_READY",
                             "detail": f"último backup há {round(age / 3600, 1)}h (limite {round(backup_max_age / 3600, 1)}h)"}
        else:
            ops["backup"] = {"status": "READY",
                             "detail": f"último backup há {round(max(age, 0) / 3600, 2)}h ({last.get('mode', 'UNKNOWN')})"}
    cfg = config_check.production_config_report(env)
    for k in ("db_path", "vault_master_key", "session_secret", "owner_credential"):
        ops[k] = cfg[k]
    ops["tls"] = cfg["tls"]
    required = [k for k in ops if k != "tls"]
    operationally_ready = all(ops[k]["status"] == "READY" for k in required)
    pending = [k for k in _PROVIDER_ENV if not env.get(k)]
    return {"checked_at": now, "TECHNICALLY_READY": tech["status"] != "UNHEALTHY",
            "OPERATIONALLY_READY": operationally_ready,
            "PUBLIC_EXPOSURE_READY": cfg["tls"]["status"] == "DECLARED_NOT_VALIDATED" and cfg["cookie_secure"],
            "EXTERNAL_INTEGRATION_PENDING": bool(pending), "pending_integrations_env": pending,
            "operational_checks": ops, "technical_health": {"status": tech["status"], "problems": tech["problems"]}}
