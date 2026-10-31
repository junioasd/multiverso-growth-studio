"""
core.operations.kill_switch — EMERGENCY STOP GLOBAL (v1.0.1-OPERATIONAL-CANDIDATE).

Estado PERSISTENTE no SQLite (sobrevive a restart, compartilhado entre
processos serve/worker/scheduler). Lido A CADA verificação — nunca em cache.
- ENABLED  => kill switch ACIONADO: `external_execution_allowed() == False`.
- DISABLED => execução externa permitida (sujeita às demais travas).
Só o OWNER ativa/desativa, com motivo obrigatório; cada mudança é gravada em
`operational_kill_switch_events` e na trilha de auditoria.
FAIL-CLOSED: se o estado não puder ser lido num banco configurado, a execução
externa é BLOQUEADA. Sem banco configurado (uso como biblioteca/testes), não há
estado a consultar — a readiness operacional marca isso como NÃO pronto.
"""
from __future__ import annotations

import time

from core.models import db as db_module

BLOCK_CODE = "BLOCKED_BY_GLOBAL_KILL_SWITCH"
_CONFIGURED_DB: str | None = None
# v1.0.4 (auditoria de readiness 1.0.3, bloqueador 1): processos operacionais
# (`main.py serve/worker/scheduler/...`) se declaram OPERACIONAIS; neles — e em
# APP_ENV=production — a ausência de configuração BLOQUEIA o egress.
_OPERATIONAL_RUNTIME = False


def set_operational_runtime(flag: bool = True) -> None:
    global _OPERATIONAL_RUNTIME
    _OPERATIONAL_RUNTIME = bool(flag)


def fail_closed_enforced() -> bool:
    """True em runtime operacional ou produção: nada de modo permissivo."""
    from core.operations.config_check import is_production

    return _OPERATIONAL_RUNTIME or is_production()


class KillSwitchActive(RuntimeError):
    code = BLOCK_CODE


def configure(db_path: str | None) -> None:
    global _CONFIGURED_DB
    _CONFIGURED_DB = db_path


def configured_db() -> str | None:
    return _CONFIGURED_DB


def state(db_path: str | None = None) -> dict:
    path = db_path or _CONFIGURED_DB
    if path is None:
        if fail_closed_enforced():
            return {"state": "KILL_SWITCH_NOT_CONFIGURED", "external_execution_allowed": False,
                    "reason": "KILL_SWITCH_NOT_CONFIGURED: runtime operacional/produção sem estado de kill switch "
                              "configurado — bloqueio preventivo (nunca autorização implícita)"}
        # modo BIBLIOTECA/DESENVOLVIMENTO explícito: fora de produção e fora de
        # processo operacional (uso como biblioteca e suíte de testes).
        return {"state": "NOT_CONFIGURED_LIBRARY_MODE", "external_execution_allowed": True,
                "reason": "modo biblioteca/desenvolvimento (APP_ENV != production, fora de processo operacional)"}
    import os

    if not os.path.isfile(path):   # nunca cria banco ao consultar; ausência = fail-closed
        return {"state": "UNREADABLE", "external_execution_allowed": False,
                "reason": "banco do kill switch inexistente — bloqueio preventivo"}
    try:
        with db_module.connect(path) as conn:
            row = conn.execute("SELECT state, reason, actor, changed_at FROM operational_kill_switch WHERE id = 1").fetchone()
    except Exception as exc:  # fail-closed
        return {"state": "UNREADABLE", "external_execution_allowed": False,
                "reason": f"estado do kill switch ilegível ({type(exc).__name__}) — bloqueio preventivo"}
    # v1.0.2 (auditoria de readiness, bloqueador 4): a linha de estado é criada
    # EXPLICITAMENTE com a tabela (init_db). Linha ausente ou estado inválido
    # significa estado PERDIDO/ADULTERADO — nunca autorização implícita.
    if row is None:
        return {"state": "UNREADABLE", "external_execution_allowed": False,
                "reason": "linha de estado do kill switch AUSENTE — bloqueio preventivo; o Owner deve redefinir o estado"}
    if row["state"] not in ("ENABLED", "DISABLED"):
        return {"state": "UNREADABLE", "external_execution_allowed": False,
                "reason": f"estado inválido {row['state']!r} — bloqueio preventivo"}
    return {"state": row["state"], "external_execution_allowed": row["state"] != "ENABLED", "reason": row["reason"],
            "actor": row["actor"], "changed_at": row["changed_at"]}


def external_execution_allowed(db_path: str | None = None) -> bool:
    return state(db_path)["external_execution_allowed"]


def block_reason(db_path: str | None = None) -> str | None:
    s = state(db_path)
    if s["external_execution_allowed"]:
        return None
    return f"{BLOCK_CODE}: execução externa bloqueada ({s['state']}; motivo: {s.get('reason')})"


def _change(db_path: str, new_state: str, *, principal, reason: str, audit=None) -> dict:
    from core.security.rbac import Role

    if getattr(principal, "role", None) != Role.OWNER:
        raise PermissionError("somente o OWNER autorizado altera o kill switch")
    if not reason or not reason.strip():
        raise ValueError("motivo obrigatório para alterar o kill switch")
    now = time.time()
    actor = getattr(principal, "subject", "?")
    with db_module.connect(db_path) as conn:
        conn.execute("INSERT INTO operational_kill_switch (id, state, reason, actor, changed_at) VALUES (1, ?, ?, ?, ?) "
                     "ON CONFLICT(id) DO UPDATE SET state=excluded.state, reason=excluded.reason, actor=excluded.actor, "
                     "changed_at=excluded.changed_at", (new_state, reason, actor, now))
        conn.execute("INSERT INTO operational_kill_switch_events (state, reason, actor, changed_at) VALUES (?, ?, ?, ?)",
                     (new_state, reason, actor, now))
    if audit is not None:
        audit.record(actor=actor, action=f"kill_switch_{new_state.lower()}", resource="global_kill_switch",
                     outcome="SUCCESS", details={"state": new_state, "reason": reason, "at": now})
    return state(db_path)


def activate(db_path: str, *, principal, reason: str, audit=None) -> dict:
    return _change(db_path, "ENABLED", principal=principal, reason=reason, audit=audit)


def deactivate(db_path: str, *, principal, reason: str, audit=None) -> dict:
    return _change(db_path, "DISABLED", principal=principal, reason=reason, audit=audit)


def history(db_path: str) -> list:
    with db_module.connect(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM operational_kill_switch_events ORDER BY event_id").fetchall()]
