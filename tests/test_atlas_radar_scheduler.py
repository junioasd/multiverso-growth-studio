import os
import tempfile
import threading
import unittest

from atlas.radar.engine import CommercialOpportunityRadar
from atlas.radar.scheduler import RadarScheduleDisabled, RadarScheduler
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.models.entities import JobState

"""
Testes ATLAS+RADAR - CORRECAO RESIDUAL 1 (EXECUCAO RECORRENTE DO
RADAR).
"""


class InMemorySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.radar = CommercialOpportunityRadar()
        self.scheduler = RadarScheduler(job_queue=JobQueue(), radar=self.radar)

    def test_disabled_by_default(self):
        self.assertFalse(self.scheduler.status().enabled)
        self.assertEqual(self.scheduler.status().last_run_status, "NEVER_RUN")

    def test_enable_sets_interval_and_due_immediately(self):
        state = self.scheduler.enable(interval_seconds=120)
        self.assertTrue(state.enabled)
        self.assertEqual(state.interval_seconds, 120)
        self.assertIsNotNone(state.next_run_at)

    def test_disable_stops_further_runs(self):
        self.scheduler.enable(interval_seconds=60)
        self.scheduler.disable()
        self.assertFalse(self.scheduler.status().enabled)
        result = self.scheduler.run_if_due()
        self.assertIsNone(result)

    def test_run_if_due_returns_none_when_disabled(self):
        result = self.scheduler.run_if_due()
        self.assertIsNone(result)

    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):
            self.scheduler.enable(interval_seconds=0)
        with self.assertRaises(ValueError):
            self.scheduler.enable(interval_seconds=-10)

    def test_run_records_last_run_and_next_run(self):
        self.scheduler.enable(interval_seconds=3600)
        job = self.scheduler.run_if_due(niche="saude")
        self.assertEqual(job.state, JobState.COMPLETED)
        status = self.scheduler.status()
        self.assertEqual(status.last_run_status, "SUCCESS")
        self.assertIsNotNone(status.last_run_at)
        self.assertIsNotNone(status.next_run_at)
        self.assertGreater(status.next_run_at, status.last_run_at)

    def test_run_not_due_before_interval_elapses(self):
        self.scheduler.enable(interval_seconds=3600)
        self.scheduler.run_if_due(niche="saude")
        second = self.scheduler.run_if_due(niche="saude")
        self.assertIsNone(second)

    def test_run_due_again_after_interval_elapses(self):
        self.scheduler.enable(interval_seconds=10)
        first = self.scheduler.run_if_due(niche="saude")  # tempo real, já devido
        self.assertEqual(first.state, JobState.COMPLETED)
        later = self.scheduler.status().next_run_at + 1  # passou do próximo ciclo
        second = self.scheduler.run_if_due(niche="saude", now=later)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.job_id, second.job_id)

    def test_force_run_requires_enabled(self):
        with self.assertRaises(RadarScheduleDisabled):
            self.scheduler.force_run()

    def test_force_run_works_when_enabled(self):
        self.scheduler.enable(interval_seconds=3600)
        job = self.scheduler.force_run(niche="saude")
        self.assertEqual(job.state, JobState.COMPLETED)


class NeverExecutesCriticalActionTests(unittest.TestCase):
    def test_scheduled_run_never_touches_escalation(self):
        radar = CommercialOpportunityRadar()
        scheduler = RadarScheduler(job_queue=JobQueue(), radar=radar)
        public_attrs = {a for a in dir(scheduler) if not a.startswith("_")}
        self.assertNotIn("escalation_desk", public_attrs)
        self.assertNotIn("request_critical_action", public_attrs)

    def test_scheduled_scan_only_produces_opportunities_never_executes_them(self):
        radar = CommercialOpportunityRadar()
        scheduler = RadarScheduler(job_queue=JobQueue(), radar=radar)
        scheduler.enable(interval_seconds=3600)
        job = scheduler.run_if_due(niche="saude")
        self.assertIn("opportunities_found", job.result)
        self.assertEqual(set(job.result.keys()), {"opportunities_found"})


class MockEvidencePreservedInScheduledPathTests(unittest.TestCase):
    def test_scheduled_scan_still_classifies_unknown(self):
        from atlas.radar.models import EvidenceLevel

        radar = CommercialOpportunityRadar()
        scheduler = RadarScheduler(job_queue=JobQueue(), radar=radar)
        scheduler.enable(interval_seconds=3600)
        scheduler.run_if_due(niche="saude")

        tracked = radar.tracked_opportunities()
        self.assertGreater(len(tracked), 0)
        for t in tracked:
            for signal in t.opportunity.signals.values():
                self.assertEqual(signal.evidence, EvidenceLevel.UNKNOWN)


class PersistenceTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)


class RestartSurvivalTests(PersistenceTestBase):
    def test_enabled_state_survives_restart(self):
        radar1 = CommercialOpportunityRadar()
        scheduler1 = RadarScheduler(job_queue=JobQueue(db_path=self.db_path), radar=radar1, db_path=self.db_path)
        scheduler1.enable(interval_seconds=1800)

        radar2 = CommercialOpportunityRadar()
        scheduler2 = RadarScheduler(job_queue=JobQueue(db_path=self.db_path), radar=radar2, db_path=self.db_path)
        status2 = scheduler2.status()
        self.assertTrue(status2.enabled)
        self.assertEqual(status2.interval_seconds, 1800)

    def test_run_history_survives_restart(self):
        radar1 = CommercialOpportunityRadar()
        scheduler1 = RadarScheduler(job_queue=JobQueue(db_path=self.db_path), radar=radar1, db_path=self.db_path)
        scheduler1.enable(interval_seconds=3600)
        scheduler1.run_if_due(niche="saude")

        radar2 = CommercialOpportunityRadar()
        scheduler2 = RadarScheduler(job_queue=JobQueue(db_path=self.db_path), radar=radar2, db_path=self.db_path)
        status2 = scheduler2.status()
        self.assertEqual(status2.last_run_status, "SUCCESS")
        self.assertIsNotNone(status2.last_run_at)

    def test_next_run_respected_after_restart_no_immediate_rerun(self):
        radar1 = CommercialOpportunityRadar()
        scheduler1 = RadarScheduler(job_queue=JobQueue(db_path=self.db_path), radar=radar1, db_path=self.db_path)
        scheduler1.enable(interval_seconds=3600)
        scheduler1.run_if_due(niche="saude")

        radar2 = CommercialOpportunityRadar()
        scheduler2 = RadarScheduler(job_queue=JobQueue(db_path=self.db_path), radar=radar2, db_path=self.db_path)
        job2 = scheduler2.run_if_due(niche="saude")
        self.assertIsNone(job2)

    def test_never_auto_enabled_by_restart(self):
        radar1 = CommercialOpportunityRadar()
        scheduler1 = RadarScheduler(job_queue=JobQueue(db_path=self.db_path), radar=radar1, db_path=self.db_path)
        self.assertFalse(scheduler1.status().enabled)

        radar2 = CommercialOpportunityRadar()
        scheduler2 = RadarScheduler(job_queue=JobQueue(db_path=self.db_path), radar=radar2, db_path=self.db_path)
        self.assertFalse(scheduler2.status().enabled)


class IdempotencyAndConcurrencyTests(PersistenceTestBase):
    def test_same_cycle_never_creates_two_jobs_persisted(self):
        job_queue = JobQueue(db_path=self.db_path)
        radar = CommercialOpportunityRadar()
        scheduler = RadarScheduler(job_queue=job_queue, radar=radar, db_path=self.db_path)
        scheduler.enable(interval_seconds=3600)

        fixed_now = 5000.0
        scheduler.store.set_enabled(True, interval_seconds=3600)
        scheduler.run_if_due(niche="saude", now=fixed_now)

        job_queue2 = JobQueue(db_path=self.db_path)
        scheduler2 = RadarScheduler(job_queue=job_queue2, radar=radar, db_path=self.db_path)
        scheduler2.store.record_run(status="NEVER_RUN", now=fixed_now - 1, next_run_at=fixed_now - 1)
        scheduler2.run_if_due(niche="saude", now=fixed_now)

        with db_module.connect(self.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) as n FROM jobs WHERE job_type='atlas_radar_scan'"
            ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_concurrent_run_if_due_only_one_execution_real_threads(self):
        job_queue = JobQueue(db_path=self.db_path, max_concurrent_running=5)
        radar = CommercialOpportunityRadar()
        scheduler = RadarScheduler(job_queue=job_queue, radar=radar, db_path=self.db_path)
        scheduler.enable(interval_seconds=3600)

        fixed_now = 9999.0
        scheduler.store.record_run(status="NEVER_RUN", now=fixed_now - 1, next_run_at=fixed_now - 1)

        results = []
        barrier = threading.Barrier(4)

        def attempt():
            q = JobQueue(db_path=self.db_path, max_concurrent_running=5)
            s = RadarScheduler(job_queue=q, radar=CommercialOpportunityRadar(), db_path=self.db_path)
            barrier.wait()
            results.append(s.run_if_due(niche="saude", now=fixed_now))

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with db_module.connect(self.db_path) as conn:
            jobs = conn.execute(
                "SELECT * FROM jobs WHERE job_type='atlas_radar_scan'"
            ).fetchall()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["attempts"], 1)

    def test_retry_after_failure_does_not_duplicate_within_same_cycle(self):
        class FailingRadar(CommercialOpportunityRadar):
            def scan(self, **kwargs):
                raise RuntimeError("falha simulada na descoberta")

        job_queue = JobQueue(db_path=self.db_path)
        scheduler = RadarScheduler(job_queue=job_queue, radar=FailingRadar(), db_path=self.db_path)
        scheduler.enable(interval_seconds=3600)

        with self.assertRaises(RuntimeError):
            scheduler.run_if_due(niche="saude")

        status = scheduler.status()
        self.assertEqual(status.last_run_status, "FAILED")

        with db_module.connect(self.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) as n FROM jobs WHERE job_type='atlas_radar_scan'"
            ).fetchone()["n"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
