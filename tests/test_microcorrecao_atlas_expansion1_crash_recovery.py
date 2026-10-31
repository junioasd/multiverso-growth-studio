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
MICROCORREÇÃO — ATLAS EXPANSION 1: RECUPERAÇÃO DE CRASH REAL EM
GATE_IN_PROGRESS (TTL + FENCING GENERATION).

Falha real reproduzida por auditoria independente, comprovada com um
SUBPROCESSO REAL morto via `os._exit()`: se o processo que reivindica
um outcome tardio (`OUTCOME_AVAILABLE_PENDING_GATE` →
`GATE_IN_PROGRESS`) morre ABRUPTAMENTE — não uma exceção tratável,
que a microcorreção anterior já resolve, mas um crash real de
processo/máquina — o lease ficava preso em `GATE_IN_PROGRESS` PARA
SEMPRE. Nenhuma instância futura, por mais saudável que fosse,
conseguia reivindicar de novo — `TIMED_OUT_UNKNOWN_OUTCOME`
indefinidamente, o Quality Gate nunca era avaliado, o outcome já
obtido de verdade pelo `task_runner` nunca era finalizado.

Corrigido: `leased_at` (já existente) agora participa da detecção de
expiração — um `GATE_IN_PROGRESS` mais velho que
`gate_in_progress_ttl_seconds` é elegível para reclaim. O reclaim é
ATÔMICO (compare-and-set SQL condicionado a `status` E `leased_at`
ainda serem exatamente os lidos — nunca assume que nada mudou nesse
meio-tempo), rotaciona o `lease_token` E incrementa um novo contador
`generation` (fencing) — nova coluna aditiva, migração testada
separadamente. Qualquer instância com o token/generation antigo (o
processo morto) nunca mais consegue resolver o lease (`_resolve_lease`
já era condicionado ao `lease_token`, que a partir do reclaim nunca
mais bate para o token antigo).
"""

_WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "_helpers", "crash_gate_worker.py")


def _fresh_tracker_and_queue(db_path=None):
    return MissionTracker(db_path=db_path), JobQueue(db_path=db_path)


class RealSubprocessCrashRecoveryTests(unittest.TestCase):
    """Cenário EXATO exigido: timeout → outcome tardio → processo
    reivindica gate e morre com `os._exit()` → aguarda expiração →
    nova instância faz reclaim → runner permanece 1x, gate executa 1x
    e finaliza corretamente."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.ttl = 0.3  # curto, para viabilidade prática do teste

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _produce_pending_gate_outcome(self, plan, runner, mission_id):
        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id=mission_id, timeout_per_task=0.05,
        )
        self.assertEqual(report.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        time.sleep(0.35)
        return executor

    def _crash_subprocess_during_gate(self, mission_id: str) -> int:
        result = subprocess.run(
            [sys.executable, _WORKER_SCRIPT, self.db_path, mission_id, str(self.ttl)], timeout=15,
        )
        return result.returncode

    def test_real_subprocess_crash_recovers_via_ttl_reclaim(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        original_executor = self._produce_pending_gate_outcome(plan, runner, "m_subprocess_crash")

        # SUBPROCESSO REAL reivindica o gate e MORRE via os._exit()
        exit_code = self._crash_subprocess_during_gate("m_subprocess_crash")
        self.assertNotEqual(exit_code, 0, "o subprocesso deveria ter morrido abruptamente")

        # confirma o lease genuinamente preso em GATE_IN_PROGRESS logo apos o crash
        self.assertEqual(self._read_lease_status("m_subprocess_crash"), "GATE_IN_PROGRESS")

        # AGUARDA o TTL expirar de verdade
        time.sleep(self.ttl + 0.2)

        gate_calls = {"n": 0}

        def gate_approves(task, outcome):
            gate_calls["n"] += 1
            return True

        tracker_healthy, queue_healthy = _fresh_tracker_and_queue(self.db_path)
        executor_healthy = GraphExecutor(
            mission_tracker=tracker_healthy, job_queue=queue_healthy, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report_healthy = executor_healthy.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_subprocess_crash", timeout_per_task=5.0,
            quality_gate_checker=gate_approves,
        )

        self.assertEqual(call_count["n"], 1, "runner deveria ter executado exatamente 1 vez")
        self.assertEqual(gate_calls["n"], 1, "gate deveria ter executado exatamente 1 vez após a recuperação")
        self.assertEqual(report_healthy.result_for("t1").status, "VERIFIED")

        mission = tracker_healthy.get("m_subprocess_crash", "t1")
        self.assertEqual(mission.status, MissionStatus.VERIFIED)
        self.assertEqual(self._read_lease_status("m_subprocess_crash"), "COMPLETED")

        original_executor.wait_for_inflight_completion(timeout=1.0)

    def test_reclaim_never_happens_before_ttl_expires(self):
        """Controle negativo: tentar reclamar IMEDIATAMENTE após o
        crash (antes do TTL expirar) nunca reclama — devolve
        `TIMED_OUT_UNKNOWN_OUTCOME` imediatamente, nunca gira."""
        self.ttl = 5.0  # TTL longo desta vez
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        def runner(task):
            time.sleep(0.2)
            return {"ok": True}

        original_executor = self._produce_pending_gate_outcome(plan, runner, "m_too_early")
        exit_code = self._crash_subprocess_during_gate("m_too_early")
        self.assertNotEqual(exit_code, 0)

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        start = time.time()
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_too_early", timeout_per_task=5.0,
            quality_gate_checker=lambda t, o: True,
        )
        duration = time.time() - start

        self.assertLess(duration, 1.0, "deveria ter retornado imediatamente, nunca girado esperando")
        self.assertEqual(report.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        self.assertEqual(self._read_lease_status("m_too_early"), "GATE_IN_PROGRESS")

        original_executor.wait_for_inflight_completion(timeout=1.0)

    def test_generation_increments_on_reclaim_fencing_token_rotates(self):
        """Confirma diretamente que o fencing (generation) avança e o
        token muda a cada reclaim — o processo morto nunca poderia
        finalizar com o token antigo."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        def runner(task):
            time.sleep(0.2)
            return {"ok": True}

        original_executor = self._produce_pending_gate_outcome(plan, runner, "m_fencing")
        self._crash_subprocess_during_gate("m_fencing")

        generation_after_crash = self._read_lease_generation("m_fencing")
        self.assertGreaterEqual(generation_after_crash, 1)

        time.sleep(self.ttl + 0.2)

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_fencing", timeout_per_task=5.0,
            quality_gate_checker=lambda t, o: True,
        )

        generation_after_reclaim = self._read_lease_generation("m_fencing")
        self.assertGreater(generation_after_reclaim, generation_after_crash, "generation deveria ter avançado no reclaim")

        original_executor.wait_for_inflight_completion(timeout=1.0)

    def _read_lease_status(self, mission_id: str) -> str:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM graph_executor_task_leases WHERE mission_id=? AND task_id='t1'", (mission_id,),
        ).fetchone()
        conn.close()
        return row["status"] if row else None

    def _read_lease_generation(self, mission_id: str) -> int:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT generation FROM graph_executor_task_leases WHERE mission_id=? AND task_id='t1'", (mission_id,),
        ).fetchone()
        conn.close()
        return row["generation"] if row else None


class MultipleInstancesReclaimRaceTests(unittest.TestCase):
    """Item exigido: corrida de múltiplas instâncias tentando reclaim
    simultaneamente após um crash real — apenas um vencedor."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.ttl = 0.3

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_three_instances_racing_to_reclaim_only_one_winner(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}
        call_lock = threading.Lock()

        def runner(task):
            with call_lock:
                call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        tracker_1, queue_1 = _fresh_tracker_and_queue(self.db_path)
        executor_1 = GraphExecutor(
            mission_tracker=tracker_1, job_queue=queue_1, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report_1 = executor_1.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_race_reclaim", timeout_per_task=0.05,
        )
        self.assertEqual(report_1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        time.sleep(0.35)

        result = subprocess.run(
            [sys.executable, _WORKER_SCRIPT, self.db_path, "m_race_reclaim", str(self.ttl)], timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)

        time.sleep(self.ttl + 0.2)  # aguarda TTL expirar

        gate_calls = {"n": 0}
        gate_lock = threading.Lock()

        def gate_approves(task, outcome):
            with gate_lock:
                gate_calls["n"] += 1
            return True

        results = {}
        errors = {}

        def run(label):
            tracker, queue = _fresh_tracker_and_queue(self.db_path)
            executor = GraphExecutor(
                mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
                gate_in_progress_ttl_seconds=self.ttl,
            )
            try:
                results[label] = executor.execute_plan(
                    plan, task_runner=runner, actor="teste", mission_id="m_race_reclaim", timeout_per_task=5.0,
                    quality_gate_checker=gate_approves,
                )
            except Exception as exc:
                errors[label] = exc

        threads = [threading.Thread(target=run, args=(f"i{i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, {}, f"exceções de concorrência não tratadas: {errors}")
        self.assertEqual(gate_calls["n"], 1, f"gate chamado {gate_calls['n']}x — reclaim não foi exclusivo")
        with call_lock:
            self.assertEqual(call_count["n"], 1, f"runner reexecutado {call_count['n']}x")

        for label in results:
            self.assertIn(
                results[label].result_for("t1").status,
                ("VERIFIED", "SKIPPED_ALREADY_DONE", "TIMED_OUT_UNKNOWN_OUTCOME"),
            )

        final_mission = tracker_1.get("m_race_reclaim", "t1")
        self.assertEqual(final_mission.status, MissionStatus.VERIFIED)

        executor_1.wait_for_inflight_completion(timeout=1.0)


class GenerationColumnMigrationTests(unittest.TestCase):
    """Migração aditiva da coluna `generation` — nunca perde um lease
    já persistido de uma versão anterior."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_legacy_database_without_generation_column_migrates_safely(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE graph_executor_task_leases (mission_id TEXT NOT NULL, task_id TEXT NOT NULL, "
            "lease_token TEXT NOT NULL, status TEXT NOT NULL, leased_at REAL NOT NULL, resolved_at REAL, "
            "outcome_json TEXT, error TEXT, PRIMARY KEY (mission_id, task_id))"
        )
        conn.execute(
            "INSERT INTO graph_executor_task_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("m_legacy", "t1", "tok_legado", "GATE_IN_PROGRESS", 1000.0, None, '{"ok": true}', None),
        )
        conn.commit()
        conn.close()

        db_module.init_db(self.db_path)

        conn2 = sqlite3.connect(self.db_path)
        conn2.row_factory = sqlite3.Row
        row = conn2.execute("SELECT * FROM graph_executor_task_leases WHERE mission_id='m_legacy'").fetchone()
        conn2.close()

        self.assertEqual(row["generation"], 0)
        self.assertEqual(row["status"], "GATE_IN_PROGRESS")
        self.assertEqual(row["lease_token"], "tok_legado")

    def test_fresh_database_already_has_generation_column(self):
        db_module.init_db(self.db_path)
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(graph_executor_task_leases)").fetchall()]
        conn.close()
        self.assertIn("generation", cols)


if __name__ == "__main__":
    unittest.main()
