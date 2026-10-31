"""
Worker de teste standalone — executa task_runner com SUCESSO (sem
Quality Gate); o lease e resolvido para COMPLETED com o outcome real
persistido; o processo MORRE ABRUPTAMENTE via os._exit() logo em
seguida, ANTES de Mission/Job serem sincronizados -- simula um crash
real exatamente na janela entre a resolucao do lease e a
sincronizacao de Mission/Job no caminho SEM Quality Gate.

MICROCORREÇÃO — ATLAS EXPANSION 1: persistencia do outcome PRIMEIRO
(lease), sincronizacao de Mission/Job DEPOIS -- um crash nesta janela
nunca perde o outcome, so atrasa a sincronizacao (reconciliavel numa
retomada via _finalize_cached_completed()).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas.advanced_planning import AdvancedPlanner  # noqa: E402
from core.coordination.mission import MissionTracker  # noqa: E402
from core.jobs.queue import JobQueue  # noqa: E402
from atlas.orchestration.graph_executor import GraphExecutor  # noqa: E402


def _runner(task):
    return {"ok": True, "efeito_aplicado": True}


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

    # intercepta o PRIMEIRO passo apos o lease ja ter sido resolvido
    # para COMPLETED (o outcome real ja esta persistido no banco) --
    # mata o processo ANTES de Mission/Job serem sincronizados. Isso
    # testa exatamente a garantia da correção: mesmo com Mission/Job
    # nunca sincronizados por este processo, o outcome persistido no
    # lease é suficiente para uma retomada reconciliar tudo sem
    # reexecutar o runner.
    def _die_before_mission_sync(*args, **kwargs):
        os._exit(1)

    executor._advance_mission_to_completed_and_verify = _die_before_mission_sync

    executor.execute_plan(
        plan, task_runner=_runner, actor="teste", mission_id=mission_id, timeout_per_task=5.0,
    )
    print("NUNCA DEVERIA CHEGAR AQUI")


if __name__ == "__main__":
    main()
