import os
import tempfile
import threading
import time
import unittest

from atlas.advanced_planning import AdvancedPlanner
from core.coordination.mission import MissionTracker
from core.jobs.queue import JobQueue
from core.models import db as db_module
from atlas.orchestration.graph_executor import GraphExecutor

"""
MICROCORREÇÃO — ATLAS EXPANSION 1: PROTEÇÃO MULTI-INSTÂNCIA REAL.

Falha real reproduzida por auditoria independente: a proteção contra
execução concorrente de uma MESMA `(mission_id, task_id)` era local à
instância de `GraphExecutor` (`_inflight`) — duas instâncias
diferentes (dois processos, ou dois objetos `GraphExecutor`) não
tinham como saber uma da outra. Em memória, `JobQueue.try_start()`
também não validava `_STARTABLE_STATES`, permitindo reiniciar um job
já `RUNNING`. Em SQLite/restart, uma nova instância ficava presa em
`RUNNING` (sem saber que a original nunca resolveu), e depois de
`recover_stuck_jobs()` podia retentar um `TIMED_OUT_UNKNOWN_OUTCOME`
cujo efeito original já tinha ocorrido, duplicando o efeito.

Corrigido:
1. `JobQueue.try_start()` em memória agora valida `_STARTABLE_STATES`
   como o caminho durável já fazia.
2. A exclusão de execução por `(mission_id, task_id)` é agora um
   LEASE real — compartilhado entre instâncias no mesmo processo (via
   registro de CLASSE, não de instância) quando não há `db_path`, e
   PERSISTENTE em SQLite (`graph_executor_task_leases`, `INSERT`
   atômico com `PRIMARY KEY`) quando há — sobrevive a restart,
   visível para qualquer instância apontando para o mesmo banco.
3. `TIMED_OUT_UNKNOWN_OUTCOME` é sempre persistido/compartilhado —
   nunca vira retry automático, mesmo depois de `recover_stuck_jobs()`
   mudar o estado do `Job` (o lease, não o `Job`, é a fonte de
   verdade).
4. Uma nova instância que encontra um lease `IN_PROGRESS`/
   `UNKNOWN_OUTCOME` reporta `TIMED_OUT_UNKNOWN_OUTCOME`
   IMEDIATAMENTE — nunca gira esperando, nunca reexecuta
   silenciosamente.
"""


class JobQueueTryStartInMemoryValidatesStatesTests(unittest.TestCase):
    """Item 1 — `try_start()` em memória agora rejeita reiniciar um
    job que já não está num estado iniciável."""

    def test_try_start_rejects_already_running_job(self):
        queue = JobQueue()
        job = queue.enqueue(job_type="x", idempotency_key="k1", payload={}, max_attempts=3)
        started = queue.try_start(job.job_id)
        self.assertIsNotNone(started)
        self.assertEqual(started.attempts, 1)

        second = queue.try_start(job.job_id)
        self.assertIsNone(second, "try_start() reiniciou um job já RUNNING — proteção ausente")

    def test_try_start_still_works_for_queued_and_retrying(self):
        queue = JobQueue()
        job = queue.enqueue(job_type="x", idempotency_key="k2", payload={}, max_attempts=3)
        started = queue.try_start(job.job_id)
        self.assertIsNotNone(started)  # QUEUED -> RUNNING continua funcionando

        queue.fail(job.job_id, "erro simulado")
        fresh = queue.get(job.job_id)
        if fresh.state.value == "RETRYING":
            retried = queue.try_start(job.job_id)
            self.assertIsNotNone(retried)  # RETRYING -> RUNNING continua funcionando

    def test_try_start_rejects_completed_job(self):
        queue = JobQueue()
        job = queue.enqueue(job_type="x", idempotency_key="k3", payload={}, max_attempts=3)
        queue.try_start(job.job_id)
        queue.complete(job.job_id, {"ok": True})
        self.assertIsNone(queue.try_start(job.job_id))


def _fresh_tracker_and_queue():
    return MissionTracker(), JobQueue()


class MultiInstanceMemoryNeverDuplicatesTests(unittest.TestCase):
    """Item 2/5 — duas instâncias de `GraphExecutor` no MESMO
    processo, compartilhando o MESMO `JobQueue` em memória, nunca
    aplicam o mesmo efeito duas vezes."""

    def test_two_instances_racing_same_task_apply_effect_exactly_once(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        effect_log = []
        lock = threading.Lock()

        def runner(task):
            with lock:
                effect_log.append("iniciou")
            time.sleep(0.3)
            with lock:
                effect_log.append("EFEITO_APLICADO")
            return {"ok": True}

        tracker, queue = _fresh_tracker_and_queue()
        executor_a = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)
        executor_b = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)

        results = {}

        def run_a():
            results["a"] = executor_a.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_race", timeout_per_task=0.05,
            )

        def run_b():
            time.sleep(0.02)
            results["b"] = executor_b.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_race", timeout_per_task=0.05,
            )

        t_a = threading.Thread(target=run_a)
        t_b = threading.Thread(target=run_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=5.0)
        t_b.join(timeout=5.0)

        executor_a.wait_for_inflight_completion(timeout=2.0)
        executor_b.wait_for_inflight_completion(timeout=2.0)

        starts = [e for e in effect_log if e == "iniciou"]
        applied = [e for e in effect_log if e == "EFEITO_APLICADO"]
        self.assertEqual(len(starts), 1, f"{len(starts)} inícios — duas instâncias iniciaram a mesma tarefa")
        self.assertEqual(len(applied), 1, f"{len(applied)} efeitos aplicados — duplicidade real de efeito externo")

    def test_two_instances_different_job_queues_never_share_lease(self):
        """Controle negativo: instâncias apontando para `JobQueue`
        DIFERENTES (mundos genuinamente não relacionados) NÃO
        compartilham lease — cada uma pode executar sua própria
        tarefa livremente."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1"}])

        tracker_a, queue_a = _fresh_tracker_and_queue()
        tracker_b, queue_b = _fresh_tracker_and_queue()
        executor_a = GraphExecutor(mission_tracker=tracker_a, job_queue=queue_a, max_workers=4)
        executor_b = GraphExecutor(mission_tracker=tracker_b, job_queue=queue_b, max_workers=4)

        report_a = executor_a.execute_plan(plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_same_id")
        report_b = executor_b.execute_plan(plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_same_id")

        self.assertEqual(report_a.result_for("t1").status, "VERIFIED")
        self.assertEqual(report_b.result_for("t1").status, "VERIFIED")


class MultiInstanceDurableRestartNeverDuplicatesTests(unittest.TestCase):
    """Item 2/3/4/5 — cenário SQLite/restart: nova instância nunca
    reexecuta, nunca gira indefinidamente, mesmo após
    `recover_stuck_jobs()`."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_new_instance_after_restart_never_reexecutes_never_spins(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        effect_log = []
        lock = threading.Lock()

        def runner(task):
            with lock:
                effect_log.append("iniciou")
            time.sleep(0.3)
            with lock:
                effect_log.append("EFEITO_APLICADO")
            return {"ok": True}

        # "processo 1"
        tracker_1 = MissionTracker(db_path=self.db_path)
        queue_1 = JobQueue(db_path=self.db_path)
        executor_1 = GraphExecutor(mission_tracker=tracker_1, job_queue=queue_1, db_path=self.db_path, max_workers=4)
        report_1 = executor_1.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_restart", timeout_per_task=0.05,
        )
        self.assertEqual(report_1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        # "restart" -- nova instância, mesmo db_path, ANTES da thread
        # original terminar (ainda dorme — só 0.05s passaram de 0.3s)
        tracker_2 = MissionTracker(db_path=self.db_path)
        queue_2 = JobQueue(db_path=self.db_path)
        executor_2 = GraphExecutor(mission_tracker=tracker_2, job_queue=queue_2, db_path=self.db_path, max_workers=4)

        start = time.time()
        report_2 = executor_2.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_restart", timeout_per_task=5.0,
        )
        duration = time.time() - start

        self.assertLess(duration, 1.0, "nova instância girou/esperou em vez de retornar imediatamente")
        self.assertEqual(report_2.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        starts = [e for e in effect_log if e == "iniciou"]
        self.assertEqual(len(starts), 1, f"{len(starts)} inícios — restart causou reexecução")

        executor_1.wait_for_inflight_completion(timeout=2.0)

    def test_recover_stuck_jobs_never_bypasses_the_lease(self):
        """Cenário EXATO reportado: `recover_stuck_jobs()` move o Job
        para RETRYING, mas o lease (fonte de verdade) continua
        `UNKNOWN_OUTCOME` — nunca libera retry."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 3}])

        effect_log = []
        lock = threading.Lock()

        def runner(task):
            with lock:
                effect_log.append("iniciou")
            time.sleep(0.3)
            with lock:
                effect_log.append("EFEITO_APLICADO")
            return {"ok": True}

        tracker_1 = MissionTracker(db_path=self.db_path)
        queue_1 = JobQueue(db_path=self.db_path)
        executor_1 = GraphExecutor(mission_tracker=tracker_1, job_queue=queue_1, db_path=self.db_path, max_workers=4)
        report_1 = executor_1.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_recover", timeout_per_task=0.05,
        )
        self.assertEqual(report_1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        # mecanismo de recuperação (housekeeping) roda ENQUANTO a
        # thread original ainda está viva
        queue_1.recover_stuck_jobs(timeout_seconds=0.001)

        tracker_2 = MissionTracker(db_path=self.db_path)
        queue_2 = JobQueue(db_path=self.db_path)
        executor_2 = GraphExecutor(mission_tracker=tracker_2, job_queue=queue_2, db_path=self.db_path, max_workers=4)

        start = time.time()
        report_2 = executor_2.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_recover", timeout_per_task=5.0,
        )
        duration = time.time() - start

        self.assertLess(duration, 1.0)
        self.assertEqual(
            report_2.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME",
            "recover_stuck_jobs() permitiu reexecução apesar do lease UNKNOWN_OUTCOME",
        )
        starts = [e for e in effect_log if e == "iniciou"]
        self.assertEqual(len(starts), 1)

        executor_1.wait_for_inflight_completion(timeout=2.0)

    def test_lease_survives_restart_across_genuinely_separate_processes_simulation(self):
        """Confirma que o lease é lido corretamente do banco por uma
        instância que NUNCA viu a primeira em memória alguma —
        simulação mais forte de processo genuinamente separado
        (nenhum objeto Python compartilhado, só o arquivo)."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        def runner_that_hangs_past_timeout(task):
            time.sleep(0.3)
            return {"ok": True}

        tracker_1 = MissionTracker(db_path=self.db_path)
        queue_1 = JobQueue(db_path=self.db_path)
        executor_1 = GraphExecutor(mission_tracker=tracker_1, job_queue=queue_1, db_path=self.db_path, max_workers=4)
        executor_1.execute_plan(
            plan, task_runner=runner_that_hangs_past_timeout, actor="teste", mission_id="m_sep",
            timeout_per_task=0.05,
        )

        # NENHUMA referência a `executor_1`/`tracker_1`/`queue_1` a
        # partir daqui — só o `db_path`, como um processo genuinamente
        # novo faria.
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM graph_executor_task_leases WHERE mission_id=? AND task_id=?", ("m_sep", "t1"),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertIn(row["status"], ("UNKNOWN_OUTCOME", "IN_PROGRESS"))

        executor_1.wait_for_inflight_completion(timeout=2.0)

    def test_late_resolution_after_restart_scenario_still_never_reruns(self):
        """Depois que a thread original termina de verdade (mesmo
        processo desta vez, mas simulando o padrão de retomada), uma
        chamada seguinte usa o resultado real — nunca reexecuta."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])
        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True, "chamada": call_count["n"]}

        tracker = MissionTracker(db_path=self.db_path)
        queue = JobQueue(db_path=self.db_path)
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4)

        report1 = executor.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m_late", timeout_per_task=0.05)
        self.assertEqual(report1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        time.sleep(0.4)  # thread original termina de verdade
        self.assertEqual(call_count["n"], 1)

        report2 = executor.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m_late", timeout_per_task=5.0)
        self.assertEqual(call_count["n"], 1, "reexecutou apesar do lease já resolvido")
        self.assertEqual(report2.result_for("t1").status, "VERIFIED")


if __name__ == "__main__":
    unittest.main()
