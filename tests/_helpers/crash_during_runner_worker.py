"""
Worker de teste standalone — MORRE ABRUPTAMENTE via os._exit() DENTRO
do proprio task_runner, ANTES dele conseguir retornar qualquer coisa
-- simula um crash real durante a execucao do runner, onde o desfecho
do efeito externo e genuinamente DESCONHECIDO (pode ou nao ter
ocorrido).

MICROCORREÇÃO — ATLAS EXPANSION 1: TTL para o lease IN_PROGRESS --
apos expirar, transiciona para UNKNOWN_OUTCOME, NUNCA retenta
automaticamente.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas.advanced_planning import AdvancedPlanner  # noqa: E402
from core.coordination.mission import MissionTracker  # noqa: E402
from core.jobs.queue import JobQueue  # noqa: E402
from atlas.orchestration.graph_executor import GraphExecutor  # noqa: E402


def _crashing_runner(task):
    time.sleep(0.05)
    os._exit(1)  # morre ANTES de retornar -- desfecho genuinamente desconhecido


def main() -> None:
    db_path, mission_id, ttl_seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
    planner = AdvancedPlanner()
    plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])
    tracker = MissionTracker(db_path=db_path)
    queue = JobQueue(db_path=db_path)
    executor = GraphExecutor(
        mission_tracker=tracker, job_queue=queue, db_path=db_path, max_workers=4,
        gate_in_progress_ttl_seconds=ttl_seconds,
    )
    # timeout generoso -- o processo morre por conta propria, nunca por timeout_per_task
    executor.execute_plan(
        plan, task_runner=_crashing_runner, actor="teste", mission_id=mission_id, timeout_per_task=10.0,
    )
    print("NUNCA DEVERIA CHEGAR AQUI")


if __name__ == "__main__":
    main()
