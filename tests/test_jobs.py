import time
import unittest

from core.jobs.queue import JobQueue
from core.models.entities import JobState


class JobQueueTests(unittest.TestCase):
    def test_enqueue_creates_job(self):
        q = JobQueue()
        job = q.enqueue(job_type="produce_video", idempotency_key="k1")
        self.assertEqual(job.state, JobState.QUEUED)

    def test_idempotency_same_key_returns_same_job(self):
        q = JobQueue()
        job1 = q.enqueue(job_type="produce_video", idempotency_key="k1")
        job2 = q.enqueue(job_type="produce_video", idempotency_key="k1")
        self.assertEqual(job1.job_id, job2.job_id)
        self.assertEqual(len(q.all()), 1)

    def test_start_and_complete_lifecycle(self):
        q = JobQueue()
        job = q.enqueue(job_type="produce_video", idempotency_key="k1")
        q.start(job.job_id)
        self.assertEqual(q.get(job.job_id).state, JobState.RUNNING)
        q.complete(job.job_id, {"ok": True})
        self.assertEqual(q.get(job.job_id).state, JobState.COMPLETED)

    def test_fail_retries_until_max_attempts(self):
        q = JobQueue()
        job = q.enqueue(job_type="produce_video", idempotency_key="k1", max_attempts=2)
        q.start(job.job_id)
        q.fail(job.job_id, "erro 1")
        self.assertEqual(q.get(job.job_id).state, JobState.RETRYING)
        q.start(job.job_id)
        q.fail(job.job_id, "erro 2")
        self.assertEqual(q.get(job.job_id).state, JobState.FAILED)

    def test_capacity_control_waits_when_full(self):
        q = JobQueue(max_concurrent_running=1)
        job1 = q.enqueue(job_type="t", idempotency_key="k1")
        job2 = q.enqueue(job_type="t", idempotency_key="k2")
        q.start(job1.job_id)
        q.start(job2.job_id)
        self.assertEqual(q.get(job2.job_id).state, JobState.WAITING)

    def test_recover_stuck_jobs(self):
        q = JobQueue(default_timeout_seconds=0.01)
        job = q.enqueue(job_type="t", idempotency_key="k1", max_attempts=2)
        q.start(job.job_id)
        time.sleep(0.05)
        recovered = q.recover_stuck_jobs()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(q.get(job.job_id).state, JobState.RETRYING)

    def test_circuit_breaker_opens_after_repeated_failures(self):
        q = JobQueue()
        breaker = q._breaker_for("flaky_job")
        for _ in range(5):
            breaker.record_failure()
        self.assertTrue(breaker.is_open())

        job = q.enqueue(job_type="flaky_job", idempotency_key="k1")
        q.start(job.job_id)
        self.assertEqual(q.get(job.job_id).state, JobState.WAITING)

    def test_cancel_job(self):
        q = JobQueue()
        job = q.enqueue(job_type="t", idempotency_key="k1")
        q.cancel(job.job_id)
        self.assertEqual(q.get(job.job_id).state, JobState.CANCELLED)


if __name__ == "__main__":
    unittest.main()
