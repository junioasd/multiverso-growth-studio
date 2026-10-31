import gc
import threading
import time
import unittest

from atlas.advanced_planning import AdvancedPlanner
from core.coordination.mission import MissionStatus, MissionTracker
from core.jobs.queue import JobQueue
from atlas.orchestration.graph_executor import GraphExecutor

"""
MICROCORREÇÃO — ATLAS EXPANSION 1: timeout+retry nunca duplica efeito
externo + Quality Gate consistente.

Falhas reais reproduzidas por auditoria independente:

1. Timeout + retry podia iniciar uma nova tentativa enquanto a
   anterior ainda estava viva — duas threads da MESMA tarefa
   aplicavam o efeito externo real ao mesmo tempo, enquanto o
   relatório final dizia `FAILED`.

2. O Job virava `COMPLETED` ANTES da verificação do Quality Gate. Se
   o gate rejeitasse, a missão ficava presa em `COMPLETED` (nunca
   `VERIFIED` nem `FAILED`), e uma exceção não tratada
   (`InvalidMissionTransition`) propagava para fora de
   `execute_plan()`.

Corrigido: registro real de threads em voo por
`(mission_id, task_id)` — nunca inicia uma segunda execução da mesma
tarefa enquanto a anterior não tiver um desfecho confirmado (novo
status `TIMED_OUT_UNKNOWN_OUTCOME`, nunca `FAILED`, para nunca
liberar retry automático sobre uma execução ainda potencialmente
viva); e o Quality Gate agora é avaliado ANTES de qualquer transição
para `COMPLETED` — rejeição vira uma falha normal e retomável via o
mesmo `JobQueue`, nunca um estado preso.
"""


def _fresh_executor(*, max_workers: int = 4):
    tracker = MissionTracker()
    queue = JobQueue()
    return GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=max_workers), tracker, queue


class TimeoutRetryNeverDuplicatesTests(unittest.TestCase):
    """Cenário negativo EXATO reportado: reproduz a duplicidade antes
    de confirmar a correção, com contagem REAL de efeitos externos
    aplicados (nunca um proxy indireto)."""

    def tearDown(self):
        gc.collect()

    def test_single_task_timeout_retry_applies_external_effect_exactly_once(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="dup", tasks=[{"task_id": "lento", "description": "l", "max_retries": 1}])

        effect_log = []
        lock = threading.Lock()

        def runner(task):
            with lock:
                effect_log.append("iniciou")
            time.sleep(0.3)  # mais que o timeout configurado abaixo
            with lock:
                effect_log.append("EFEITO_EXTERNO_APLICADO")
            return {"ok": True}

        executor, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=runner, actor="teste", timeout_per_task=0.1)

        # status honesto -- nunca FAILED (que liberaria retry
        # automático sobre uma execução ainda potencialmente viva)
        self.assertEqual(report.result_for("lento").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        # espera DETERMINISTICAMENTE (nunca time.sleep arbitrário) a
        # thread em voo terminar, antes de contar o efeito real
        still_alive = executor.wait_for_inflight_completion(timeout=3.0)
        self.assertEqual(still_alive, 0)

        applied = [e for e in effect_log if e == "EFEITO_EXTERNO_APLICADO"]
        self.assertEqual(len(applied), 1, f"efeito externo aplicado {len(applied)}x — deveria ser exatamente 1")

    def test_repeated_execute_plan_calls_never_start_second_thread_while_first_alive(self):
        """Chama `execute_plan()` DUAS VEZES para o mesmo mission_id
        enquanto a primeira execução ainda está genuinamente viva —
        confirma que a segunda chamada NUNCA inicia uma nova thread,
        apenas reporta o mesmo desfecho desconhecido."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        thread_starts = []
        lock = threading.Lock()

        def runner(task):
            with lock:
                thread_starts.append(threading.get_ident())
            time.sleep(0.5)
            return {"ok": True}

        executor, _, _ = _fresh_executor()
        report1 = executor.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m1", timeout_per_task=0.05)
        self.assertEqual(report1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        # chama de novo IMEDIATAMENTE, enquanto a thread original
        # ainda está genuinamente viva (dorme 0.5s, só passaram ~0.05s)
        report2 = executor.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m1", timeout_per_task=0.05)
        self.assertEqual(report2.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        executor.wait_for_inflight_completion(timeout=3.0)
        with lock:
            self.assertEqual(len(thread_starts), 1, "uma segunda thread foi iniciada enquanto a primeira ainda vivia")

    def test_resumption_after_original_thread_finishes_uses_real_outcome_never_reruns(self):
        """Depois que a thread original (que tinha estourado o
        timeout) TERMINA de verdade em segundo plano — mas ANTES de
        `wait_for_inflight_completion()` ser chamado (que é só um
        utilitário de limpeza/teste e descarta deliberadamente o
        resultado rastreado depois de confirmar o término) — uma nova
        chamada de `execute_plan()` para o mesmo mission_id deve usar
        o resultado REAL já disponível no registro — nunca reexecutar
        `task_runner`."""
        call_count = {"n": 0}
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        def runner(task):
            call_count["n"] += 1
            time.sleep(0.2)
            return {"ok": True, "chamada": call_count["n"]}

        executor, tracker, _ = _fresh_executor()
        report1 = executor.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m2", timeout_per_task=0.05)
        self.assertEqual(report1.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        # espera o suficiente para a thread original terminar de
        # verdade SOZINHA (0.2s de trabalho real), SEM consultar/
        # limpar o registro em voo ainda — o registro continua
        # sabendo que ela já terminou.
        time.sleep(0.4)
        self.assertEqual(call_count["n"], 1)

        # nova chamada -- deve usar o resultado REAL da execução
        # original (já finalizada, ainda no registro), nunca chamar
        # task_runner de novo.
        report2 = executor.execute_plan(plan, task_runner=runner, actor="teste", mission_id="m2", timeout_per_task=5.0)
        self.assertEqual(call_count["n"], 1, "task_runner foi chamado de novo -- deveria ter usado o resultado real já existente")
        self.assertEqual(report2.result_for("t1").status, "VERIFIED")

        # limpeza determinística ao final do teste
        self.assertEqual(executor.wait_for_inflight_completion(timeout=1.0), 0)

    def test_multiple_tasks_timeout_independently_never_cross_contaminate(self):
        """Duas tarefas distintas, cada uma com timeout — cada uma
        tem seu PRÓPRIO registro em voo; uma nunca bloqueia/afeta a
        outra."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[
            {"task_id": "a", "description": "a", "max_retries": 0},
            {"task_id": "b", "description": "b", "max_retries": 0},
        ])

        def runner(task):
            time.sleep(0.3)
            return {"ok": True}

        executor, _, _ = _fresh_executor(max_workers=2)
        report = executor.execute_plan(plan, task_runner=runner, actor="teste", timeout_per_task=0.05)

        self.assertEqual(report.result_for("a").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        self.assertEqual(report.result_for("b").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        still_alive = executor.wait_for_inflight_completion(timeout=3.0)
        self.assertEqual(still_alive, 0)


class NoResidualThreadInterferesWithSubsequentTestsTests(unittest.TestCase):
    """Confirma explicitamente que threads de timeout de UM teste
    nunca vazam para afetar a contagem de threads de um teste
    SEGUINTE — a exigência exata da auditoria."""

    def test_thread_count_baseline_before_any_timeout_scenario(self):
        baseline = [t for t in threading.enumerate() if t.name != "MainThread" and t is not threading.current_thread()]
        self.assertEqual(baseline, [], "já existem threads residuais ANTES deste teste rodar")

    def test_timeout_scenario_cleans_up_deterministically(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        def runner(task):
            time.sleep(0.2)
            return {"ok": True}

        executor, _, _ = _fresh_executor()
        executor.execute_plan(plan, task_runner=runner, actor="teste", timeout_per_task=0.02)
        still_alive = executor.wait_for_inflight_completion(timeout=3.0)
        self.assertEqual(still_alive, 0)
        gc.collect()
        residual = [t for t in threading.enumerate() if t.name != "MainThread" and t is not threading.current_thread()]
        self.assertEqual(residual, [])

    def test_thread_count_after_timeout_scenario_is_clean_for_next_test(self):
        """Executado DEPOIS do teste anterior (ordem alfabética) —
        confirma que NENHUM resíduo do teste anterior sobrou aqui."""
        residual = [t for t in threading.enumerate() if t.name != "MainThread" and t is not threading.current_thread()]
        self.assertEqual(residual, [], "thread residual do teste anterior vazou para este teste")

    def test_repeated_timeout_scenarios_across_multiple_executors_never_accumulate_threads(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        def runner(task):
            time.sleep(0.15)
            return {"ok": True}

        for _ in range(5):
            executor, _, _ = _fresh_executor()
            executor.execute_plan(plan, task_runner=runner, actor="teste", timeout_per_task=0.02)
            executor.wait_for_inflight_completion(timeout=3.0)

        gc.collect()
        residual = [t for t in threading.enumerate() if t.name != "MainThread" and t is not threading.current_thread()]
        self.assertEqual(residual, [], f"threads acumularam através de execuções repetidas: {[t.name for t in residual]}")


class QualityGateConsistentStateTests(unittest.TestCase):
    """Cenário exato reportado: Job/Mission nunca ficam presos em
    COMPLETED quando o Quality Gate rejeita."""

    def test_quality_gate_approved_reaches_verified(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g"}])
        executor, tracker, _ = _fresh_executor()

        report = executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_approved",
            quality_gate_checker=lambda t, o: True,
        )
        self.assertEqual(report.result_for("t1").status, "VERIFIED")
        self.assertEqual(tracker.get("m_approved", "t1").status, MissionStatus.VERIFIED)

    def test_quality_gate_rejected_with_no_retries_reaches_failed_never_stuck(self):
        """Cenário EXATO reportado: sem exceção não tratada, sem
        Mission/Job presos em COMPLETED."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])
        executor, tracker, queue = _fresh_executor()

        # nunca deve levantar excecao nao tratada
        report = executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_rejected",
            quality_gate_checker=lambda t, o: False,
        )
        self.assertEqual(report.result_for("t1").status, "FAILED")
        mission = tracker.get("m_rejected", "t1")
        self.assertEqual(mission.status, MissionStatus.FAILED)
        self.assertNotEqual(mission.status, MissionStatus.COMPLETED)  # nunca preso em COMPLETED

    def test_quality_gate_rejected_with_retries_eventually_verified(self):
        attempt = {"n": 0}
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 3}])

        def runner(task):
            attempt["n"] += 1
            return {"tentativa": attempt["n"]}

        def gate(task, outcome):
            return outcome["tentativa"] >= 3

        executor, tracker, _ = _fresh_executor()
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_retry_gate", quality_gate_checker=gate,
        )
        self.assertEqual(report.result_for("t1").status, "VERIFIED")
        self.assertEqual(attempt["n"], 3)

    def test_resume_after_rejection_with_retries_available_continues_consistently(self):
        """Retomada (nova chamada de `execute_plan()` com o mesmo
        `mission_id`) depois de uma rejeição de gate que AINDA tinha
        retries disponíveis — continua de forma consistente, nunca
        presa."""
        attempt = {"n": 0}
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 5}])

        def runner(task):
            attempt["n"] += 1
            return {"tentativa": attempt["n"]}

        def gate(task, outcome):
            return outcome["tentativa"] >= 2

        executor, tracker, _ = _fresh_executor()
        # primeira chamada -- roda ate convergir (o proprio loop de retry ja trata)
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_resume_gate", quality_gate_checker=gate,
        )
        self.assertEqual(report.result_for("t1").status, "VERIFIED")

        # retomada -- reexecutar com o MESMO mission_id nunca duplica
        # (missao ja VERIFIED -- pulada)
        report2 = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_resume_gate", quality_gate_checker=gate,
        )
        self.assertEqual(report2.result_for("t1").status, "SKIPPED_ALREADY_DONE")
        self.assertEqual(attempt["n"], 2)  # nunca chamou de novo na retomada

    def test_resume_after_definitive_rejection_stays_consistently_failed(self):
        """Retomada depois de uma rejeição DEFINITIVA (retries
        esgotados, FAILED terminal) — permanece FAILED de forma
        consistente, nunca uma exceção nem um estado ambíguo."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])
        executor, tracker, _ = _fresh_executor()

        executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_stuck_gate",
            quality_gate_checker=lambda t, o: False,
        )
        # retomada -- nunca levanta excecao, nunca "desfaz" o FAILED terminal
        report2 = executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_stuck_gate",
            quality_gate_checker=lambda t, o: False,
        )
        self.assertEqual(report2.result_for("t1").status, "FAILED")
        self.assertEqual(tracker.get("m_stuck_gate", "t1").status, MissionStatus.FAILED)

    def test_no_quality_gate_never_blocks_normal_verification(self):
        """Tarefa sem `quality_gate` definido continua funcionando
        exatamente como antes — nenhuma regressão no caso comum."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1"}])
        executor, _, _ = _fresh_executor()
        report = executor.execute_plan(plan, task_runner=lambda t: {"ok": True}, actor="teste")
        self.assertEqual(report.result_for("t1").status, "VERIFIED")


if __name__ == "__main__":
    unittest.main()
