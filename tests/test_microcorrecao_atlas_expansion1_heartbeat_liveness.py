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
MICROCORREÇÃO — ATLAS EXPANSION 1: LIVENESS REAL (HEARTBEAT) PARA
GATE_IN_PROGRESS.

Falha real reproduzida por auditoria independente: `GATE_IN_PROGRESS`
expirava apenas por `leased_at` estático — sem heartbeat/renovação,
um Quality Gate SAUDÁVEL mas legitimamente mais lento que
`gate_in_progress_ttl_seconds` fazia OUTRA instância considerar o
lease morto, reclamar, e avaliar o MESMO gate simultaneamente.
Reproduzido: `runner` 1x, gate 2x, DUAS chamadas retornando
`VERIFIED`.

Corrigido: enquanto a instância dona avalia o gate,
`_evaluate_quality_gate_with_heartbeat()` roda uma thread de
heartbeat em segundo plano que renova `leased_at` atomicamente
(condicionado ao `lease_token` ATUAL) a cada `gate_in_progress_ttl_
seconds / 3` — o lease nunca parece expirado para outra instância
enquanto o heartbeat continuar, não importa quão lento (mas
genuinamente vivo) o checker seja. Reclaim só ocorre quando o
heartbeat CESSA de verdade (crash real — a thread de heartbeat,
sendo do MESMO processo, morre junto) e o TTL se esgota sem nenhuma
renovação.
"""

_WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "_helpers", "crash_gate_worker.py")


def _fresh_tracker_and_queue(db_path=None):
    return MissionTracker(db_path=db_path), JobQueue(db_path=db_path)


class SlowHealthyCheckerLivenessTests(unittest.TestCase):
    """Cenário exato exigido: checker deliberadamente lento (>TTL),
    mas SAUDÁVEL — nenhuma segunda instância deve reclamar enquanto
    houver heartbeat; `runner` 1x, gate 1x."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.ttl = 0.3  # curto, para viabilidade prática do teste

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_slow_healthy_checker_never_reclaimed_while_heartbeating(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}
        call_lock = threading.Lock()

        def runner(task):
            with call_lock:
                call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        gate_calls = {"n": 0}
        gate_lock = threading.Lock()

        def slow_healthy_gate(task, outcome):
            with gate_lock:
                gate_calls["n"] += 1
            # DELIBERADAMENTE mais lento que o TTL (0.3s) — mas
            # genuinamente SAUDÁVEL, nunca crasha, sempre termina.
            time.sleep(self.ttl * 4)
            return True

        # produz o outcome tardio pendente de gate
        tracker_1, queue_1 = _fresh_tracker_and_queue(self.db_path)
        executor_1 = GraphExecutor(
            mission_tracker=tracker_1, job_queue=queue_1, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report_1 = executor_1.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_slow_gate", timeout_per_task=0.05,
        )
        self.assertEqual(report_1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        time.sleep(0.35)

        results = {}

        def run_owner():
            tracker, queue = _fresh_tracker_and_queue(self.db_path)
            executor = GraphExecutor(
                mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
                gate_in_progress_ttl_seconds=self.ttl,
            )
            results["owner"] = executor.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_slow_gate", timeout_per_task=5.0,
                quality_gate_checker=slow_healthy_gate,
            )

        def run_intruder():
            # espera o suficiente para o TTL "nominal" já ter passado
            # DESDE A REIVINDICAÇÃO original — sem heartbeat, isso
            # tentaria (incorretamente) reclamar; com heartbeat, o
            # lease continua sendo renovado e o intruso nunca reclama.
            time.sleep(self.ttl * 1.5)
            tracker, queue = _fresh_tracker_and_queue(self.db_path)
            executor = GraphExecutor(
                mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
                gate_in_progress_ttl_seconds=self.ttl,
            )
            results["intruder"] = executor.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_slow_gate", timeout_per_task=5.0,
                quality_gate_checker=slow_healthy_gate,
            )

        t_owner = threading.Thread(target=run_owner)
        t_intruder = threading.Thread(target=run_intruder)
        t_owner.start()
        t_intruder.start()
        t_owner.join(timeout=10.0)
        t_intruder.join(timeout=10.0)

        with call_lock:
            self.assertEqual(call_count["n"], 1, f"runner reexecutado {call_count['n']}x")
        with gate_lock:
            self.assertEqual(gate_calls["n"], 1, f"gate reexecutado {gate_calls['n']}x — reclaim indevido durante avaliação legítima")

        self.assertEqual(results["owner"].result_for("t1").status, "VERIFIED")
        # o "intruso" nunca reclamou -- ou observou o lease ainda em
        # andamento (nunca reclamando), ou encontrou a missão já
        # VERIFIED pelo caminho rápido de retomada, dependendo da
        # ordem exata das threads -- nunca reexecutou o gate.
        self.assertIn(results["intruder"].result_for("t1").status, ("TIMED_OUT_UNKNOWN_OUTCOME", "SKIPPED_ALREADY_DONE", "VERIFIED"))

        mission = tracker_1.get("m_slow_gate", "t1")
        self.assertEqual(mission.status, MissionStatus.VERIFIED)

        executor_1.wait_for_inflight_completion(timeout=1.0)

    def test_heartbeat_actually_renews_leased_at_in_database(self):
        """Confirma diretamente, via SQL, que `leased_at` avança
        genuinamente ENQUANTO o gate lento está rodando — prova
        positiva do heartbeat, não apenas o efeito indireto."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        def runner(task):
            time.sleep(0.2)
            return {"ok": True}

        def slow_gate(task, outcome):
            time.sleep(self.ttl * 3)
            return True

        tracker_1, queue_1 = _fresh_tracker_and_queue(self.db_path)
        executor_1 = GraphExecutor(
            mission_tracker=tracker_1, job_queue=queue_1, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        executor_1.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m_heartbeat_proof", timeout_per_task=0.05)
        time.sleep(0.35)

        tracker_2, queue_2 = _fresh_tracker_and_queue(self.db_path)
        executor_2 = GraphExecutor(
            mission_tracker=tracker_2, job_queue=queue_2, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )

        def run():
            executor_2.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_heartbeat_proof", timeout_per_task=5.0,
                quality_gate_checker=slow_gate,
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
                "SELECT leased_at FROM graph_executor_task_leases WHERE mission_id='m_heartbeat_proof' AND task_id='t1'",
            ).fetchone()
            conn.close()
            if row is not None:
                readings.append(row["leased_at"])

        t.join(timeout=10.0)
        executor_1.wait_for_inflight_completion(timeout=1.0)

        # leased_at deve ter avancado ao longo das leituras -- prova
        # direta de que o heartbeat esteve genuinamente renovando.
        self.assertGreaterEqual(len(readings), 2)
        self.assertLess(readings[0], readings[-1], "leased_at nunca avançou -- heartbeat não está funcionando")


class CrashStillRecoversWithHeartbeatPresentTests(unittest.TestCase):
    """Reconfirma explicitamente (nesta rodada) que o cenário de
    crash real continua funcionando com o heartbeat presente — a
    thread de heartbeat, sendo do MESMO processo que crasha, morre
    junto; nada impede o reclaim após o TTL."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.ttl = 0.3

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_real_subprocess_crash_still_recovers_correctly(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        tracker_1, queue_1 = _fresh_tracker_and_queue(self.db_path)
        executor_1 = GraphExecutor(
            mission_tracker=tracker_1, job_queue=queue_1, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report_1 = executor_1.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_crash_with_heartbeat", timeout_per_task=0.05,
        )
        self.assertEqual(report_1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        time.sleep(0.35)

        # SUBPROCESSO REAL reivindica o gate e morre via os._exit()
        # ANTES de qualquer heartbeat ter chance de rodar de verdade
        # (o worker mata o processo imediatamente dentro do checker).
        result = subprocess.run(
            [sys.executable, _WORKER_SCRIPT, self.db_path, "m_crash_with_heartbeat", str(self.ttl)], timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)

        # confirma preso em GATE_IN_PROGRESS logo apos o crash
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM graph_executor_task_leases WHERE mission_id='m_crash_with_heartbeat' AND task_id='t1'",
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "GATE_IN_PROGRESS")

        # aguarda o TTL expirar de verdade -- SEM heartbeat algum
        # (processo morto, nada renovando)
        time.sleep(self.ttl + 0.2)

        tracker_healthy, queue_healthy = _fresh_tracker_and_queue(self.db_path)
        executor_healthy = GraphExecutor(
            mission_tracker=tracker_healthy, job_queue=queue_healthy, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report_healthy = executor_healthy.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_crash_with_heartbeat", timeout_per_task=5.0,
            quality_gate_checker=lambda t, o: True,
        )

        self.assertEqual(call_count["n"], 1, "runner deveria ter executado exatamente 1 vez")
        self.assertEqual(report_healthy.result_for("t1").status, "VERIFIED")

        mission = tracker_healthy.get("m_crash_with_heartbeat", "t1")
        self.assertEqual(mission.status, MissionStatus.VERIFIED)

        executor_1.wait_for_inflight_completion(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
