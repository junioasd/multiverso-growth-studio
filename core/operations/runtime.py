"""
core.operations.runtime — configuração OPERACIONAL única (v1.0.5).

Auditoria de readiness 1.0.4 (bloqueador 1): `api/server.py` era um segundo
entrypoint que subia o servidor SEM kill switch/CostGuard configurados.
Todo entrypoint de produção (`main.py` e `api/server.py::run_server`) chama
`configure_operational(app)` — o teste estático exige isso.
"""
from __future__ import annotations


def configure_operational(app) -> None:
    from core.operations import cost_guard, kill_switch

    kill_switch.set_operational_runtime(True)      # sem configuração => bloqueio (fail-closed)
    kill_switch.configure(app.atlas.db_path)
    cost_guard.configure(app.atlas.db_path)
