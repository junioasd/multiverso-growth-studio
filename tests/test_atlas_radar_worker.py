import os
import tempfile
import threading
import time
import unittest
from tests._auth_test_helpers import fast_hash_password

from atlas.radar.engine import CommercialOpportunityRadar
from atlas.radar.scheduler import RadarScheduler
from atlas.radar.worker import RadarWorker
from core.jobs.queue import JobQueue
from core.models import db as db_module

"""
Testes ATLAS+RADAR - MICROCORRECAO FINAL (WORKER/LOOP AUTONOMO DO
RADAR NO RUNTIME).

CORREÇÃO (item 8/9/11 — diagnóstico da regressão monolítica): este
arquivo é um dos mais antigos do projeto e criava `RadarWorker`
diretamente em cada teste, muitas vezes chamando `worker.stop()` sem
`try/finally` — uma falha de asserção NO MEIO do teste (antes de
chegar ao `stop()`) deixava a thread do worker viva, rodando sozinha
pelo resto da execução da suíte inteira (poll_interval tão baixo
quanto 0.03s), contribuindo para lentidão/contenção acumulada em
execuções monolíticas longas. Agora TODA classe usa `_WorkerTrackingTestCase`
— `tearDown` sempre para qualquer worker rastreado, mesmo em falha.
"""


class _WorkerTrackingTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tracked_workers: list = []

    def tearDown(self):
        for worker in self._tracked_workers:
            try:
                worker.stop(timeout=5.0)
            except Exception:
                pass
        super().tearDown()

    def make_worker(self, *, db_path=None, poll_interval_seconds=0.05, max_concurrent_running=5, radar=None):
        job_queue = JobQueue(db_path=db_path, max_concurrent_running=max_concurrent_running)
        radar = radar if radar is not None else CommercialOpportunityRadar()
        scheduler = RadarScheduler(job_queue=job_queue, radar=radar, db_path=db_path)
        worker = RadarWorker(scheduler=scheduler, poll_interval_seconds=poll_interval_seconds)
        self._tracked_workers.append(worker)
        return worker, scheduler, radar


class AutomaticExecutionTests(_WorkerTrackingTestCase):
    def test_never_calls_run_if_due_manually_still_executes_automatically(self):
        worker, scheduler, _ = self.make_worker()
        scheduler.enable(interval_seconds=0.2)
        self.assertEqual(scheduler.status().last_run_status, "NEVER_RUN")

        worker.start()
        deadline = time.time() + 3.0
        while scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
            time.sleep(0.05)
        status = scheduler.status()
        worker.stop()

        self.assertEqual(status.last_run_status, "SUCCESS")
        self.assertIsNotNone(status.last_run_at)

    def test_next_run_rescheduled_automatically(self):
        worker, scheduler, _ = self.make_worker()
        scheduler.enable(interval_seconds=0.2)
        original_next_run = scheduler.status().next_run_at

        worker.start()
        deadline = time.time() + 3.0
        while scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
            time.sleep(0.05)
        status = scheduler.status()
        worker.stop()

        self.assertNotEqual(status.next_run_at, original_next_run)
        self.assertGreater(status.next_run_at, status.last_run_at)

    def test_automatic_execution_repeats_across_multiple_cycles(self):
        worker, scheduler, _ = self.make_worker()
        scheduler.enable(interval_seconds=0.15)

        worker.start()
        seen_last_run_values = set()
        deadline = time.time() + 3.0
        while len(seen_last_run_values) < 3 and time.time() < deadline:
            status = scheduler.status()
            if status.last_run_at is not None:
                seen_last_run_values.add(status.last_run_at)
            time.sleep(0.05)
        worker.stop()

        self.assertGreaterEqual(len(seen_last_run_values), 2)


class DisableEnableTests(_WorkerTrackingTestCase):
    def test_disable_stops_further_automatic_executions(self):
        worker, scheduler, _ = self.make_worker()
        scheduler.enable(interval_seconds=0.15)
        worker.start()

        deadline = time.time() + 3.0
        while scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
            time.sleep(0.05)

        scheduler.disable()
        last_run_at_disabled = scheduler.status().last_run_at
        time.sleep(0.6)
        worker.stop()

        self.assertEqual(scheduler.status().last_run_at, last_run_at_disabled)

    def test_enable_again_resumes_automatic_execution(self):
        worker, scheduler, _ = self.make_worker()
        scheduler.enable(interval_seconds=0.15)
        worker.start()

        deadline = time.time() + 3.0
        while scheduler.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
            time.sleep(0.05)
        scheduler.disable()
        last_run_before_pause = scheduler.status().last_run_at
        time.sleep(0.3)

        scheduler.enable(interval_seconds=0.15)
        deadline = time.time() + 3.0
        while scheduler.status().last_run_at == last_run_before_pause and time.time() < deadline:
            time.sleep(0.05)
        worker.stop()

        self.assertNotEqual(scheduler.status().last_run_at, last_run_before_pause)


class RestartTests(_WorkerTrackingTestCase):
    def setUp(self):
        super().setUp()
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        super().tearDown()
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def test_restart_preserves_config_and_resumes_cycle(self):
        worker1, scheduler1, _ = self.make_worker(db_path=self.db_path)
        scheduler1.enable(interval_seconds=3600)
        worker1.start()

        deadline = time.time() + 3.0
        while scheduler1.status().last_run_status == "NEVER_RUN" and time.time() < deadline:
            time.sleep(0.05)
        worker1.stop()
        status_before_restart = scheduler1.status()
        self.assertEqual(status_before_restart.last_run_status, "SUCCESS")

        worker2, scheduler2, _ = self.make_worker(db_path=self.db_path)
        status_after_restart = scheduler2.status()
        self.assertTrue(status_after_restart.enabled)
        self.assertEqual(status_after_restart.interval_seconds, 3600)
        self.assertEqual(status_after_restart.last_run_status, "SUCCESS")

        worker2.start()
        time.sleep(0.4)
        worker2.stop()

        self.assertEqual(scheduler2.status().last_run_at, status_before_restart.last_run_at)

    def test_worker_never_running_immediately_after_construction(self):
        worker, _, _ = self.make_worker(db_path=self.db_path)
        self.assertFalse(worker.is_running())


class ConcurrencyTests(_WorkerTrackingTestCase):
    def setUp(self):
        super().setUp()
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        super().tearDown()
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)

    def test_multiple_workers_same_db_never_duplicate_scan(self):
        workers = []
        for _ in range(4):
            w, s, _ = self.make_worker(db_path=self.db_path, poll_interval_seconds=0.03)
            workers.append((w, s))

        workers[0][1].enable(interval_seconds=0.3)

        for w, _ in workers:
            w.start()

        time.sleep(1.2)

        for w, _ in workers:
            w.stop()

        with db_module.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT job_id, attempts FROM jobs WHERE job_type='atlas_radar_scan'"
            ).fetchall()
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertEqual(row["attempts"], 1)


class ErrorResilienceTests(_WorkerTrackingTestCase):
    def test_temporary_scan_failure_does_not_kill_worker(self):
        class FlakyRadar(CommercialOpportunityRadar):
            def __init__(self):
                super().__init__()
                self.call_count = 0

            def scan(self, **kwargs):
                self.call_count += 1
                if self.call_count <= 2:
                    raise RuntimeError(f"falha simulada #{self.call_count}")
                return super().scan(**kwargs)

        radar = FlakyRadar()
        worker, scheduler, _ = self.make_worker(radar=radar, poll_interval_seconds=0.05)
        scheduler.enable(interval_seconds=0.05)
        worker.max_backoff_seconds = 1.0

        worker.start()
        deadline = time.time() + 3.0
        while radar.call_count < 3 and time.time() < deadline:
            time.sleep(0.05)
        still_alive_during_failures = worker.is_running()
        while scheduler.status().last_run_status != "SUCCESS" and time.time() < deadline:
            time.sleep(0.05)
        worker.stop()

        self.assertTrue(still_alive_during_failures)
        self.assertGreaterEqual(radar.call_count, 3)
        self.assertEqual(scheduler.status().last_run_status, "SUCCESS")

    def test_backoff_increases_wait_after_consecutive_failures(self):
        class AlwaysFailingRadar(CommercialOpportunityRadar):
            def scan(self, **kwargs):
                raise RuntimeError("sempre falha")

        worker, scheduler, _ = self.make_worker(radar=AlwaysFailingRadar(), poll_interval_seconds=0.05)
        worker.max_backoff_seconds = 0.5
        scheduler.enable(interval_seconds=0.05)

        worker.start()
        time.sleep(1.5)
        worker.stop()

        self.assertGreater(worker.consecutive_failures, 0)
        self.assertIsNotNone(worker.last_error)
        self.assertLess(worker.poll_count, 30)


class CleanShutdownTests(_WorkerTrackingTestCase):
    def test_stop_joins_thread_cleanly(self):
        worker, scheduler, _ = self.make_worker(poll_interval_seconds=0.05)
        scheduler.enable(interval_seconds=0.1)
        worker.start()
        time.sleep(0.2)
        self.assertTrue(worker.is_running())

        thread_names_before = {t.name for t in threading.enumerate()}
        self.assertIn("RadarWorker", thread_names_before)

        worker.stop()

        self.assertFalse(worker.is_running())
        thread_names_after = {t.name for t in threading.enumerate()}
        self.assertNotIn("RadarWorker", thread_names_after)

    def test_stop_without_start_is_safe_noop(self):
        worker, _, _ = self.make_worker()
        worker.stop()
        self.assertFalse(worker.is_running())

    def test_start_is_idempotent_never_spawns_second_thread(self):
        worker, scheduler, _ = self.make_worker(poll_interval_seconds=0.1)
        scheduler.enable(interval_seconds=3600)
        worker.start()
        first_thread = worker._thread
        worker.start()
        second_thread = worker._thread
        worker.stop()

        self.assertIs(first_thread, second_thread)

    def test_stop_is_idempotent(self):
        worker, scheduler, _ = self.make_worker(poll_interval_seconds=0.05)
        scheduler.enable(interval_seconds=3600)
        worker.start()
        time.sleep(0.1)
        worker.stop()
        worker.stop()
        self.assertFalse(worker.is_running())

    def test_app_shutdown_stops_radar_worker(self):
        from api.bootstrap import build_app

        app = build_app(
            owner_email="owner@x.com", owner_password_hash=fast_hash_password("StrongPass123"),
            persistent=False, start_radar_worker=False,
        )
        app.atlas.radar_worker.poll_interval_seconds = 0.05
        app.atlas.enable_radar_schedule(interval_seconds=3600)
        app.atlas.start_radar_worker()
        self.assertTrue(app.atlas.radar_worker.is_running())

        app.shutdown()
        self.assertFalse(app.atlas.radar_worker.is_running())


class NeverExecutesCriticalActionAutomaticallyTests(_WorkerTrackingTestCase):
    def test_automatic_cycles_never_create_escalations(self):
        worker, scheduler, _ = self.make_worker(poll_interval_seconds=0.05)
        scheduler.enable(interval_seconds=0.1)
        worker.start()
        time.sleep(0.5)
        worker.stop()
        self.assertFalse(hasattr(worker, "escalation_desk"))
        self.assertFalse(hasattr(scheduler, "escalation_desk"))


if __name__ == "__main__":
    unittest.main()
