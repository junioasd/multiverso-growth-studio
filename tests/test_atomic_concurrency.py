import os
import tempfile
import threading
import unittest

from commercial.crm import ConcurrentModificationError, CRM, InvalidLeadTransition
from core.jobs.queue import InvalidJobTransition, JobQueue
from core.models import db as db_module
from core.models.entities import JobState, LeadStatus

"""
Testes de regressão — MICROCORREÇÃO 5, item 3
(ATUALIZAÇÕES E CAPACIDADE ATÔMICAS ENTRE INSTÂNCIAS).

Falha original:
- Uma atualização baseada em estado antigo sobrescrevia uma transição
  já confirmada por outra instância.
- Duas tarefas conseguiam começar simultaneamente quando o limite era
  um, porque "checar capacidade" e "escrever RUNNING" eram passos
  separados vulneráveis a corrida entre instâncias.

Corrigido com UPDATE condicional (versão) para transições, e um único
UPDATE atômico (capacidade + versão + estado juntos) para iniciar jobs.
"""


class PersistenceTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)


class CRMLostUpdateTests(PersistenceTestBase):
    def test_stale_update_cannot_revert_confirmed_terminal_state(self):
        crm_a = CRM(db_path=self.db_path)
        crm_b = CRM(db_path=self.db_path)
        lead = crm_a.create_lead("chan_1", source="youtube")

        crm_b.get(lead.lead_id)
        crm_a.transition(lead.lead_id, LeadStatus.LOST)

        with self.assertRaises(InvalidLeadTransition):
            crm_b.transition(lead.lead_id, LeadStatus.ENGAGED)

        self.assertEqual(crm_a.get(lead.lead_id).status, LeadStatus.LOST)

    def test_concurrent_valid_transitions_from_different_instances_both_eventually_consistent(self):
        crm_a = CRM(db_path=self.db_path)
        crm_b = CRM(db_path=self.db_path)
        lead = crm_a.create_lead("chan_1", source="youtube")

        crm_a.transition(lead.lead_id, LeadStatus.ENGAGED)
        result = crm_b.transition(lead.lead_id, LeadStatus.QUALIFIED)
        self.assertEqual(result.status, LeadStatus.QUALIFIED)

    def test_real_concurrent_threads_racing_on_same_lead_no_lost_update(self):
        crm_a = CRM(db_path=self.db_path)
        crm_b = CRM(db_path=self.db_path)
        lead = crm_a.create_lead("chan_1", source="youtube")

        barrier = threading.Barrier(2)
        results = {}

        # LOST e UNSUBSCRIBED são ambos alcançáveis a partir de NEW, mas
        # AMBOS são estados terminais — uma vez que um se aplica, o
        # outro se torna estruturalmente impossível (nenhuma transição
        # sai de um estado terminal). Isso garante uma disputa
        # genuinamente exclusiva (diferente de ENGAGED/LOST, onde LOST
        # também é alcançável a partir de ENGAGED — nesse caso as duas
        # tentativas podem legitimamente suceder em sequência via retry,
        # o que não seria um "lost update").
        def attempt(crm, name, target):
            barrier.wait()
            try:
                results[name] = crm.transition(lead.lead_id, target).status
            except (InvalidLeadTransition, ConcurrentModificationError) as e:
                results[name] = type(e).__name__

        t1 = threading.Thread(target=attempt, args=(crm_a, "A", LeadStatus.LOST))
        t2 = threading.Thread(target=attempt, args=(crm_b, "B", LeadStatus.UNSUBSCRIBED))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exatamente uma das duas deve ter vencido (o estado final é
        # terminal e corresponde a uma delas); a outra deve ter sido
        # recusada por InvalidLeadTransition (ao relêr o estado já
        # terminal deixado pela vencedora) — nunca as duas "sucedendo"
        # e nunca um estado corrompido/misto.
        final = crm_a.get(lead.lead_id)
        self.assertIn(final.status, (LeadStatus.LOST, LeadStatus.UNSUBSCRIBED))

        winner_result = final.status
        loser_name = "B" if results["A"] == winner_result else "A"
        self.assertEqual(results[loser_name], "InvalidLeadTransition")

        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT version FROM leads WHERE lead_id=?", (lead.lead_id,)).fetchone()
        self.assertEqual(row["version"], 1, "exatamente uma transição deve ter sido aplicada com sucesso")


class JobLostUpdateTests(PersistenceTestBase):
    def test_stale_complete_cannot_revert_cancelled_state(self):
        q_a = JobQueue(db_path=self.db_path)
        q_b = JobQueue(db_path=self.db_path)
        job = q_a.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")
        q_a.start(job.job_id)

        q_b.get(job.job_id)
        q_a.cancel(job.job_id)

        with self.assertRaises(InvalidJobTransition):
            q_b.complete(job.job_id, {"ok": True})

        self.assertEqual(q_a.get(job.job_id).state, JobState.CANCELLED)

    def test_stale_fail_cannot_revert_completed_state(self):
        q_a = JobQueue(db_path=self.db_path)
        q_b = JobQueue(db_path=self.db_path)
        job = q_a.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")
        q_a.start(job.job_id)
        q_b.get(job.job_id)

        q_a.complete(job.job_id, {"done": True})

        with self.assertRaises(InvalidJobTransition):
            q_b.fail(job.job_id, "erro tardio")

        self.assertEqual(q_a.get(job.job_id).state, JobState.COMPLETED)


class AtomicCapacityTests(PersistenceTestBase):
    def test_capacity_one_only_one_job_ends_up_running_with_two_instances(self):
        queue_setup = JobQueue(db_path=self.db_path, max_concurrent_running=1)
        job_a = queue_setup.enqueue(job_type="produce_video", idempotency_key="a", channel_id="c1")
        job_b = queue_setup.enqueue(job_type="produce_video", idempotency_key="b", channel_id="c1")

        q1 = JobQueue(db_path=self.db_path, max_concurrent_running=1)
        q2 = JobQueue(db_path=self.db_path, max_concurrent_running=1)

        barrier = threading.Barrier(2)
        results = {}

        def start_job(queue, job_id, name):
            barrier.wait()
            results[name] = queue.start(job_id).state

        t1 = threading.Thread(target=start_job, args=(q1, job_a.job_id, "A"))
        t2 = threading.Thread(target=start_job, args=(q2, job_b.job_id, "B"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        states = {results["A"], results["B"]}
        self.assertIn(JobState.RUNNING, states)
        self.assertIn(JobState.WAITING, states)

        with db_module.connect(self.db_path) as conn:
            running_count = conn.execute(
                "SELECT COUNT(*) as n FROM jobs WHERE state='RUNNING'"
            ).fetchone()["n"]
        self.assertEqual(running_count, 1, "no máximo um job RUNNING com limite 1")

    def test_capacity_respected_under_many_concurrent_attempts(self):
        queue_setup = JobQueue(db_path=self.db_path, max_concurrent_running=2)
        jobs = [
            queue_setup.enqueue(job_type="produce_video", idempotency_key=f"k{i}", channel_id="c1")
            for i in range(6)
        ]

        barrier = threading.Barrier(6)
        results = [None] * 6

        def start_job(i):
            queue = JobQueue(db_path=self.db_path, max_concurrent_running=2)
            barrier.wait()
            results[i] = queue.start(jobs[i].job_id).state

        threads = [threading.Thread(target=start_job, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        running_count = sum(1 for s in results if s == JobState.RUNNING)
        self.assertEqual(running_count, 2, "exatamente o limite de 2 deve ter conseguido iniciar")

        with db_module.connect(self.db_path) as conn:
            db_running_count = conn.execute(
                "SELECT COUNT(*) as n FROM jobs WHERE state='RUNNING'"
            ).fetchone()["n"]
        self.assertEqual(db_running_count, 2)

    def test_no_job_started_twice_as_independent_executions(self):
        queue_setup = JobQueue(db_path=self.db_path, max_concurrent_running=5)
        job = queue_setup.enqueue(job_type="produce_video", idempotency_key="k1", channel_id="c1")

        q1 = JobQueue(db_path=self.db_path, max_concurrent_running=5)
        q2 = JobQueue(db_path=self.db_path, max_concurrent_running=5)

        barrier = threading.Barrier(2)
        results = {}

        def start_job(queue, name):
            barrier.wait()
            results[name] = queue.start(job.job_id)

        t1 = threading.Thread(target=start_job, args=(q1, "A"))
        t2 = threading.Thread(target=start_job, args=(q2, "B"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT attempts, state FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["state"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
