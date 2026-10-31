import os
import tempfile
import time
import unittest

from atlas.advanced_planning import AdvancedPlanner
from core.coordination.mission import MissionStatus, MissionTracker
from core.jobs.queue import JobQueue
from core.models import db as db_module
from atlas.orchestration.graph_executor import GraphExecutor

"""
MICROCORREÇÃO — ATLAS EXPANSION 1: LEASE NUNCA ÓRFÃO EM
`GATE_IN_PROGRESS` QUANDO O CHECKER LEVANTA EXCEÇÃO.

Falha real reproduzida por auditoria independente: depois que um
outcome tardio era reivindicado (lease movido para
`GATE_IN_PROGRESS`), se `quality_gate_checker` levantasse exceção
durante `_finalize_pending_gate_outcome()`, o lease ficava preso
PARA SEMPRE em `GATE_IN_PROGRESS` — uma instância FUTURA com um
checker saudável encontrava esse estado exclusivo bloqueante e
retornava `TIMED_OUT_UNKNOWN_OUTCOME` indefinidamente, nunca
conseguindo finalizar o outcome já obtido de verdade pelo
`task_runner`.

Corrigido: a avaliação do gate é envolvida em tratamento de exceção
— se o checker levantar, o lease é devolvido ATOMICAMENTE para
`OUTCOME_AVAILABLE_PENDING_GATE` (usando o token atual, nunca
descartado), o erro é registrado (log de aviso + persistido no
próprio lease) e um novo status explícito e retryable
`QUALITY_GATE_EVALUATION_ERROR` é devolvido — nunca `COMPLETED`/
`VERIFIED`, nunca reexecuta `task_runner`.
"""


def _fresh_tracker_and_queue(db_path=None):
    return MissionTracker(db_path=db_path), JobQueue(db_path=db_path)


class LeaseNeverOrphanedOnCheckerExceptionTests(unittest.TestCase):
    """Cenário exato exigido: timeout → outcome tardio → restart →
    checker lança exceção → nova instância com checker saudável →
    runner permanece 1x, gate executa de novo e finaliza."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _produce_pending_gate_outcome(self, plan, runner, mission_id="m_orphan"):
        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4)
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id=mission_id, timeout_per_task=0.05,
        )
        self.assertEqual(report.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        time.sleep(0.35)
        return executor

    def test_crashing_checker_then_healthy_checker_approves_runner_once(self):
        """Variante APPROVE: checker crasha; instância seguinte com
        checker saudável (que APROVA) finaliza corretamente."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        original_executor = self._produce_pending_gate_outcome(plan, runner, "m_orphan")

        # nova instância -- checker CRASHA
        tracker_2, queue_2 = _fresh_tracker_and_queue(self.db_path)
        executor_2 = GraphExecutor(mission_tracker=tracker_2, job_queue=queue_2, db_path=self.db_path, max_workers=4)

        def crashing_checker(task, outcome):
            raise RuntimeError("infra indisponível")

        report_2 = executor_2.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_orphan", timeout_per_task=5.0,
            quality_gate_checker=crashing_checker,
        )
        self.assertEqual(report_2.result_for("t1").status, "QUALITY_GATE_EVALUATION_ERROR")

        # lease NUNCA órfão -- confirma diretamente no banco
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM graph_executor_task_leases WHERE mission_id=? AND task_id=?", ("m_orphan", "t1"),
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "OUTCOME_AVAILABLE_PENDING_GATE", "lease ficou órfão em GATE_IN_PROGRESS")

        # nova instância -- checker SAUDÁVEL, APROVA
        tracker_3, queue_3 = _fresh_tracker_and_queue(self.db_path)
        executor_3 = GraphExecutor(mission_tracker=tracker_3, job_queue=queue_3, db_path=self.db_path, max_workers=4)
        report_3 = executor_3.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_orphan", timeout_per_task=5.0,
            quality_gate_checker=lambda t, o: True,
        )

        self.assertEqual(call_count["n"], 1, "runner foi reexecutado")
        self.assertEqual(report_3.result_for("t1").status, "VERIFIED")
        mission = tracker_3.get("m_orphan", "t1")
        self.assertEqual(mission.status, MissionStatus.VERIFIED)

        original_executor.wait_for_inflight_completion(timeout=1.0)

    def test_crashing_checker_then_healthy_checker_rejects_runner_once(self):
        """Variante REJECT: checker crasha; instância seguinte com
        checker saudável (que REJEITA) processa a rejeição
        corretamente — sem retries, `FAILED` definitivo; `runner`
        nunca reexecutado para obter o outcome tardio em si."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        original_executor = self._produce_pending_gate_outcome(plan, runner, "m_orphan_reject")

        tracker_2, queue_2 = _fresh_tracker_and_queue(self.db_path)
        executor_2 = GraphExecutor(mission_tracker=tracker_2, job_queue=queue_2, db_path=self.db_path, max_workers=4)

        def crashing_checker(task, outcome):
            raise ValueError("checker temporariamente indisponível")

        report_2 = executor_2.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_orphan_reject", timeout_per_task=5.0,
            quality_gate_checker=crashing_checker,
        )
        self.assertEqual(report_2.result_for("t1").status, "QUALITY_GATE_EVALUATION_ERROR")

        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM graph_executor_task_leases WHERE mission_id=? AND task_id=?",
            ("m_orphan_reject", "t1"),
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "OUTCOME_AVAILABLE_PENDING_GATE")

        tracker_3, queue_3 = _fresh_tracker_and_queue(self.db_path)
        executor_3 = GraphExecutor(mission_tracker=tracker_3, job_queue=queue_3, db_path=self.db_path, max_workers=4)
        report_3 = executor_3.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_orphan_reject", timeout_per_task=5.0,
            quality_gate_checker=lambda t, o: False,
        )

        self.assertEqual(call_count["n"], 1, "runner foi reexecutado")
        self.assertEqual(report_3.result_for("t1").status, "FAILED")
        mission = tracker_3.get("m_orphan_reject", "t1")
        self.assertEqual(mission.status, MissionStatus.FAILED)
        self.assertNotEqual(mission.status, MissionStatus.VERIFIED)

        original_executor.wait_for_inflight_completion(timeout=1.0)

    def test_reject_variant_with_retries_available_triggers_fresh_attempt(self):
        """Variante REJECT com retries disponíveis — depois que o
        checker inicialmente crasha e uma chamada seguinte rejeita de
        verdade, uma tentativa NOVA e genuína deve ocorrer (nunca
        reaproveitando o outcome tardio original)."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 3}])

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            if call_count["n"] == 1:
                time.sleep(0.2)  # 1a chamada -- estoura o timeout de propósito
            return {"tentativa": call_count["n"]}

        original_executor = self._produce_pending_gate_outcome(plan, runner, "m_orphan_retry")

        tracker_2, queue_2 = _fresh_tracker_and_queue(self.db_path)
        executor_2 = GraphExecutor(mission_tracker=tracker_2, job_queue=queue_2, db_path=self.db_path, max_workers=4)

        def crashing_checker(task, outcome):
            raise RuntimeError("checker crasha")

        report_2 = executor_2.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_orphan_retry", timeout_per_task=5.0,
            quality_gate_checker=crashing_checker,
        )
        self.assertEqual(report_2.result_for("t1").status, "QUALITY_GATE_EVALUATION_ERROR")
        self.assertEqual(call_count["n"], 1)

        # checker saudável, mas só aprova a partir da 2a tentativa
        def gate_approves_from_second(task, outcome):
            return outcome["tentativa"] >= 2

        tracker_3, queue_3 = _fresh_tracker_and_queue(self.db_path)
        executor_3 = GraphExecutor(mission_tracker=tracker_3, job_queue=queue_3, db_path=self.db_path, max_workers=4)
        report_3 = executor_3.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_orphan_retry", timeout_per_task=5.0,
            quality_gate_checker=gate_approves_from_second,
        )

        # o outcome tardio (tentativa=1) foi rejeitado -- gerou uma
        # NOVA tentativa real (tentativa=2), que o gate aprova.
        self.assertEqual(call_count["n"], 2, "deveria ter feito uma nova tentativa real")
        self.assertEqual(report_3.result_for("t1").status, "VERIFIED")

        original_executor.wait_for_inflight_completion(timeout=1.0)

    def test_checker_exception_logged_and_persisted_in_lease_error_column(self):
        """A telemetria (registro do erro) é confirmada diretamente:
        o motivo real do erro fica persistido na própria linha do
        lease, nunca descartado silenciosamente."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        def runner(task):
            time.sleep(0.2)
            return {"ok": True}

        original_executor = self._produce_pending_gate_outcome(plan, runner, "m_telemetry")

        tracker_2, queue_2 = _fresh_tracker_and_queue(self.db_path)
        executor_2 = GraphExecutor(mission_tracker=tracker_2, job_queue=queue_2, db_path=self.db_path, max_workers=4)

        def crashing_checker(task, outcome):
            raise RuntimeError("motivo especifico do erro simulado")

        executor_2.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_telemetry", timeout_per_task=5.0,
            quality_gate_checker=crashing_checker,
        )

        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, error FROM graph_executor_task_leases WHERE mission_id=? AND task_id=?",
            ("m_telemetry", "t1"),
        ).fetchone()
        conn.close()

        self.assertEqual(row["status"], "OUTCOME_AVAILABLE_PENDING_GATE")
        self.assertIn("motivo especifico do erro simulado", row["error"])

        original_executor.wait_for_inflight_completion(timeout=1.0)


class FreshExecutionCheckerExceptionConsistencyTests(unittest.TestCase):
    """Confirma que a MESMA proteção se aplica ao caminho de execução
    FRESCA (dentro do timeout), não só ao outcome tardio — mesma
    classe de falha, mesmo helper de tratamento."""

    def test_fresh_execution_checker_exception_never_orphans_and_resolves_later(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            return {"ok": True}

        tracker, queue = _fresh_tracker_and_queue()
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)

        def crashing_checker(task, outcome):
            raise RuntimeError("crash na execucao fresca")

        report1 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_fresh", timeout_per_task=5.0,
            quality_gate_checker=crashing_checker,
        )
        self.assertEqual(report1.result_for("t1").status, "QUALITY_GATE_EVALUATION_ERROR")
        self.assertEqual(call_count["n"], 1)

        report2 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_fresh", timeout_per_task=5.0,
            quality_gate_checker=lambda t, o: True,
        )
        self.assertEqual(call_count["n"], 1, "runner foi reexecutado")
        self.assertEqual(report2.result_for("t1").status, "VERIFIED")

        executor.wait_for_inflight_completion(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
