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
MICROCORREÇÃO — ATLAS EXPANSION 1: CONCLUSÃO TARDIA NUNCA EQUIVALE A
CONCLUSÃO VALIDADA.

Falha real reproduzida por auditoria independente: após
`TIMED_OUT_UNKNOWN_OUTCOME`, se a thread de fundo terminasse depois
com SUCESSO, `_run_with_timeout_tracked()` resolvia o lease
DIRETAMENTE como `COMPLETED` — sem nunca consultar o Quality Gate. Em
restart/nova instância, `_finalize_cached_completed()` usava esse
resultado e marcava a missão `VERIFIED` chamando
`mission_tracker.verify(..., quality_gate_verdict=None)`
incondicionalmente, ignorando por completo `quality_gate_checker`.

Corrigido: uma conclusão tardia bem-sucedida agora resolve o lease
como `"OUTCOME_AVAILABLE_PENDING_GATE"` — nunca `"COMPLETED"`
diretamente. Na retomada (mesma ou outra instância), o outcome é
reivindicado atomicamente para avaliação, o Quality Gate é
OBRIGATORIAMENTE avaliado sobre ele antes de qualquer transição para
`COMPLETED`/`VERIFIED`. `task_runner` NUNCA é reexecutado para obter
esse resultado (o outcome tardio já é o efeito real). Se o gate
rejeita, segue o fluxo normal de rejeição/retry/falha (uma eventual
nova tentativa é uma chamada FRESCA e distinta de `task_runner`,
nunca um reaproveitamento do outcome tardio rejeitado).
"""


def _fresh_tracker_and_queue(db_path=None):
    return MissionTracker(db_path=db_path), JobQueue(db_path=db_path)


class DelayedOutcomeGateRejectedTests(unittest.TestCase):
    """Cenário 1 exigido: timeout + resultado tardio + retomada +
    gate REJECT — runner executa 1x, gate executa, missão NUNCA
    termina VERIFIED."""

    def test_memory_same_instance_gate_rejects_delayed_outcome(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}
        gate_calls = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        def gate_rejects(task, outcome):
            gate_calls["n"] += 1
            return False

        tracker, queue = _fresh_tracker_and_queue()
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)

        report1 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_reject", timeout_per_task=0.05,
            quality_gate_checker=gate_rejects,
        )
        self.assertEqual(report1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        time.sleep(0.4)  # thread original termina de verdade, COM SUCESSO

        report2 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_reject", timeout_per_task=5.0,
            quality_gate_checker=gate_rejects,
        )

        self.assertEqual(call_count["n"], 1, "runner deveria ter executado exatamente 1 vez")
        self.assertGreaterEqual(gate_calls["n"], 1, "o Quality Gate nunca foi consultado")
        self.assertNotEqual(report2.result_for("t1").status, "VERIFIED")
        self.assertEqual(report2.result_for("t1").status, "FAILED")
        mission = tracker.get("m_reject", "t1")
        self.assertNotEqual(mission.status, MissionStatus.VERIFIED)
        self.assertEqual(mission.status, MissionStatus.FAILED)

        executor.wait_for_inflight_completion(timeout=1.0)

    def test_durable_new_instance_after_restart_gate_rejects_delayed_outcome(self):
        """Mesmo cenário, mas com uma instância GENUINAMENTE nova
        (simula restart) descobrindo o outcome tardio via SQLite."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            planner = AdvancedPlanner()
            plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

            call_count = {"n": 0}

            def runner(task):
                call_count["n"] += 1
                time.sleep(0.2)
                return {"ok": True}

            def gate_rejects(task, outcome):
                return False

            tracker_1, queue_1 = _fresh_tracker_and_queue(db_path)
            executor_1 = GraphExecutor(mission_tracker=tracker_1, job_queue=queue_1, db_path=db_path, max_workers=4)
            report_1 = executor_1.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_restart_reject", timeout_per_task=0.05,
                quality_gate_checker=gate_rejects,
            )
            self.assertEqual(report_1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

            time.sleep(0.4)  # thread original termina de verdade, COM SUCESSO

            # "restart" -- nova instância, sem nenhuma referência à original
            tracker_2, queue_2 = _fresh_tracker_and_queue(db_path)
            executor_2 = GraphExecutor(mission_tracker=tracker_2, job_queue=queue_2, db_path=db_path, max_workers=4)
            report_2 = executor_2.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_restart_reject", timeout_per_task=5.0,
                quality_gate_checker=gate_rejects,
            )

            self.assertEqual(call_count["n"], 1, "runner deveria ter executado exatamente 1 vez, mesmo após restart")
            self.assertNotEqual(report_2.result_for("t1").status, "VERIFIED")
            self.assertEqual(report_2.result_for("t1").status, "FAILED")
            mission = tracker_2.get("m_restart_reject", "t1")
            self.assertNotEqual(mission.status, MissionStatus.VERIFIED)

            executor_1.wait_for_inflight_completion(timeout=1.0)
        finally:
            db_module.remove_db_file(db_path)

    def test_gate_rejection_with_retries_available_triggers_genuinely_new_attempt(self):
        """Rejeição de um outcome tardio com retries disponíveis deve
        gerar uma NOVA execução real de `task_runner` (nunca reusar o
        outcome tardio rejeitado) — e essa nova execução pode
        eventualmente convergir se o gate mudar de ideia (ex: baseado
        em conteúdo diferente)."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 3}])

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            if call_count["n"] == 1:
                time.sleep(0.2)  # 1a chamada -- estoura o timeout, termina tarde
            return {"tentativa": call_count["n"]}

        def gate_only_approves_second_attempt(task, outcome):
            return outcome["tentativa"] >= 2

        tracker, queue = _fresh_tracker_and_queue()
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)

        report1 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_retry_after_reject", timeout_per_task=0.05,
            quality_gate_checker=gate_only_approves_second_attempt,
        )
        self.assertEqual(report1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        self.assertEqual(call_count["n"], 1)

        time.sleep(0.4)  # 1a chamada termina de verdade -- tentativa=1, gate rejeita

        report2 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_retry_after_reject", timeout_per_task=5.0,
            quality_gate_checker=gate_only_approves_second_attempt,
        )
        # gate rejeitou a tentativa=1 (tardia), retentou -- NOVA
        # chamada real (tentativa=2), que o gate aprova.
        self.assertEqual(call_count["n"], 2, "deveria ter feito uma nova tentativa real, nunca reusado o outcome tardio")
        self.assertEqual(report2.result_for("t1").status, "VERIFIED")

        executor.wait_for_inflight_completion(timeout=1.0)


class DelayedOutcomeGateApprovedTests(unittest.TestCase):
    """Cenário 2 exigido: mesmo cenário com gate APPROVED — runner
    executa 1x e finaliza corretamente sem rerun."""

    def test_memory_same_instance_gate_approves_delayed_outcome(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        def gate_approves(task, outcome):
            return True

        tracker, queue = _fresh_tracker_and_queue()
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)

        report1 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_approve", timeout_per_task=0.05,
            quality_gate_checker=gate_approves,
        )
        self.assertEqual(report1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        time.sleep(0.4)

        report2 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_approve", timeout_per_task=5.0,
            quality_gate_checker=gate_approves,
        )

        self.assertEqual(call_count["n"], 1, "runner deveria ter executado exatamente 1 vez")
        self.assertEqual(report2.result_for("t1").status, "VERIFIED")
        mission = tracker.get("m_approve", "t1")
        self.assertEqual(mission.status, MissionStatus.VERIFIED)

        executor.wait_for_inflight_completion(timeout=1.0)

    def test_durable_new_instance_after_restart_gate_approves_delayed_outcome(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            planner = AdvancedPlanner()
            plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

            call_count = {"n": 0}

            def runner(task):
                call_count["n"] += 1
                time.sleep(0.2)
                return {"ok": True}

            def gate_approves(task, outcome):
                return True

            tracker_1, queue_1 = _fresh_tracker_and_queue(db_path)
            executor_1 = GraphExecutor(mission_tracker=tracker_1, job_queue=queue_1, db_path=db_path, max_workers=4)
            report_1 = executor_1.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_restart_approve", timeout_per_task=0.05,
                quality_gate_checker=gate_approves,
            )
            self.assertEqual(report_1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

            time.sleep(0.4)

            tracker_2, queue_2 = _fresh_tracker_and_queue(db_path)
            executor_2 = GraphExecutor(mission_tracker=tracker_2, job_queue=queue_2, db_path=db_path, max_workers=4)
            report_2 = executor_2.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_restart_approve", timeout_per_task=5.0,
                quality_gate_checker=gate_approves,
            )

            self.assertEqual(call_count["n"], 1, "runner deveria ter executado exatamente 1 vez, mesmo após restart")
            self.assertEqual(report_2.result_for("t1").status, "VERIFIED")
            mission = tracker_2.get("m_restart_approve", "t1")
            self.assertEqual(mission.status, MissionStatus.VERIFIED)

            executor_1.wait_for_inflight_completion(timeout=1.0)
        finally:
            db_module.remove_db_file(db_path)

    def test_no_quality_gate_applicable_finalizes_normally(self):
        """Sem `quality_gate` na tarefa, um outcome tardio finaliza
        normalmente (comportamento já aprovado, sem regressão)."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        tracker, queue = _fresh_tracker_and_queue()
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)

        report1 = executor.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m_no_gate", timeout_per_task=0.05)
        self.assertEqual(report1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        time.sleep(0.4)

        report2 = executor.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m_no_gate", timeout_per_task=5.0)
        self.assertEqual(call_count["n"], 1)
        self.assertEqual(report2.result_for("t1").status, "VERIFIED")

        executor.wait_for_inflight_completion(timeout=1.0)


class PendingGateAtomicClaimTests(unittest.TestCase):
    """A reivindicação de um outcome `OUTCOME_AVAILABLE_PENDING_GATE`
    para avaliação é atômica — duas instâncias descobrindo o mesmo
    outcome tardio simultaneamente nunca avaliam/finalizam em
    duplicidade real (o gate roda pelo menos uma vez, de forma
    determinística por quem ganha a reivindicação)."""

    def test_two_instances_racing_to_claim_pending_gate_outcome_never_rerun_task_runner(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        call_count = {"n": 0}
        lock = threading.Lock()

        def runner(task):
            with lock:
                call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        def gate_approves(task, outcome):
            return True

        tracker, queue = _fresh_tracker_and_queue()
        executor_a = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)
        executor_b = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)

        report_first = executor_a.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_race_gate", timeout_per_task=0.05,
            quality_gate_checker=gate_approves,
        )
        self.assertEqual(report_first.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        time.sleep(0.4)  # outcome tardio disponível, pendente de gate

        results = {}

        def run_a():
            results["a"] = executor_a.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_race_gate", timeout_per_task=5.0,
                quality_gate_checker=gate_approves,
            )

        def run_b():
            results["b"] = executor_b.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_race_gate", timeout_per_task=5.0,
                quality_gate_checker=gate_approves,
            )

        t_a = threading.Thread(target=run_a)
        t_b = threading.Thread(target=run_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=5.0)
        t_b.join(timeout=5.0)

        with lock:
            self.assertEqual(call_count["n"], 1, "task_runner foi reexecutado apesar do outcome tardio já disponível")

        # cada chamador reporta "VERIFIED" (se foi quem finalizou ou
        # observou a finalização via o lease), "SKIPPED_ALREADY_DONE"
        # (se, pela ordem real de execução das threads, encontrou a
        # missão JÁ VERIFIED pelo caminho rápido de retomada existente
        # desde a primeira versão desta funcionalidade), OU
        # "TIMED_OUT_UNKNOWN_OUTCOME" (se perdeu a corrida pela
        # reivindicação exclusiva do lease e observou `GATE_IN_PROGRESS`
        # ainda dentro do TTL — o MESMO comportamento correto e já
        # estabelecido de qualquer perdedor de uma corrida por lease
        # em toda esta série de correções, ver
        # `test_loser_of_claim_race_observes_gate_in_progress_never_evaluates`
        # em `test_microcorrecao_atlas_expansion1_gate_failclosed_race.py`)
        # -- nunca FAILED, e o estado final da missão é sempre
        # VERIFIED, independente de qual caminho cada um seguiu.
        for label in ("a", "b"):
            self.assertIn(
                results[label].result_for("t1").status,
                ("VERIFIED", "SKIPPED_ALREADY_DONE", "TIMED_OUT_UNKNOWN_OUTCOME"),
                f"resultado de {label} inesperado: {results[label].result_for('t1').status}",
            )

        mission = tracker.get("m_race_gate", "t1")
        self.assertEqual(mission.status, MissionStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main()
