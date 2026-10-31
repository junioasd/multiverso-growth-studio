import os
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
MICROCORREÇÃO — ATLAS EXPANSION 1: QUALITY GATE FAIL-CLOSED SEM
CHECKER + REIVINDICAÇÃO REALMENTE EXCLUSIVA EM SQLITE.

Falhas reais reproduzidas por auditoria independente:

1. Fail-open de Quality Gate: se `task.quality_gate` estava definido,
   mas `quality_gate_checker` estava ausente numa chamada de
   retomada, o sistema tratava isso como implicitamente aprovado —
   `_evaluate_quality_gate()` não distinguia "nenhum gate aplicável"
   de "gate declarado mas checker indisponível agora". Reproduzido:
   primeira execução `TIMED_OUT_UNKNOWN_OUTCOME`; resultado tardio
   ficou pendente; nova chamada retomou SEM checker; terminou
   `VERIFIED`, com `runner` 1x e o gate NUNCA avaliado.

2. Corrida em SQLite: a reivindicação de
   `OUTCOME_AVAILABLE_PENDING_GATE` não era realmente exclusiva — o
   `UPDATE` trocava apenas `lease_token`, mas MANTINHA `status` no
   mesmo valor, então uma segunda instância rodando a MESMA instrução
   também batia no `WHERE status = 'OUTCOME_AVAILABLE_PENDING_GATE'`
   (nada tinha mudado) e TAMBÉM "ganhava" a reivindicação.
   Reproduzido: gate chamado 2x; uma instância `VERIFIED`, a outra
   levantou `ConcurrentMissionUpdate`.

Corrigido:
1. `_evaluate_quality_gate()` agora distingue 3 desfechos —
   `"APPROVED"`/`"REJECTED"`/`"UNAVAILABLE"`. Gate declarado + checker
   ausente → `"UNAVAILABLE"`, fail-closed: NUNCA
   `COMPLETED`/`VERIFIED`, NUNCA reexecuta `task_runner`, outcome
   preservado (`OUTCOME_AVAILABLE_PENDING_GATE`) para uma chamada
   futura com checker real resolver.
2. A reivindicação de `OUTCOME_AVAILABLE_PENDING_GATE` agora
   TRANSICIONA `status` para `'GATE_IN_PROGRESS'` (compare-and-set
   genuíno) — uma segunda instância rodando a MESMA instrução SQL
   encontra `status` já diferente e `rowcount == 0`, nunca
   reivindicando de novo.
"""


def _fresh_tracker_and_queue(db_path=None):
    return MissionTracker(db_path=db_path), JobQueue(db_path=db_path)


class QualityGateFailClosedWithoutCheckerTests(unittest.TestCase):
    """Item 1 — gate declarado + checker ausente = fail-closed."""

    def test_resumption_without_checker_never_reaches_verified(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        tracker, queue = _fresh_tracker_and_queue()
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)

        report1 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_no_checker", timeout_per_task=0.05,
            quality_gate_checker=lambda t, o: False,
        )
        self.assertEqual(report1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        time.sleep(0.4)

        # retomada SEM checker -- deve ser fail-closed
        report2 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_no_checker", timeout_per_task=5.0,
        )

        self.assertEqual(call_count["n"], 1, "runner foi reexecutado")
        self.assertEqual(report2.result_for("t1").status, "QUALITY_GATE_UNAVAILABLE")
        self.assertNotEqual(report2.result_for("t1").status, "VERIFIED")
        mission = tracker.get("m_no_checker", "t1")
        self.assertNotEqual(mission.status, MissionStatus.VERIFIED)

        executor.wait_for_inflight_completion(timeout=1.0)

    def test_later_call_with_real_checker_still_resolves_correctly(self):
        """O outcome nunca é perdido — uma chamada POSTERIOR com o
        checker real disponível resolve corretamente, sem reexecutar."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])
        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        tracker, queue = _fresh_tracker_and_queue()
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)

        executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_later_checker", timeout_per_task=0.05,
        )
        time.sleep(0.4)
        report2 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_later_checker", timeout_per_task=5.0,
        )
        self.assertEqual(report2.result_for("t1").status, "QUALITY_GATE_UNAVAILABLE")

        report3 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_later_checker", timeout_per_task=5.0,
            quality_gate_checker=lambda t, o: True,
        )
        self.assertEqual(call_count["n"], 1, "runner foi reexecutado")
        self.assertEqual(report3.result_for("t1").status, "VERIFIED")

        executor.wait_for_inflight_completion(timeout=1.0)

    def test_fresh_execution_without_gate_declared_unaffected(self):
        """Sem `quality_gate` declarado, o comportamento nunca muda —
        implicitamente aprovado como sempre."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1"}])
        tracker, queue = _fresh_tracker_and_queue()
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)
        report = executor.execute_plan(plan, task_runner=lambda t: {"ok": True}, actor="teste")
        self.assertEqual(report.result_for("t1").status, "VERIFIED")

    def test_fresh_execution_within_timeout_also_fail_closed_without_checker(self):
        """Mesmo uma execução DENTRO do timeout (nunca tardia) — com
        gate declarado mas checker ausente — nunca aprova
        implicitamente."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g"}])
        tracker, queue = _fresh_tracker_and_queue()
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)
        report = executor.execute_plan(plan, task_runner=lambda t: {"ok": True}, actor="teste", timeout_per_task=5.0)
        self.assertEqual(report.result_for("t1").status, "QUALITY_GATE_UNAVAILABLE")
        self.assertNotEqual(report.result_for("t1").status, "VERIFIED")


class ExclusiveGateClaimSqliteRaceTests(unittest.TestCase):
    """Item 2 — duas instâncias SQLite genuinamente concorrentes
    disputando o mesmo outcome tardio pendente de gate."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _produce_pending_gate_outcome(self, plan, runner, gate):
        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4)
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_race", timeout_per_task=0.05,
            quality_gate_checker=gate,
        )
        self.assertEqual(report.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        time.sleep(0.35)  # thread original termina de verdade -- outcome fica pendente de gate
        return executor

    def test_two_real_sqlite_instances_race_gate_runs_exactly_once_zero_exceptions(self):
        """Cenário EXATO reportado: duas instâncias SQLite reais
        (conexões/objetos distintos, sem nenhum estado Python
        compartilhado além do arquivo) disputando o mesmo outcome
        pendente de gate."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        def runner(task):
            time.sleep(0.2)
            return {"ok": True}

        gate_calls = {"n": 0}
        lock = threading.Lock()

        def gate_approves(task, outcome):
            with lock:
                gate_calls["n"] += 1
            return True

        original_executor = self._produce_pending_gate_outcome(plan, runner, gate_approves)

        tracker_a, queue_a = _fresh_tracker_and_queue(self.db_path)
        executor_a = GraphExecutor(mission_tracker=tracker_a, job_queue=queue_a, db_path=self.db_path, max_workers=4)
        tracker_b, queue_b = _fresh_tracker_and_queue(self.db_path)
        executor_b = GraphExecutor(mission_tracker=tracker_b, job_queue=queue_b, db_path=self.db_path, max_workers=4)

        results = {}
        errors = {}

        def run(label, executor):
            try:
                results[label] = executor.execute_plan(
                    plan, task_runner=runner, actor="teste", mission_id="m_race", timeout_per_task=5.0,
                    quality_gate_checker=gate_approves,
                )
            except Exception as exc:
                errors[label] = exc

        t_a = threading.Thread(target=run, args=("a", executor_a))
        t_b = threading.Thread(target=run, args=("b", executor_b))
        t_a.start()
        t_b.start()
        t_a.join(timeout=10.0)
        t_b.join(timeout=10.0)

        # ZERO excecoes de concorrencia (ex: ConcurrentMissionUpdate)
        self.assertEqual(errors, {}, f"exceções de concorrência não tratadas: {errors}")
        # gate executado EXATAMENTE 1 vez
        self.assertEqual(gate_calls["n"], 1, f"gate chamado {gate_calls['n']}x — reivindicação não foi exclusiva")
        # ambos os chamadores obtiveram uma resposta válida (nunca uma excecao)
        for label in ("a", "b"):
            self.assertIn(label, results, f"{label} não completou (provável exceção não capturada)")
            self.assertIn(
                results[label].result_for("t1").status,
                ("VERIFIED", "SKIPPED_ALREADY_DONE", "TIMED_OUT_UNKNOWN_OUTCOME"),
            )
        # estado final CONSISTENTE
        final_mission = tracker_a.get("m_race", "t1")
        self.assertEqual(final_mission.status, MissionStatus.VERIFIED)

        original_executor.wait_for_inflight_completion(timeout=1.0)

    def test_two_real_sqlite_instances_race_runner_never_rerun(self):
        """O mesmo cenário de corrida, mas focado em confirmar que
        `task_runner` nunca é reexecutado (o outcome tardio já é o
        efeito real, nunca duplicado)."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}
        lock = threading.Lock()

        def runner(task):
            with lock:
                call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        original_executor = self._produce_pending_gate_outcome(plan, runner, lambda t, o: True)
        call_count["n"] = 0  # a chamada inicial (setup) já contou 1 -- reseta antes da corrida real

        tracker_a, queue_a = _fresh_tracker_and_queue(self.db_path)
        executor_a = GraphExecutor(mission_tracker=tracker_a, job_queue=queue_a, db_path=self.db_path, max_workers=4)
        tracker_b, queue_b = _fresh_tracker_and_queue(self.db_path)
        executor_b = GraphExecutor(mission_tracker=tracker_b, job_queue=queue_b, db_path=self.db_path, max_workers=4)

        def run(executor):
            executor.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_race2", timeout_per_task=5.0,
                quality_gate_checker=lambda t, o: True,
            )

        t_a = threading.Thread(target=run, args=(executor_a,))
        t_b = threading.Thread(target=run, args=(executor_b,))
        t_a.start()
        t_b.start()
        t_a.join(timeout=10.0)
        t_b.join(timeout=10.0)

        with lock:
            self.assertEqual(call_count["n"], 1, f"task_runner reexecutado {call_count['n']}x")

        original_executor.wait_for_inflight_completion(timeout=1.0)

    def test_loser_of_claim_race_observes_gate_in_progress_never_evaluates(self):
        """O perdedor da corrida pela reivindicação nunca chega a
        avaliar o gate — observa um estado de "em andamento" e
        devolve imediatamente, nunca girando."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        def runner(task):
            time.sleep(0.2)
            return {"ok": True}

        # gate lento o suficiente para o vencedor ainda estar
        # avaliando quando o perdedor tenta reivindicar
        def slow_gate(task, outcome):
            time.sleep(0.3)
            return True

        original_executor = self._produce_pending_gate_outcome(plan, runner, slow_gate)

        tracker_a, queue_a = _fresh_tracker_and_queue(self.db_path)
        executor_a = GraphExecutor(mission_tracker=tracker_a, job_queue=queue_a, db_path=self.db_path, max_workers=4)
        tracker_b, queue_b = _fresh_tracker_and_queue(self.db_path)
        executor_b = GraphExecutor(mission_tracker=tracker_b, job_queue=queue_b, db_path=self.db_path, max_workers=4)

        results = {}

        def run_a():
            results["a"] = executor_a.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_race3", timeout_per_task=5.0,
                quality_gate_checker=slow_gate,
            )

        def run_b():
            time.sleep(0.05)  # A comeca primeiro e ganha a reivindicacao
            results["b"] = executor_b.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_race3", timeout_per_task=0.5,
                quality_gate_checker=slow_gate,
            )

        t_a = threading.Thread(target=run_a)
        t_b = threading.Thread(target=run_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=10.0)
        t_b.join(timeout=10.0)

        # B perdeu a corrida -- nunca deveria ter avaliado o gate,
        # apenas observado o estado em andamento
        self.assertEqual(results["b"].result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        original_executor.wait_for_inflight_completion(timeout=1.0)
        executor_a.wait_for_inflight_completion(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
