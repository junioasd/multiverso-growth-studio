import os
import subprocess
import sys
import tempfile
import time
import unittest

from atlas.advanced_planning import AdvancedPlanner
from core.coordination.mission import MissionStatus, MissionTracker
from core.jobs.queue import JobQueue
from core.models import db as db_module
from atlas.orchestration.graph_executor import GraphExecutor

"""
MICROCORREÇÃO — ATLAS EXPANSION 1: UNIFICAÇÃO DO CAMINHO FRESCO COM O
MECANISMO DURÁVEL JÁ VALIDADO.

Falha real reproduzida por auditoria independente: no caminho de
execução FRESCA (quando `task_runner` termina DENTRO do timeout,
nunca um outcome tardio), se havia `Quality Gate` configurado, o
outcome recém-obtido só existia em memória LOCAL até a avaliação do
gate terminar — o lease permanecia em `IN_PROGRESS`. Se o processo
morresse ABRUPTAMENTE durante a avaliação do gate, o EFEITO REAL do
`task_runner` já tinha ocorrido, mas o outcome nunca fora persistido
— e `IN_PROGRESS` não tem NENHUM mecanismo de TTL/reclaim aplicável
(só `GATE_IN_PROGRESS` tem) — o outcome ficava PERDIDO PARA SEMPRE,
um cenário ainda mais severo que o já corrigido para outcomes
tardios.

Corrigido: assim que `task_runner` retorna sucesso e há
`quality_gate` configurado, `_persist_outcome_and_claim_gate()`
persiste o outcome ATOMICAMENTE e transiciona o lease DIRETO para
`GATE_IN_PROGRESS` (mesmo `lease_token`, `generation` avançada) ANTES
de qualquer avaliação começar — então delega diretamente para
`_finalize_pending_gate_outcome()`, reaproveitando o MESMO mecanismo
já validado (claim exclusivo + heartbeat + TTL + reclaim) usado para
outcomes tardios. Nenhum caminho separado e mais frágil.
"""

_WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "_helpers", "crash_fresh_gate_worker.py")


def _fresh_tracker_and_queue(db_path=None):
    return MissionTracker(db_path=db_path), JobQueue(db_path=db_path)


class FreshPathOutcomeSurvivesGateCrashTests(unittest.TestCase):
    """Cenário exato exigido: runner conclui 1x → gate encerra o
    processo via os._exit() → lease mantém outcome recuperável →
    após TTL nova instância faz reclaim → runner continua 1x, gate
    saudável executa e finaliza."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.ttl = 0.3

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _crash_subprocess_during_fresh_gate(self, mission_id: str, max_retries: int = 0) -> int:
        result = subprocess.run(
            [sys.executable, _WORKER_SCRIPT, self.db_path, mission_id, str(self.ttl), str(max_retries)], timeout=15,
        )
        return result.returncode

    def _read_lease(self, mission_id: str):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, outcome_json, generation FROM graph_executor_task_leases "
            "WHERE mission_id=? AND task_id='t1'",
            (mission_id,),
        ).fetchone()
        conn.close()
        return row

    def test_real_subprocess_crash_during_fresh_gate_approve(self):
        """Variante APPROVE: subprocesso conclui o runner, morre
        durante o gate; instância saudável recupera e APROVA."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        exit_code = self._crash_subprocess_during_fresh_gate("m_fresh_approve")
        self.assertNotEqual(exit_code, 0, "o subprocesso deveria ter morrido abruptamente")

        # confirma: outcome PERSISTIDO, lease em GATE_IN_PROGRESS --
        # NUNCA IN_PROGRESS sem outcome (o bug original).
        row = self._read_lease("m_fresh_approve")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "GATE_IN_PROGRESS")
        self.assertIsNotNone(row["outcome_json"], "outcome nunca foi persistido -- bug original ainda presente")
        self.assertIn('"ok": true', row["outcome_json"])
        generation_after_crash = row["generation"]

        time.sleep(self.ttl + 0.2)  # aguarda o TTL expirar de verdade

        call_count = {"n": 0}
        gate_calls = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            return {"ok": True}

        def healthy_gate_approves(task, outcome):
            gate_calls["n"] += 1
            return True

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_fresh_approve", timeout_per_task=5.0,
            quality_gate_checker=healthy_gate_approves,
        )

        self.assertEqual(call_count["n"], 0, "task_runner NUNCA deveria ser reexecutado -- o efeito já ocorreu no subprocesso")
        self.assertEqual(gate_calls["n"], 1, "gate deveria ter executado exatamente 1 vez após a recuperação")
        self.assertEqual(report.result_for("t1").status, "VERIFIED")

        mission = tracker.get("m_fresh_approve", "t1")
        self.assertEqual(mission.status, MissionStatus.VERIFIED)

        final_row = self._read_lease("m_fresh_approve")
        self.assertEqual(final_row["status"], "COMPLETED")
        self.assertGreater(final_row["generation"], generation_after_crash, "generation deveria ter avançado no reclaim")

        executor.wait_for_inflight_completion(timeout=1.0)

    def test_real_subprocess_crash_during_fresh_gate_reject(self):
        """Variante REJECT: subprocesso conclui o runner, morre
        durante o gate; instância saudável recupera e REJEITA (sem
        retries -- FAILED definitivo, nunca VERIFIED)."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        exit_code = self._crash_subprocess_during_fresh_gate("m_fresh_reject")
        self.assertNotEqual(exit_code, 0)

        row = self._read_lease("m_fresh_reject")
        self.assertEqual(row["status"], "GATE_IN_PROGRESS")
        self.assertIsNotNone(row["outcome_json"])

        time.sleep(self.ttl + 0.2)

        call_count = {"n": 0}
        gate_calls = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            return {"ok": True}

        def healthy_gate_rejects(task, outcome):
            gate_calls["n"] += 1
            return False

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_fresh_reject", timeout_per_task=5.0,
            quality_gate_checker=healthy_gate_rejects,
        )

        self.assertEqual(call_count["n"], 0, "task_runner NUNCA deveria ser reexecutado")
        self.assertEqual(gate_calls["n"], 1)
        self.assertEqual(report.result_for("t1").status, "FAILED")
        self.assertNotEqual(report.result_for("t1").status, "VERIFIED")

        mission = tracker.get("m_fresh_reject", "t1")
        self.assertEqual(mission.status, MissionStatus.FAILED)
        self.assertNotEqual(mission.status, MissionStatus.VERIFIED)

        final_row = self._read_lease("m_fresh_reject")
        self.assertEqual(final_row["status"], "FAILED")

        executor.wait_for_inflight_completion(timeout=1.0)

    def test_reject_variant_with_retries_available_triggers_genuinely_new_attempt(self):
        """Variante REJECT com retries disponíveis — depois que o
        outcome fresco original (obtido pelo subprocesso, agora
        recuperado) é rejeitado, uma tentativa NOVA e genuína de
        `task_runner` deve ocorrer (nunca reaproveitando o efeito
        original que já ocorreu no subprocesso morto)."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 3}])

        exit_code = self._crash_subprocess_during_fresh_gate("m_fresh_retry", max_retries=3)
        self.assertNotEqual(exit_code, 0)
        time.sleep(self.ttl + 0.2)

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            return {"tentativa": call_count["n"]}

        def gate_approves_from_second_real_attempt(task, outcome):
            # o outcome do subprocesso (recuperado, sem "tentativa")
            # e rejeitado; uma NOVA chamada real (tentativa=1, ja que
            # e a primeira desta nova instancia) e aprovada.
            return "tentativa" in outcome and outcome["tentativa"] >= 1

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_fresh_retry", timeout_per_task=5.0,
            quality_gate_checker=gate_approves_from_second_real_attempt,
        )

        # o outcome recuperado do subprocesso (sem "tentativa") foi
        # rejeitado -- gerou UMA nova tentativa real, que o gate aprovou.
        self.assertEqual(call_count["n"], 1, "deveria ter feito exatamente uma nova tentativa real")
        self.assertEqual(report.result_for("t1").status, "VERIFIED")

        executor.wait_for_inflight_completion(timeout=1.0)

    def test_reclaim_never_happens_before_ttl_expires_fresh_path(self):
        """Controle negativo: tentar reclamar IMEDIATAMENTE após o
        crash (antes do TTL expirar) nunca reclama — mesmo protocolo
        do caminho tardio, agora também no caminho fresco."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "quality_gate": "g", "max_retries": 0}])

        self._crash_subprocess_during_fresh_gate("m_fresh_too_early")

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        start = time.time()
        report = executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_fresh_too_early",
            timeout_per_task=5.0, quality_gate_checker=lambda t, o: True,
        )
        duration = time.time() - start

        self.assertLess(duration, 1.0, "deveria ter retornado imediatamente, nunca girado esperando")
        self.assertEqual(report.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")
        self.assertEqual(self._read_lease("m_fresh_too_early")["status"], "GATE_IN_PROGRESS")


class FreshPathNoGateUnaffectedTests(unittest.TestCase):
    """Confirma que uma tarefa SEM `quality_gate` continua no caminho
    rápido/direto, sem a sobrecarga da unificação — nenhuma
    regressão no caso comum."""

    def test_task_without_quality_gate_completes_normally(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1"}])
        tracker, queue = _fresh_tracker_and_queue()
        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, max_workers=4)
        report = executor.execute_plan(plan, task_runner=lambda t: {"ok": True}, actor="teste")
        self.assertEqual(report.result_for("t1").status, "VERIFIED")
        mission = tracker.get(report.mission_id, "t1")
        self.assertEqual(mission.status, MissionStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main()
