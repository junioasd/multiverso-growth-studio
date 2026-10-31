"""
main.py
========
Ponto de entrada de linha de comando do MULTIVERSO GROWTH STUDIO.
Uso local/dev apenas.

MICROCORREÇÃO 2 (ACESSO OWNER): adicionado `set-owner-password`, que
completa o fluxo local de configuração segura de credenciais — gera o
hash PBKDF2 da senha (nunca a senha em si) para colar em `.env` como
`OWNER_PASSWORD_HASH`. A senha nunca é passada por argumento de linha
de comando por padrão (evita ficar no histórico do shell); use
`getpass` interativo, ou `--password` apenas se aceitar esse risco
conscientemente (ex: scripts de CI local).
"""

from __future__ import annotations

import argparse
import getpass
import sys

from api.bootstrap import build_app
from api.server import run_server
from core.config import SETTINGS
from core.security.auth import hash_password


def cmd_serve(args: argparse.Namespace) -> None:
    _setup_logging("serve")
    # CORREÇÃO (item 1 — RADAR WEB COMERCIAL: autostart real no
    # runtime). O worker SEMPRE sobe junto com a aplicação (mesma
    # disciplina do RADAR DE OPORTUNIDADES) — infraestrutura pronta
    # desde o início. O AGENDAMENTO (se scans de verdade acontecem)
    # depende de `SETTINGS.radar_web_enabled` (env `RADAR_WEB_ENABLED`,
    # persistido/configurável) — nunca depende de uma chamada manual
    # posterior nem de um teste habilitar o scheduler.
    app = build_app(start_radar_worker=True, start_radar_web_worker=True)

    app.atlas_gateway_worker.start()
    _configure_operational(app)
    if SETTINGS.radar_web_enabled:
        app.atlas.radar_web_scheduler.enable(interval_seconds=SETTINGS.radar_web_interval_seconds)
    server = run_server(host=args.host, port=args.port, app=app)
    print(f"MULTIVERSO GROWTH STUDIO API em http://{args.host}:{args.port}")
    print(f"Login: http://{args.host}:{args.port}/login")
    print(f"Dashboard: http://{args.host}:{args.port}/dashboard")
    print(f"Persistência: {app.db_path or '(em memória — persistent=False)'}")
    print("Radar de oportunidades (worker de fundo): ativo")
    radar_web_status = (
        f"ativo — scans a cada {SETTINGS.radar_web_interval_seconds:.0f}s"
        if SETTINGS.radar_web_enabled else "infraestrutura ativa — agendamento DESABILITADO (RADAR_WEB_ENABLED=false)"
    )
    print(f"Radar Web Comercial (worker de fundo): {radar_web_status}")
    previous_sigint = None
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando...")
        import signal
        previous_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        server.server_close()
    finally:
        try:
            app.shutdown()
        finally:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)


def cmd_health(args: argparse.Namespace) -> None:
    app = build_app()
    _configure_operational(app)      # v1.0.5: todo processo de produção é operacional (fail-closed)
    print("OK - fundação carregada com sucesso.")
    print(f"Owner email (config): {app.auth.owner_email}")
    print(f"Owner password configurado: {'sim' if app.auth.stored_password_hash else 'NÃO — login ficará indisponível'}")
    print(f"Persistência: {app.db_path or '(em memória)'}")


def cmd_set_owner_password(args: argparse.Namespace) -> None:
    password = args.password or getpass.getpass("Nova senha do Owner (mín. 8 caracteres): ")
    try:
        pw_hash = hash_password(password)
    except ValueError as exc:
        print(f"Erro: {exc}")
        return
    print("\nAdicione (ou substitua) esta linha no seu .env real:\n")
    print(f"OWNER_PASSWORD_HASH={pw_hash}")
    print("\nNunca cole isso no .env.example nem faça commit do .env real.")


# ----------------------------------------------------------------------
# v1.0.1-OPERATIONAL-CANDIDATE — comandos operacionais EXPLÍCITOS.
# ----------------------------------------------------------------------
def _setup_logging(component: str) -> None:
    import os
    if os.environ.get("LOG_DIR"):
        from core.operations.logging_setup import configure_logging

        configure_logging(log_dir=os.environ["LOG_DIR"], component=component,
                          max_bytes=int(os.environ.get("LOG_MAX_BYTES", 5 * 1024 * 1024)),
                          backup_count=int(os.environ.get("LOG_BACKUP_COUNT", 10)))


def _configure_operational(app) -> None:
    """Delegado à configuração operacional ÚNICA (também usada por api/server.py)."""
    from core.operations.runtime import configure_operational

    configure_operational(app)


def _owner_principal(app):
    import getpass
    import os
    from core.security.auth import principal_from_session

    email = os.environ.get("OWNER_EMAIL")
    if not email:
        raise SystemExit("OWNER_EMAIL não configurado")
    password = getpass.getpass("Senha do Owner (não ecoa): ")
    return principal_from_session(app.auth.current_session(app.auth.login(email, password)))


def _runner(app):
    from core.operations import kill_switch  # noqa: F401
    from navigator.planning import OperationPlanner, OperationRunner

    return OperationRunner(db_path=app.atlas.db_path, planner=OperationPlanner(bridge_hub=app.atlas.bridge,
                           evidence_gate=app.atlas.evidence_gate), navigator=app.atlas.navigator_real,
                           escalation_desk=app.atlas.escalation_desk, actor="WORKER",
                           observability=app.atlas.execution_observability)


def cmd_worker(args: argparse.Namespace) -> None:
    import json
    import logging
    import time
    from core.operations.worker import run_worker_cycle

    _setup_logging("worker")
    app = build_app()
    _configure_operational(app)
    runner = _runner(app)
    log = logging.getLogger("growth_studio.worker")
    log.info("worker iniciado (id=%s, intervalo=%ss)", args.worker_id, args.interval)
    while True:
        try:
            out = run_worker_cycle(db_path=app.atlas.db_path, runner=runner, director=app.atlas, audit=app.facade.audit,
                                   worker_id=args.worker_id, stuck_job_seconds=args.stuck_seconds)
        except Exception:
            log.exception("worker cycle failed (id=%s); preserving failure for supervisor", args.worker_id)
            raise
        print(json.dumps(out, default=str), flush=True)
        if args.once:
            return
        time.sleep(args.interval)


def cmd_scheduler(args: argparse.Namespace) -> None:
    import json
    import time
    from core.operations.scheduler import Scheduler

    _setup_logging("scheduler")
    app = build_app()
    _configure_operational(app)
    sch = Scheduler(app.atlas.db_path, intervals={"recover_stuck_jobs": args.recover_interval,
                                                  "operation_runs": args.runs_interval},
                    stuck_job_seconds=args.stuck_seconds)
    while True:
        print(json.dumps(sch.tick(), default=str), flush=True)
        if args.once:
            return
        time.sleep(args.poll)


def cmd_backup(args: argparse.Namespace) -> None:
    import json
    import os
    from core.config import SETTINGS
    from core.operations.backup import run_backup

    _setup_logging("backup")
    backup_dir = args.backup_dir or os.environ.get("BACKUP_DIR")
    if not backup_dir:
        raise SystemExit("BACKUP_DIR não configurado (use --backup-dir ou a variável BACKUP_DIR)")
    rep = run_backup(db_path=os.environ.get("DB_PATH") or SETTINGS.db_path, backup_dir=backup_dir,
                     retention=args.retention, external_target=os.environ.get("BACKUP_EXTERNAL_TARGET"))
    print(json.dumps(rep, ensure_ascii=False))
    if rep["status"] != "SUCCESS":
        raise SystemExit(1)


def cmd_killswitch(args: argparse.Namespace) -> None:
    import json
    from core.operations import kill_switch

    app = build_app()
    _configure_operational(app)
    if args.action == "status":
        print(json.dumps(kill_switch.state(app.atlas.db_path), ensure_ascii=False, default=str))
        return
    principal = _owner_principal(app)
    fn = kill_switch.activate if args.action == "activate" else kill_switch.deactivate
    print(json.dumps(fn(app.atlas.db_path, principal=principal, reason=args.reason, audit=app.facade.audit),
                     ensure_ascii=False, default=str))


def cmd_readiness(args: argparse.Namespace) -> None:
    import json
    from core.operations.readiness import operational_readiness

    app = build_app()
    _configure_operational(app)
    print(json.dumps(operational_readiness(app.atlas.db_path), ensure_ascii=False, indent=1, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(prog="mgs", description="MULTIVERSO GROWTH STUDIO")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="Inicia a API + páginas locais (/login, /dashboard)")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.set_defaults(func=cmd_serve)

    w = sub.add_parser("worker", help="Worker EXPLÍCITO da fila (drena jobs, recupera travados, respeita kill switch)")
    w.add_argument("--once", action="store_true"); w.add_argument("--interval", type=float, default=5.0)
    w.add_argument("--stuck-seconds", type=float, default=300.0); w.add_argument("--worker-id", default="worker-1")
    w.set_defaults(func=cmd_worker)
    sc = sub.add_parser("scheduler", help="Scheduler EXPLÍCITO (janelas sem duplicação, respeita kill switch)")
    sc.add_argument("--once", action="store_true"); sc.add_argument("--poll", type=float, default=10.0)
    sc.add_argument("--recover-interval", type=float, default=60.0); sc.add_argument("--runs-interval", type=float, default=30.0)
    sc.add_argument("--stuck-seconds", type=float, default=300.0)
    sc.set_defaults(func=cmd_scheduler)
    b = sub.add_parser("backup", help="Backup consistente do SQLite com hash, integridade e retenção")
    b.add_argument("--backup-dir"); b.add_argument("--retention", type=int, default=14)
    b.set_defaults(func=cmd_backup)
    k = sub.add_parser("killswitch", help="Emergency stop global (status | activate | deactivate — só Owner)")
    k.add_argument("action", choices=["status", "activate", "deactivate"]); k.add_argument("--reason", default="")
    k.set_defaults(func=cmd_killswitch)
    rd = sub.add_parser("readiness", help="Readiness operacional (técnico × operacional × integrações pendentes)")
    rd.set_defaults(func=cmd_readiness)

    health_parser = sub.add_parser("health", help="Verifica se a fundação carrega corretamente")
    health_parser.set_defaults(func=cmd_health)

    setpw_parser = sub.add_parser(
        "set-owner-password", help="Gera o hash de senha do Owner para colocar no .env (fluxo de configuração segura)"
    )
    setpw_parser.add_argument("--password", default=None, help="Evite em produção — prefira o prompt interativo")
    setpw_parser.set_defaults(func=cmd_set_owner_password)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
