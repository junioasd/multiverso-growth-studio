"""
Worker de teste standalone — executa task_runner (dentro do timeout,
caminho FRESCO, nunca tardio) e MORRE ABRUPTAMENTE via os._exit()
DENTRO do proprio Quality Gate checker -- simula um crash real
durante a avaliacao do gate de um outcome RECEM-OBTIDO, nunca um
outcome tardio.

MICROCORREÇÃO — ATLAS EXPANSION 1: unificacao do caminho fresco com
o mecanismo duravel ja validado (persistencia do outcome ANTES do
Quality Gate).
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
    time.sleep(0.05)
    return {"ok": True}


def _crashing_checker(task, outcome):
    os._exit(1)


def main() -> None:
    db_path, mission_id, ttl_seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
    max_retries = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    planner = AdvancedPlanner()
    plan = planner.build_plan(
        objective="x",
        tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": max_retries}],
    )
    tracker = MissionTracker(db_path=db_path)
    queue = JobQueue(db_path=db_path)
    executor = GraphExecutor(
        mission_tracker=tracker, job_queue=queue, db_path=db_path, max_workers=4,
        gate_in_progress_ttl_seconds=ttl_seconds,
    )
    # timeout GENEROSO -- o runner conclui DENTRO do timeout (caminho
    # fresco), o crash acontece dentro do checker, nao por timeout.
    executor.execute_plan(
        plan, task_runner=_runner, actor="teste", mission_id=mission_id, timeout_per_task=5.0,
        quality_gate_checker=_crashing_checker,
    )
    print("NUNCA DEVERIA CHEGAR AQUI")


if __name__ == "__main__":
    main()
