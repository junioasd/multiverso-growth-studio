"""
Worker de teste standalone — reivindica um outcome tardio pendente de
Quality Gate (`OUTCOME_AVAILABLE_PENDING_GATE` -> `GATE_IN_PROGRESS`)
e MORRE ABRUPTAMENTE via `os._exit()` dentro do próprio checker —
simulando um crash real de processo/máquina, nunca uma exceção
tratável. Usado por
`tests/test_microcorrecao_atlas_expansion1_crash_recovery.py` via
`subprocess.run([sys.executable, __file__, db_path, mission_id,
ttl_seconds])`.

MICROCORREÇÃO — ATLAS EXPANSION 1: recuperação de crash real em
GATE_IN_PROGRESS (falha real reproduzida por auditoria independente,
comprovada com um subprocesso real morto via `os._exit()`).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas.advanced_planning import AdvancedPlanner  # noqa: E402
from core.coordination.mission import MissionTracker  # noqa: E402
from core.jobs.queue import JobQueue  # noqa: E402
from atlas.orchestration.graph_executor import GraphExecutor  # noqa: E402


def _runner(task):
    time.sleep(0.2)
    return {"ok": True}


def _crashing_checker(task, outcome):
    # reivindica o gate (o ACQUIRE ja aconteceu antes desta chamada,
    # dentro de execute_plan) e morre ABRUPTAMENTE -- nunca uma
    # excecao tratavel, um crash real de processo.
    os._exit(1)


def main() -> None:
    db_path, mission_id, ttl_seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
    planner = AdvancedPlanner()
    plan = planner.build_plan(
        objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}],
    )
    tracker = MissionTracker(db_path=db_path)
    queue = JobQueue(db_path=db_path)
    executor = GraphExecutor(
        mission_tracker=tracker, job_queue=queue, db_path=db_path, max_workers=4,
        gate_in_progress_ttl_seconds=ttl_seconds,
    )
    executor.execute_plan(
        plan, task_runner=_runner, actor="teste", mission_id=mission_id, timeout_per_task=5.0,
        quality_gate_checker=_crashing_checker,
    )
    print("NUNCA DEVERIA CHEGAR AQUI — o checker deveria ter matado o processo antes")


if __name__ == "__main__":
    main()
