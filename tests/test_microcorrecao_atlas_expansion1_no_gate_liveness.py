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
MICROCORREÇÃO — ATLAS EXPANSION 1: CAMINHO SEM QUALITY GATE —
PERSISTÊNCIA IMEDIATA DO OUTCOME + LIVENESS/TTL PARA `IN_PROGRESS`.

Falha real reproduzida por auditoria independente: no caminho SEM
Quality Gate, `task_runner` aplicava o efeito e retornava sucesso,
mas o outcome só era persistido no lease como ÚLTIMO passo — depois
de sincronizar Mission/Job. Se o processo morresse ABRUPTAMENTE em
QUALQUER ponto antes dessa última linha, o lease ficava preso em
`IN_PROGRESS` com `outcome_json=NULL` — e `IN_PROGRESS` não tinha
NENHUM mecanismo de TTL/reclaim (só `GATE_IN_PROGRESS` tinha) —
preso PARA SEMPRE, mesmo o efeito real já tendo ocorrido.

Corrigido:
1. O outcome agora é persistido e o lease resolvido para `COMPLETED`
   PRIMEIRO (atômico) — Mission/Job são sincronizados DEPOIS. Um
   crash nessa janela final deixa Mission/Job temporariamente
   dessincronizados, mas NUNCA perde o outcome — uma retomada futura
   usa `_finalize_cached_completed()` (agora mais robusto, via
   `_advance_mission_to_completed_and_verify()`, que avança a partir
   de QUALQUER estado intermediário válido) para reconciliar sem
   jamais reexecutar `task_runner`.
2. `IN_PROGRESS` agora também tem TTL — mas com um desfecho
   DELIBERADAMENTE diferente do reclaim de `GATE_IN_PROGRESS`: nunca
   reclama como `ACQUIRED` (isso reexecutaria `task_runner`,
   prometendo exactly-once sem prova de idempotência) — transiciona
   ATOMICAMENTE para `UNKNOWN_OUTCOME`, terminal, nunca retentado
   automaticamente.
3. Este executor nunca promete exactly-once para efeitos externos
   arbitrários — retry automático só ocorre sobre um desfecho
   DEFINITIVAMENTE conhecido (uma exceção real, ou uma rejeição de
   gate sobre um outcome já obtido); um desfecho genuinamente
   desconhecido (`UNKNOWN_OUTCOME`) nunca dispara retry algum.
"""

_NO_GATE_WORKER = os.path.join(os.path.dirname(__file__), "_helpers", "crash_no_gate_worker.py")
_DURING_RUNNER_WORKER = os.path.join(os.path.dirname(__file__), "_helpers", "crash_during_runner_worker.py")


def _fresh_tracker_and_queue(db_path=None):
    return MissionTracker(db_path=db_path), JobQueue(db_path=db_path)


def _read_lease(db_path: str, mission_id: str):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, outcome_json, error FROM graph_executor_task_leases WHERE mission_id=? AND task_id='t1'",
        (mission_id,),
    ).fetchone()
    conn.close()
    return row


class CrashBetweenLeaseAndMissionSyncTests(unittest.TestCase):
    """Cenário (a) exigido: crash REAL após `runner` retornar, sem
    gate — outcome persistido no lease, mesmo com Mission/Job
    temporariamente dessincronizados; retomada reconcilia sem
    reexecutar."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.ttl = 0.3

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_real_subprocess_crash_after_runner_success_no_gate_never_stuck(self):
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        result = subprocess.run(
            [sys.executable, _NO_GATE_WORKER, self.db_path, "m_no_gate_crash", str(self.ttl)], timeout=15,
        )
        self.assertNotEqual(result.returncode, 0, "o subprocesso deveria ter morrido abruptamente")

        # o outcome JA esta persistido -- lease COMPLETED, nunca preso
        # em IN_PROGRESS/outcome_json=NULL (o bug original).
        row = _read_lease(self.db_path, "m_no_gate_crash")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "COMPLETED", "lease deveria estar COMPLETED com o outcome real, nunca preso")
        self.assertIsNotNone(row["outcome_json"])

        # Mission esta genuinamente dessincronizada nesse ponto (o
        # processo morreu ANTES de sincronizar) -- confirma o cenario
        # real antes de testar a reconciliacao.
        tracker_check = MissionTracker(db_path=self.db_path)
        mission_before = tracker_check.get("m_no_gate_crash", "t1")
        self.assertNotEqual(mission_before.status, MissionStatus.VERIFIED)

        # retomada -- nova instancia, nunca reexecuta o runner
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
            plan, task_runner=runner, actor="teste", mission_id="m_no_gate_crash", timeout_per_task=5.0,
        )

        self.assertEqual(call_count["n"], 0, "task_runner NUNCA deveria ser reexecutado -- o efeito já ocorreu no subprocesso")
        self.assertEqual(report.result_for("t1").status, "VERIFIED")

        mission_after = tracker.get("m_no_gate_crash", "t1")
        self.assertEqual(mission_after.status, MissionStatus.VERIFIED)

        executor.wait_for_inflight_completion(timeout=1.0)

    def test_reconciliation_never_requires_immediate_reclaim_wait(self):
        """A reconciliação a partir de um lease já `COMPLETED` NUNCA
        depende do TTL — funciona imediatamente, sem esperar nada
        (diferente de `GATE_IN_PROGRESS`/`IN_PROGRESS`, que exigem
        expiração para serem reclamados).

        FECHAMENTO 1/6 (sensibilidade do teste): com o TTL de 0.3s do
        setUp e orçamento de 0.5s, uma regressão que ESPERASSE o TTL
        custava ~0.3s + ~0.015s de custo real medido (p50, 15 execuções)
        e PASSAVA — prova por mutação: `sleep(ttl)` no ramo COMPLETED
        passou 3/3. Subir o orçamento para 1.0s (proposta anterior)
        aprofundaria a cegueira. Correção: TTL desta prova = 5.0s,
        ordem de grandeza acima do orçamento (1.0s) — qualquer espera
        por TTL custa >= 5s e FALHA; o mesmo mutante agora é detectado.
        O orçamento de 1.0s mantém ~66x de folga sobre o custo medido
        (~15ms) sem depender do TTL para discriminar."""
        self.ttl = 5.0
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        subprocess.run([sys.executable, _NO_GATE_WORKER, self.db_path, "m_immediate", str(self.ttl)], timeout=15)

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        start = time.time()
        report = executor.execute_plan(
            plan, task_runner=lambda t: {"ok": True}, actor="teste", mission_id="m_immediate", timeout_per_task=5.0,
        )
        duration = time.time() - start

        self.assertLess(duration, 1.0, "reconciliação de um lease já COMPLETED nunca deveria esperar (TTL=%.1fs)" % self.ttl)
        self.assertEqual(report.result_for("t1").status, "VERIFIED")

        executor.wait_for_inflight_completion(timeout=1.0)


class CrashDuringRunnerBecomesUnknownOutcomeTests(unittest.TestCase):
    """Cenário (b) exigido: crash REAL durante/antes do retorno do
    `runner` — após TTL vira `UNKNOWN_OUTCOME`, sem rerun
    automático."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.ttl = 0.3

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_real_subprocess_crash_during_runner_never_retried_after_ttl(self):
        # CORRECAO DO TESTE (producao intocada): TTL desta prova elevado para 1.0s.
        # Medicao nesta maquina: aquisicao do lease -> inicio do task_runner no
        # worker custa 0.37-0.45s, consumindo o TTL de 0.3s ANTES do crash — a
        # premissa "tentativa imediata dentro do TTL" era insatisfativel por
        # construcao. Com 1.0s a intencao original se mantem: dentro do TTL nunca
        # reclama; fora dele + holder provadamente morto converte para
        # UNKNOWN_OUTCOME. Confinado a ESTE teste: o setUp e o teste irmao
        # (m_repeated) permanecem com 0.3s.
        self.ttl = 1.0
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        result = subprocess.run(
            [sys.executable, _DURING_RUNNER_WORKER, self.db_path, "m_during_crash", str(self.ttl)], timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)

        row = _read_lease(self.db_path, "m_during_crash")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "IN_PROGRESS")
        self.assertIsNone(row["outcome_json"], "desfecho deveria ser genuinamente desconhecido -- nunca um outcome inventado")

        # tentativa IMEDIATA -- ainda dentro do TTL, nunca reclama
        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            return {"ok": True}

        tracker_early, queue_early = _fresh_tracker_and_queue(self.db_path)
        executor_early = GraphExecutor(
            mission_tracker=tracker_early, job_queue=queue_early, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        start = time.time()
        report_early = executor_early.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_during_crash", timeout_per_task=5.0,
        )
        duration_early = time.time() - start
        self.assertLess(duration_early, 1.0, "nunca deveria girar esperando")
        self.assertEqual(call_count["n"], 0)
        self.assertEqual(_read_lease(self.db_path, "m_during_crash")["status"], "IN_PROGRESS")

        # aguarda o TTL expirar de verdade
        time.sleep(self.ttl + 0.2)

        tracker, queue = _fresh_tracker_and_queue(self.db_path)
        executor = GraphExecutor(
            mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
            gate_in_progress_ttl_seconds=self.ttl,
        )
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_during_crash", timeout_per_task=5.0,
        )

        self.assertEqual(call_count["n"], 0, "NUNCA deveria reexecutar automaticamente -- desfecho desconhecido, sem prova de idempotência")
        self.assertEqual(report.result_for("t1").status, "TIMED_OUT_UNKNOWN_OUTCOME")

        final_row = _read_lease(self.db_path, "m_during_crash")
        self.assertEqual(final_row["status"], "UNKNOWN_OUTCOME")

        mission = tracker.get("m_during_crash", "t1")
        self.assertNotEqual(mission.status, MissionStatus.VERIFIED)
        self.assertNotEqual(mission.status, MissionStatus.FAILED)  # tambem nunca marca falha definitiva sem prova

    def test_multiple_calls_after_ttl_never_accumulate_reexecutions(self):
        """Múltiplas tentativas seguidas após o TTL expirar — nenhuma
        delas jamais reexecuta o runner, mesmo repetidamente."""
        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        subprocess.run(
            [sys.executable, _DURING_RUNNER_WORKER, self.db_path, "m_repeated", str(self.ttl)], timeout=15,
        )
        time.sleep(self.ttl + 0.2)

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            return {"ok": True}

        for _ in range(3):
            tracker, queue = _fresh_tracker_and_queue(self.db_path)
            executor = GraphExecutor(
                mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4,
                gate_in_progress_ttl_seconds=self.ttl,
            )
            executor.execute_plan(
                plan, task_runner=runner, actor="teste", mission_id="m_repeated", timeout_per_task=5.0,
            )

        self.assertEqual(call_count["n"], 0, "nenhuma das 3 tentativas deveria ter reexecutado o runner")


class RestartAfterCompletedSyncsWithoutRerunTests(unittest.TestCase):
    """Cenário (c) exigido: restart após `COMPLETED` persistido
    sincroniza Mission/Job sem executar `runner` novamente — via
    injeção direta de um lease já `COMPLETED` (sem depender de um
    subprocesso, para isolar especificamente a reconciliação)."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_mission_stuck_at_running_reconciles_from_completed_lease(self):
        import json
        import sqlite3

        planner = AdvancedPlanner()
        plan = planner.build_plan(objective="x", tasks=[{"task_id": "t1", "description": "t1", "max_retries": 0}])

        # simula: mission chegou a RUNNING (fluxo normal ate ali), mas
        # o processo morreu antes de qualquer coisa mais -- e o lease
        # ja foi resolvido para COMPLETED por outro caminho (ex: a
        # propria thread de heartbeat/timeout, ou este teste
        # injetando diretamente).
        tracker = MissionTracker(db_path=self.db_path)
        queue = JobQueue(db_path=self.db_path)
        from core.coordination.mission import MissionAssignment
        mission = MissionAssignment.new(
            agent="teste", objective="x", mission_id="m_stuck", task_id="t1", dependencies=[], success_criteria=[],
        )
        tracker.create(mission)
        tracker.transition("m_stuck", "t1", status=MissionStatus.PLANNED)
        tracker.transition("m_stuck", "t1", status=MissionStatus.RUNNING)

        job = queue.enqueue(
            job_type="graph_executor_task", idempotency_key="m_stuck:t1",
            payload={"task_id": "t1", "mission_id": "m_stuck"}, project_id="m_stuck", max_attempts=1,
        )
        queue.try_start(job.job_id)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO graph_executor_task_leases (mission_id, task_id, lease_token, status, leased_at, "
            "outcome_json, generation) VALUES (?, ?, ?, 'COMPLETED', ?, ?, 0)",
            ("m_stuck", "t1", "tok_x", time.time(), json.dumps({"ok": True, "veio_do_lease": True})),
        )
        conn.commit()
        conn.close()

        call_count = {"n": 0}

        def runner(task):
            call_count["n"] += 1
            return {"ok": True}

        executor = GraphExecutor(mission_tracker=tracker, job_queue=queue, db_path=self.db_path, max_workers=4)
        report = executor.execute_plan(
            plan, task_runner=runner, actor="teste", mission_id="m_stuck", timeout_per_task=5.0,
        )

        self.assertEqual(call_count["n"], 0, "task_runner NUNCA deveria ser executado -- lease já COMPLETED")
        self.assertEqual(report.result_for("t1").status, "VERIFIED")
        self.assertEqual(report.result_for("t1").result, {"ok": True, "veio_do_lease": True})

        mission_final = tracker.get("m_stuck", "t1")
        self.assertEqual(mission_final.status, MissionStatus.VERIFIED)

        job_final = queue.get(job.job_id)
        self.assertEqual(job_final.state.value, "COMPLETED")


if __name__ == "__main__":
    unittest.main()
