import gc
import threading
import time
import unittest

from atlas.advanced_planning import AdvancedPlanner, CyclicDependencyError, UnknownDependencyError
from atlas.escalation import EscalationDesk
from atlas.orchestration.graph_executor import (
    GraphExecutor, LoopLimitExceeded, LoopSpec, PlanCancelled, run_loop,
)
from core.coordination.mission import MissionStatus, MissionTracker
from core.jobs.queue import JobQueue
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

"""
ATLAS EXPANSION 1 — ORQUESTRAÇÃO EXECUTÁVEL.

Testes formais e permanentes cobrindo exatamente os cenários
obrigatórios: DAG válido, ciclo rejeitado, execução paralela
comprovada por tempo/concorrência, falha parcial, retry, timeout,
cancelamento, shutdown limpo, retomada sem duplicidade.

Nenhum destes testes mocka o mecanismo de paralelismo/tempo em si —
usam threads reais, `threading.Barrier` real (prova determinística
de concorrência: se a execução fosse sequencial, o teste travaria no
Barrier até seu próprio timeout, transformando um bug real num FAIL
determinístico, nunca um teste que "passa por sorte"), e medição real
de tempo de processo para confirmar shutdown limpo.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


def _fresh_executor(*, max_workers: int = 4, with_escalation: bool = False):
    tracker = MissionTracker()
    queue = JobQueue()
    desk = EscalationDesk(AuditTrail()) if with_escalation else None
    executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, escalation_desk=desk, max_workers=max_workers)
    return executor, tracker, queue, desk


class ValidDagExecutionTests(unittest.TestCase):
    """DAG válido — execução completa, dependências respeitadas."""

    def test_simple_dag_executes_all_tasks_verified(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="dag valido", tasks=[
            {"task_id": "a", "description": "a"},
            {"task_id": "b", "description": "b", "depends_on": ["a"]},
            {"task_id": "c", "description": "c", "depends_on": ["a", "b"]},
        ])
        executor, tracker, queue, _ = _fresh_executor()

        execution_order = []
        lock = threading.Lock()

        def runner(task):
            with lock:
                execution_order.append(task.task_id)
            return {"ok": True}

        report = executor.execute_plan(plan, task_runner=runner, actor="teste")

        self.assertTrue(report.success)
        self.assertEqual(len(report.task_results), 3)
        for r in report.task_results:
            self.assertEqual(r.status, "VERIFIED")
        # ordem respeitada: 'a' sempre antes de 'b' e 'c'; 'b' antes de 'c'
        self.assertLess(execution_order.index("a"), execution_order.index("b"))
        self.assertLess(execution_order.index("b"), execution_order.index("c"))

    def test_missions_reach_verified_state_in_tracker(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1"}])
        executor, tracker, queue, _ = _fresh_executor()

        executor.execute_plan(plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m1")

        mission = tracker.get("m1", "t1")
        self.assertEqual(mission.status, MissionStatus.VERIFIED)


class CycleRejectionTests(unittest.TestCase):
    """Ciclo rejeitado — nunca executa um plano inválido."""

    def test_direct_cycle_rejected_at_construction(self):
        planner = AdvancedPlanner()
        with self.assertRaises(CyclicDependencyError):
            planner.build_plan(objective="ciclo", tasks=[
                {"task_id": "a", "description": "a", "depends_on": ["b"]},
                {"task_id": "b", "description": "b", "depends_on": ["a"]},
            ])

    def test_indirect_cycle_rejected(self):
        planner = AdvancedPlanner()
        with self.assertRaises(CyclicDependencyError):
            planner.build_plan(objective="ciclo indireto", tasks=[
                {"task_id": "a", "description": "a", "depends_on": ["c"]},
                {"task_id": "b", "description": "b", "depends_on": ["a"]},
                {"task_id": "c", "description": "c", "depends_on": ["b"]},
            ])

    def test_unknown_dependency_rejected(self):
        planner = AdvancedPlanner()
        with self.assertRaises(UnknownDependencyError):
            planner.build_plan(objective="dep desconhecida", tasks=[
                {"task_id": "a", "description": "a", "depends_on": ["nao_existe"]},
            ])

    def test_executor_never_runs_a_cyclic_plan(self):
        """O executor nunca chega a rodar nenhuma tarefa de um plano
        cíclico — a rejeição acontece na CONSTRUÇÃO do plano, antes
        de `execute_plan()` sequer ser chamado."""
        planner = AdvancedPlanner()
        calls = []
        with self.assertRaises(CyclicDependencyError):
            plan = planner.build_plan(objective="x", tasks=[
                {"task_id": "a", "description": "a", "depends_on": ["b"]},
                {"task_id": "b", "description": "b", "depends_on": ["a"]},
            ])
            executor, _, _, _ = _fresh_executor()
            executor.execute_plan(plan, task_runner=lambda t: calls.append(t.task_id), actor="teste")
        self.assertEqual(calls, [])


class ParallelExecutionProofTests(unittest.TestCase):
    """Execução paralela comprovada por CONCORRÊNCIA REAL (Barrier —
    prova determinística) e por TEMPO (grupo de N tarefas de duração D
    completa em ~D, nunca N×D)."""

    def test_deterministic_concurrency_via_barrier(self):
        """Se a execução fosse sequencial (bug), este teste TRAVARIA
        no Barrier até seu timeout — nunca um falso positivo."""
        N = 6
        barrier = threading.Barrier(N, timeout=5.0)
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="paralelo", tasks=[
            {"task_id": f"p{i}", "description": f"p{i}"} for i in range(N)
        ])

        def runner(task):
            barrier.wait()  # trava para sempre (ate o timeout) se nao for genuinamente paralelo
            return {"ok": True}

        executor, _, _, _ = _fresh_executor(max_workers=N)
        report = executor.execute_plan(plan, task_runner=runner, actor="teste")

        self.assertTrue(report.success)
        self.assertEqual(len(report.task_results), N)

    def test_group_duration_proves_real_concurrency_by_time(self):
        """N tarefas de ~0.3s cada, executadas EM PARALELO, devem
        levar ~0.3s no total (não N×0.3s) — prova por tempo,
        complementar à prova determinística acima."""
        N = 5
        task_duration = 0.3
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="tempo", tasks=[
            {"task_id": f"t{i}", "description": f"t{i}"} for i in range(N)
        ])

        def runner(task):
            time.sleep(task_duration)
            return {"ok": True}

        executor, _, _, _ = _fresh_executor(max_workers=N)
        start = time.time()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste")
        total_duration = time.time() - start

        self.assertTrue(report.success)
        # paralelo genuino: bem abaixo de N * task_duration (1.5s);
        # sequencial daria ~1.5s+. Margem generosa para não ser flaky.
        self.assertLess(total_duration, N * task_duration * 0.6)

    def test_sequential_groups_never_overlap(self):
        """Duas tarefas em grupos DIFERENTES (dependência real) NUNCA
        rodam ao mesmo tempo — o paralelismo é só DENTRO do mesmo
        nível, nunca entre níveis dependentes."""
        overlap_detected = threading.Event()
        state = {"group1_running": False}
        lock = threading.Lock()

        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="sem overlap", tasks=[
            {"task_id": "g1", "description": "g1"},
            {"task_id": "g2", "description": "g2", "depends_on": ["g1"]},
        ])

        def runner(task):
            if task.task_id == "g1":
                with lock:
                    state["group1_running"] = True
                time.sleep(0.15)
                with lock:
                    state["group1_running"] = False
            else:
                with lock:
                    if state["group1_running"]:
                        overlap_detected.set()
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste")

        self.assertTrue(report.success)
        self.assertFalse(overlap_detected.is_set())


class PartialFailureTests(unittest.TestCase):
    """Falha parcial — grupo com falha nunca deixa o próximo grupo
    iniciar (fail-closed)."""

    def test_group_failure_blocks_next_group(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="falha parcial", tasks=[
            {"task_id": "g1_ok", "description": "ok"},
            {"task_id": "g1_falha", "description": "falha", "max_retries": 0},
            {"task_id": "g2_nunca", "description": "nunca roda", "depends_on": ["g1_falha"]},
        ])

        def runner(task):
            if task.task_id == "g1_falha":
                raise RuntimeError("falha deliberada")
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste")

        self.assertTrue(report.failed)
        self.assertFalse(report.success)
        self.assertEqual(report.result_for("g1_ok").status, "VERIFIED")
        self.assertEqual(report.result_for("g1_falha").status, "FAILED")
        self.assertIsNone(report.result_for("g2_nunca"))  # nunca executado

    def test_successful_siblings_in_failed_group_still_verified(self):
        """Tarefas IRMÃS (mesmo grupo) que tiveram sucesso continuam
        `VERIFIED`, mesmo que outra do mesmo grupo tenha falhado —
        fail-closed é sobre o PRÓXIMO grupo, nunca reescreve um
        resultado já confirmado."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[
            {"task_id": "ok1", "description": "ok1"},
            {"task_id": "ok2", "description": "ok2"},
            {"task_id": "falha", "description": "falha", "max_retries": 0},
        ])

        def runner(task):
            if task.task_id == "falha":
                raise RuntimeError("x")
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste")

        self.assertEqual(report.result_for("ok1").status, "VERIFIED")
        self.assertEqual(report.result_for("ok2").status, "VERIFIED")
        self.assertEqual(report.result_for("falha").status, "FAILED")


class RetryTests(unittest.TestCase):
    """Retry real — via `JobQueue`, respeitando `max_retries`."""

    def test_task_succeeds_after_retries(self):
        counter = {"n": 0}
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="retry", tasks=[{"task_id": "rt", "description": "rt", "max_retries": 3}])

        def runner(task):
            counter["n"] += 1
            if counter["n"] < 3:
                raise RuntimeError(f"falha simulada {counter['n']}")
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste")

        self.assertTrue(report.success)
        self.assertEqual(report.result_for("rt").attempts, 3)
        self.assertEqual(counter["n"], 3)

    def test_task_fails_after_exhausting_retries(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "rt", "description": "rt", "max_retries": 2}])

        def runner(task):
            raise RuntimeError("sempre falha")

        executor, _, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste")

        self.assertTrue(report.failed)
        result = report.result_for("rt")
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.attempts, 3)  # max_retries=2 -> 3 tentativas totais

    def test_zero_retries_fails_on_first_attempt(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "rt", "description": "rt", "max_retries": 0}])
        attempts = {"n": 0}

        def runner(task):
            attempts["n"] += 1
            raise RuntimeError("falha")

        executor, _, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste")

        self.assertEqual(attempts["n"], 1)
        self.assertEqual(report.result_for("rt").status, "FAILED")


class TimeoutTests(unittest.TestCase):
    """Timeout real por tarefa — a orquestração desiste no tempo
    configurado, NUNCA espera a tarefa lenta terminar, e o PROCESSO
    consegue encerrar rapidamente mesmo com a thread órfã ainda
    rodando em segundo plano (correção central desta entrega)."""

    def test_slow_task_times_out_orchestration_gives_up_promptly(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="timeout", tasks=[{"task_id": "lento", "description": "l", "max_retries": 0}])

        def runner(task):
            time.sleep(1.5)  # BOUNDED -- nunca infinito, nunca arrisca hang de verdade
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        start = time.time()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste", timeout_per_task=0.15)
        duration = time.time() - start

        # MICROCORREÇÃO (nunca duplica efeito externo): um timeout
        # nunca é reportado como "FAILED" — isso liberaria retry
        # automático enquanto a execução anterior pode ainda estar
        # viva. O status honesto é "desfecho desconhecido".
        self.assertEqual(report.result_for("lento").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        self.assertIn("timeout", report.result_for("lento").error.lower())
        # a ORQUESTRAÇÃO desistiu bem antes do 1.5s da tarefa real
        self.assertLess(duration, 1.0)
        # limpeza determinística (nunca um sleep arbitrário) -- aguarda
        # a thread órfã terminar de verdade antes do próximo teste.
        self.assertEqual(executor.wait_for_inflight_completion(timeout=3.0), 0)

    def test_fast_task_within_timeout_succeeds(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "rapido", "description": "r"}])

        def runner(task):
            time.sleep(0.02)
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste", timeout_per_task=5.0)

        self.assertEqual(report.result_for("rapido").status, "VERIFIED")


class CancellationTests(unittest.TestCase):
    """Cancelamento cooperativo — nunca inicia tarefas/grupos após a
    detecção do cancelamento."""

    def test_cancel_during_execution_blocks_next_group(self):
        cancel_event = threading.Event()
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="cancel", tasks=[
            {"task_id": "c1", "description": "c1"},
            {"task_id": "c2", "description": "c2", "depends_on": ["c1"]},
        ])

        def runner(task):
            if task.task_id == "c1":
                cancel_event.set()
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste", cancel_event=cancel_event)

        self.assertTrue(report.cancelled)
        self.assertIsNone(report.result_for("c2"))

    def test_cancel_before_any_execution_runs_nothing(self):
        cancel_event = threading.Event()
        cancel_event.set()  # ja cancelado ANTES de comecar
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1"}])
        calls = []

        def runner(task):
            calls.append(task.task_id)
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste", cancel_event=cancel_event)

        self.assertTrue(report.cancelled)
        self.assertEqual(calls, [])

    def test_loop_cancellation_raises_plan_cancelled(self):
        cancel_event = threading.Event()
        counter = {"n": 0}

        def body(i):
            counter["n"] += 1
            if counter["n"] == 2:
                cancel_event.set()

        with self.assertRaises(PlanCancelled):
            run_loop(LoopSpec(condition=lambda i: False, body=body, max_iterations=100), cancel_event=cancel_event)
        self.assertEqual(counter["n"], 2)  # parou logo apos o cancelamento, nunca continuou


class ResumptionWithoutDuplicationTests(unittest.TestCase):
    """Retomada sem duplicidade — reexecutar com o MESMO mission_id
    nunca repete efeito externo já confirmado."""

    def test_verified_tasks_never_rerun_on_resume(self):
        call_log = []
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="retomada", tasks=[
            {"task_id": "t1", "description": "t1"}, {"task_id": "t2", "description": "t2", "depends_on": ["t1"]},
        ])

        def runner(task):
            call_log.append(task.task_id)
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        report1 = executor.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m_estavel")
        self.assertTrue(report1.success)
        self.assertEqual(sorted(call_log), ["t1", "t2"])

        report2 = executor.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m_estavel")
        self.assertEqual(sorted(call_log), ["t1", "t2"])  # NUNCA cresceu -- nenhuma re-execução
        for r in report2.task_results:
            self.assertEqual(r.status, "SKIPPED_ALREADY_DONE")

    def test_resume_after_partial_completion_only_reruns_pending(self):
        """Grupo 1 completa; simula 'processo morreu' antes do grupo 2
        rodar (usa um NOVO executor com os MESMOS trackers/filas,
        simulando restart); grupo 2 executa normalmente, grupo 1
        nunca re-executa."""
        call_log = []
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[
            {"task_id": "g1", "description": "g1"}, {"task_id": "g2", "description": "g2", "depends_on": ["g1"]},
        ])

        def runner(task):
            call_log.append(task.task_id)
            return {"ok": True}

        tracker = MissionTracker()
        queue = JobQueue()
        executor1 = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)
        executor1.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m_restart")
        self.assertEqual(sorted(call_log), ["g1", "g2"])

        # "restart" -- NOVO GraphExecutor, MESMOS tracker/queue (simula
        # processo reiniciado com persistência real)
        executor2 = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)
        report2 = executor2.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m_restart")
        self.assertEqual(sorted(call_log), ["g1", "g2"])  # nunca duplicou
        self.assertTrue(all(r.status == "SKIPPED_ALREADY_DONE" for r in report2.task_results))


class CleanShutdownTests(unittest.TestCase):
    """Shutdown limpo — nenhuma thread residual permanente, o
    PROCESSO consegue encerrar sem ficar preso, mesmo após cenários de
    timeout/cancelamento."""

    def test_no_residual_threads_after_normal_execution(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": f"t{i}", "description": f"t{i}"} for i in range(5)])
        executor, _, _, _ = _fresh_executor(max_workers=5)
        executor.execute_plan(plan, task_runner=lambda t: {"ok": True}, actor="teste")
        gc.collect()
        residual = [t for t in threading.enumerate() if t.name != "MainThread" and t is not threading.current_thread()]
        self.assertEqual(residual, [])

    def test_orphaned_timeout_thread_eventually_cleans_up_and_never_blocks_exit(self):
        """A thread órfã de uma tarefa que estourou o timeout é
        DAEMON — nunca impede o processo de encerrar (correção central
        desta entrega). Espera DETERMINISTICAMENTE (nunca um
        `time.sleep()` arbitrário) via `wait_for_inflight_completion()`
        — a mesma correção que resolveu a flakiness original destes
        testes de shutdown."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "lento", "description": "l", "max_retries": 0}])

        def runner(task):
            time.sleep(0.4)
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste", timeout_per_task=0.05)
        self.assertEqual(report.result_for("lento").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        # espera DETERMINISTICAMENTE a thread órfã terminar (nunca
        # um sleep arbitrário que poderia ser curto demais sob carga).
        still_alive = executor.wait_for_inflight_completion(timeout=3.0)
        self.assertEqual(still_alive, 0)
        gc.collect()
        residual = [t for t in threading.enumerate() if t.name != "MainThread" and t is not threading.current_thread()]
        self.assertEqual(residual, [])

    def test_cancelled_execution_leaves_no_residual_thread(self):
        cancel_event = threading.Event()
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "c1", "description": "c1"}])

        def runner(task):
            cancel_event.set()
            return {"ok": True}

        executor, _, _, _ = _fresh_executor()
        executor.execute_plan(plan, task_runner=runner, actor="teste", cancel_event=cancel_event)
        gc.collect()
        residual = [t for t in threading.enumerate() if t.name != "MainThread" and t is not threading.current_thread()]
        self.assertEqual(residual, [])


class OwnerApprovalIntegrationTests(unittest.TestCase):
    """Owner Approval real via `EscalationDesk` — nunca executa tarefa
    crítica sem aprovação explícita."""

    def test_critical_task_blocked_without_approval(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "critica", "description": "c"}])
        executor, _, _, desk = _fresh_executor(with_escalation=True)

        report = executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_critica",
            requires_owner_approval=lambda t: True,
        )
        result = report.result_for("critica")
        self.assertEqual(result.status, "WAITING_OWNER")
        self.assertIsNotNone(result.escalation_id)
        self.assertFalse(report.success)

    def test_critical_task_proceeds_after_approval(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "critica", "description": "c"}])
        executor, _, _, desk = _fresh_executor(with_escalation=True)

        blocked = executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_critica2",
            requires_owner_approval=lambda t: True,
        )
        escalation_id = blocked.result_for("critica").escalation_id
        desk.approve(escalation_id, decision="aprovado", resolved_by=OWNER)

        approved = executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_critica2",
            requires_owner_approval=lambda t: True, approved_escalation_ids={"critica": escalation_id},
        )
        self.assertEqual(approved.result_for("critica").status, "VERIFIED")
        self.assertTrue(approved.success)

    def test_non_critical_task_never_requires_approval(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "rotina", "description": "r"}])
        executor, _, _, desk = _fresh_executor(with_escalation=True)

        report = executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste",
            requires_owner_approval=lambda t: False,
        )
        self.assertEqual(report.result_for("rotina").status, "VERIFIED")


class LoopConstructTests(unittest.TestCase):
    """Loops explícitos — condição de parada real, limite de
    iterações, fail-closed."""

    def test_loop_converges_before_limit(self):
        counter = {"n": 0}
        iterations = run_loop(LoopSpec(
            condition=lambda i: counter["n"] >= 3, body=lambda i: counter.__setitem__("n", counter["n"] + 1),
            max_iterations=10,
        ))
        self.assertEqual(iterations, 3)
        self.assertEqual(counter["n"], 3)

    def test_loop_fail_closed_when_limit_exceeded(self):
        with self.assertRaises(LoopLimitExceeded):
            run_loop(LoopSpec(condition=lambda i: False, body=lambda i: None, max_iterations=5))

    def test_loop_rejects_non_positive_max_iterations(self):
        with self.assertRaises(ValueError):
            LoopSpec(condition=lambda i: True, body=lambda i: None, max_iterations=0)
        with self.assertRaises(ValueError):
            LoopSpec(condition=lambda i: True, body=lambda i: None, max_iterations=-1)

    def test_loop_condition_checked_before_body_each_time(self):
        """Se a condição já é verdadeira na primeira checagem, o corpo
        nunca executa nenhuma vez."""
        body_calls = []
        iterations = run_loop(LoopSpec(condition=lambda i: True, body=lambda i: body_calls.append(i), max_iterations=10))
        self.assertEqual(iterations, 0)
        self.assertEqual(body_calls, [])


if __name__ == "__main__":
    unittest.main()
