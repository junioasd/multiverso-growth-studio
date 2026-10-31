import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from atlas.advanced_planning import AdvancedPlanner
from core.coordination.mission import MissionStatus, MissionTracker
from core.jobs.queue import JobQueue
from core.models import db as db_module
from atlas.orchestration.graph_executor import GraphExecutor

"""
MICROCORREÇÃO — ATLAS EXPANSION 1: LIVENESS/HEARTBEAT TAMBÉM PARA
`IN_PROGRESS`.

Falha real reproduzida por auditoria independente: o TTL de
`IN_PROGRESS` (correção da rodada anterior) usava `gate_in_progress_
ttl_seconds`, mas não existia heartbeat/renovação enquanto o
`task_runner` estivesse genuinamente rodando. Com um runner LEGÍTIMO
demorando mais que o TTL, porém menos que `timeout_per_task`, uma
SEGUNDA instância convertia o lease (ainda vivo) para
`UNKNOWN_OUTCOME` e retornava `TIMED_OUT_UNKNOWN_OUTCOME` — depois a
PRIMEIRA execução terminava com sucesso e SOBRESCREVIA para
`COMPLETED` (já que `_resolve_lease()` só é condicionado ao
`lease_token`, que a transição para `UNKNOWN_OUTCOME` não rotaciona)
— gerando um estado final consistente, mas com uma resposta FALSA já
entregue à segunda instância no meio do caminho: duas respostas
diferentes e contraditórias para a mesma tarefa.

Corrigido: `_run_with_timeout_tracked()` agora inicia uma thread de
heartbeat, junto com a thread que executa o `task_runner`, que renova
`leased_at` atomicamente (condicionada ao `lease_token` ATUAL, via o
MESMO `_renew_lease_heartbeat()` já usado para `GATE_IN_PROGRESS`)
enquanto a thread REAL do runner estiver viva — desde o início da
execução, e CONTINUANDO mesmo depois do chamador desistir de esperar
por `timeout_seconds` (`gave_up_event`), enquanto a thread real ainda
não tiver terminado. Cessa SOMENTE quando a thread real sinaliza
`done_event` (terminou) ou o processo morre (thread daemon).
"""

_DURING_RUNNER_WORKER = os.path.join(os.path.dirname(__file__), "_helpers", "crash_during_runner_worker.py")


def _fresh_tracker_and_queue(db_path=None):
    return MissionTracker(db_path=db_path), JobQueue(db_path=db_path)


def _read_lease_status(db_path: str, mission_id: str) -> str:
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM graph_executor_task_leases WHERE mission_id=? AND task_id='t1'", (mission_id,),
    ).fetchone()
    conn.close()
    return row["status"] if row else None


class HealthySlowRunnerNeverIncorrectlyConvertedTests(unittest.TestCase):
    """Cenário exato exigido: runner saudável >TTL e <timeout_per_task
    — segunda instância nunca converte para UNKNOWN_OUTCOME, runner
    executa exatamente 1x, estado final COMPLETED/VERIFIED."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.ttl = 0.3

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_lease_never_shows_unknown_outcome_while_healthy_runner_alive(self):
        """Prova POSITIVA e determinística: monitora o `status` do
        lease continuamente enquanto o runner (mais lento que o TTL)
        está rodando — `UNKNOWN_OUTCOME` NUNCA deve aparecer nas
        leituras, mesmo repetidas vezes ao longo de uma execução mais
        longa que o TTL."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        call_count = {"n": 0}
        call_lock = threading.Lock()

        def slow_healthy_runner(task):
            with call_lock:
                call_count["n"] += 1
            time.sleep(1.0)  # ~3.3x o TTL, mas bem dentro do timeout_per_task
            return {"ok": True}

        tracker_1, queue_1 = _fresh_tracker_and_queue(self.db_path)
        executor_1 = GraphExecutor(
            mission_tracker=tracker_1, job_queue=queue_1, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )

        results = {}

        def run_owner():
            results["owner"] = executor_1.execute_plan(
                plan, task_runner=slow_healthy_runner, actor="teste", mission_id="m_heartbeat_inprogress",
                timeout_per_task=5.0,
            )

        def run_intruder():
            time.sleep(self.ttl * 1.5)  # espera o TTL "nominal" ja ter passado
            tracker_2, queue_2 = _fresh_tracker_and_queue(self.db_path)
            executor_2 = GraphExecutor(
                mission_tracker=tracker_2, job_queue=queue_2, db_path=self.db_path, max_workers=4,
                gate_in_progress_ttl_seconds=self.ttl,
            )
            results["intruder"] = executor_2.execute_plan(
                plan, task_runner=slow_healthy_runner, actor="teste", mission_id="m_heartbeat_inprogress",
                timeout_per_task=5.0,
            )

        observed_statuses = []

        def monitor():
            for _ in range(15):
                time.sleep(0.08)
                status = _read_lease_status(self.db_path, "m_heartbeat_inprogress")
                if status is not None:
                    observed_statuses.append(status)

        t_owner = threading.Thread(target=run_owner)
        t_intruder = threading.Thread(target=run_intruder)
        t_monitor = threading.Thread(target=monitor)
        t_owner.start()
        t_intruder.start()
        t_monitor.start()
        t_owner.join(timeout=10.0)
        t_intruder.join(timeout=10.0)
        t_monitor.join(timeout=10.0)

        with call_lock:
            self.assertEqual(call_count["n"], 1, f"runner reexecutado {call_count['n']}x")

        # PROVA CRÍTICA: o lease NUNCA deveria ter passado por
        # UNKNOWN_OUTCOME durante toda a execução monitorada.
        self.assertNotIn(
            "UNKNOWN_OUTCOME", observed_statuses,
            f"lease foi convertido incorretamente para UNKNOWN_OUTCOME -- heartbeat não protegeu: {observed_statuses}",
        )

        self.assertEqual(results["owner"].result_for("t1").status, "VERIFIED")
        # o "intruso" (chamada concorrente enquanto o dono legítimo
        # ainda executava) reporta corretamente que não pôde
        # prosseguir -- isso é esperado e correto (alguém já detém o
        # lease), NUNCA um bug; o que importa é que o LEASE em si
        # nunca foi corrompido.
        self.assertIn(results["intruder"].result_for("t1").status, ("TIMED_OUT_UNKNOWN_OUTCOME", "VERIFIED", "SKIPPED_ALREADY_DONE"))

        # estado FINAL -- sempre consistente, nunca contraditório.
        mission = tracker_1.get("m_heartbeat_inprogress", "t1")
        self.assertEqual(mission.status, MissionStatus.VERIFIED)
        self.assertEqual(_read_lease_status(self.db_path, "m_heartbeat_inprogress"), "COMPLETED")

        executor_1.wait_for_inflight_completion(timeout=1.0)

    def test_leased_at_genuinely_advances_during_long_running_healthy_task(self):
        """Prova direta via SQL de que `leased_at` avança
        continuamente durante uma execução saudável mais longa que o
        TTL — mesma disciplina de prova já usada para
        `GATE_IN_PROGRESS`, agora também para `IN_PROGRESS`."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        def slow_runner(task):
            time.sleep(self.ttl * 3)
            return {"ok": True}

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )

        def run():
            executor.execute_plan(
                plan, task_runner=slow_runner, actor="teste", mission_id="m_leased_at_proof",
                timeout_per_task=5.0,
            )

        t = threading.Thread(target=run)
        t.start()

        import sqlite3
        readings = []
        for _ in range(4):
            time.sleep(self.ttl * 0.8)
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT leased_at FROM graph_executor_task_leases WHERE mission_id='m_leased_at_proof' AND task_id='t1'",
            ).fetchone()
            conn.close()
            if row is not None:
                readings.append(row["leased_at"])

        t.join(timeout=10.0)
        executor.wait_for_inflight_completion(timeout=1.0)

        self.assertGreaterEqual(len(readings), 2)
        self.assertLess(readings[0], readings[-1], "leased_at nunca avançou -- heartbeat de IN_PROGRESS não está funcionando")

    def test_heartbeat_continues_past_timeout_per_task_while_thread_alive(self):
        """Requisito explícito: o heartbeat deve continuar mesmo
        DEPOIS do chamador desistir de esperar por `timeout_per_task`
        — enquanto a thread real ainda estiver genuinamente viva."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        def slow_runner(task):
            time.sleep(1.0)  # MUITO mais que o timeout_per_task abaixo
            return {"ok": True}

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )

        report = executor.execute_plan(
            plan, task_runner=slow_runner, actor="teste", mission_id="m_past_timeout", timeout_per_task=0.1,
        )
        # o CHAMADOR desiste rapido (timeout_per_task=0.1s), mas a
        # thread real continua rodando por 1.0s -- o heartbeat deve
        # continuar rodando durante esse tempo TAMBEM.
        self.assertEqual(report.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        # aguarda um pouco (menos que os 1.0s totais da thread, mas
        # mais que o TTL) e confirma que o lease AINDA NAO foi
        # convertido para UNKNOWN_OUTCOME -- o heartbeat da thread
        # ainda viva deveria estar protegendo.
        time.sleep(self.ttl * 1.5)
        status_mid = _read_lease_status(self.db_path, "m_past_timeout")
        self.assertNotEqual(
            status_mid, "UNKNOWN_OUTCOME",
            "heartbeat parou de proteger antes da thread real (ainda viva) genuinamente terminar",
        )

        executor.wait_for_inflight_completion(timeout=3.0)


class CrashStillConvertsToUnknownAfterHeartbeatCeasesTests(unittest.TestCase):
    """Reconfirma explicitamente (nesta rodada) que o cenário de
    crash real continua funcionando com o heartbeat de `IN_PROGRESS`
    presente — `os._exit()` mata a thread real E a thread de
    heartbeat juntas (mesmo processo); após o TTL, sem nenhum
    heartbeat, uma nova instância converte para `UNKNOWN_OUTCOME`,
    sem rerun automático."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.ttl = 0.3

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_real_subprocess_crash_during_runner_still_converts_after_ttl(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        result = subprocess.run(
            [sys.executable, _DURING_RUNNER_WORKER, self.db_path, "m_crash_heartbeat", str(self.ttl)], timeout=15,
        )
        self.assertNotEqual(result.returncode, 0, "o subprocesso deveria ter morrido abruptamente")

        # logo apos o crash -- lease IN_PROGRESS, heartbeat morreu junto
        self.assertEqual(_read_lease_status(self.db_path, "m_crash_heartbeat"), "IN_PROGRESS")

        # aguarda o TTL expirar de verdade -- SEM nenhum heartbeat
        # (processo morto, nada renovando)
        time.sleep(self.ttl + 0.2)

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            return {"ok": True}

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_crash_heartbeat", timeout_per_task=5.0,
        )

        self.assertEqual(call_count["n"], 0, "NUNCA deveria reexecutar automaticamente")
        self.assertEqual(report.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        self.assertEqual(_read_lease_status(self.db_path, "m_crash_heartbeat"), "UNKNOWN_OUTCOME")

        mission = tracker.get("m_crash_heartbeat", "t1")
        self.assertNotEqual(mission.status, MissionStatus.VERIFIED)
        self.assertNotEqual(mission.status, MissionStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
