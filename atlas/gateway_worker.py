from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
import threading
import time
from typing import Any

from core.jobs.queue import JobQueue, JobState


ATLAS_TASK_JOB = "ATLAS_TASK"


class AtlasGatewayWorkerShutdownTimeout(RuntimeError):
    pass


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Conservative serialization for agent results."""
    if depth > 8:
        return "<max-depth>"

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Enum):
        return _json_safe(value.value, depth=depth + 1)

    if is_dataclass(value):
        return _json_safe(asdict(value), depth=depth + 1)

    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key)

            # Never return obvious secrets through the Gateway.
            if any(
                token in key_text.lower()
                for token in (
                    "password",
                    "secret",
                    "api_key",
                    "apikey",
                    "token",
                    "authorization",
                    "cookie",
                )
            ):
                out[key_text] = "<redacted>"
            else:
                out[key_text] = _json_safe(
                    item,
                    depth=depth + 1,
                )

        return out

    if isinstance(value, (list, tuple, set)):
        return [
            _json_safe(item, depth=depth + 1)
            for item in value
        ]

    if hasattr(value, "__dict__"):
        public = {
            k: v
            for k, v in vars(value).items()
            if not k.startswith("_")
        }

        return _json_safe(
            public,
            depth=depth + 1,
        )

    return str(value)[:4096]


class AtlasGatewayWorker:
    def __init__(
        self,
        *,
        job_queue: JobQueue,
        atlas,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self.job_queue = job_queue
        self.atlas = atlas
        self.poll_interval_seconds = max(
            0.05,
            float(poll_interval_seconds),
        )

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def is_running(self) -> bool:
        with self._lock:
            return (
                self._thread is not None
                and self._thread.is_alive()
            )

    def start(self) -> None:
        """Idempotent: never creates a duplicate live worker."""
        with self._lock:
            if (
                self._thread is not None
                and self._thread.is_alive()
            ):
                return

            self._stop_event.clear()

            self._thread = threading.Thread(
                target=self._run_loop,
                name="AtlasGatewayWorker",
                daemon=True,
            )

            self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        with self._lock:
            thread = self._thread

            if thread is None:
                return

            self._stop_event.set()

        thread.join(timeout=timeout)

        with self._lock:
            if thread.is_alive():
                raise AtlasGatewayWorkerShutdownTimeout(
                    f"ATLAS Gateway worker did not stop "
                    f"within {timeout}s"
                )

            if self._thread is thread:
                self._thread = None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():

            handled = False

            for job in list(self.job_queue.all()):

                if job.job_type != ATLAS_TASK_JOB:
                    continue

                if job.state not in (
                    JobState.PENDING,
                    JobState.QUEUED,
                    JobState.RETRYING,
                ):
                    continue

                started = self.job_queue.try_start(
                    job.job_id
                )

                if started is None:
                    continue

                handled = True

                self._execute(started)

                if self._stop_event.is_set():
                    break

            if not handled:
                self._stop_event.wait(
                    self.poll_interval_seconds
                )

    def _execute(self, job) -> None:
        try:
            payload = dict(job.payload or {})

            task_type = str(
                payload.pop("task_type")
            )

            preferred_agent = payload.pop(
                "preferred_agent",
                None,
            )

            required_capability = payload.pop(
                "required_capability",
                None,
            )

            # First version deliberately does not convert arbitrary
            # strings into capability objects.
            if required_capability is not None:
                raise ValueError(
                    "required_capability external decoding "
                    "is not enabled yet"
                )

            result = self.atlas.execute_agent_for_task(
                task_type=task_type,
                payload=payload,
                preferred_agent=preferred_agent,
            )

            normalized = _json_safe(result)

            if not isinstance(normalized, dict):
                normalized = {
                    "value": normalized,
                }

            self.job_queue.complete(
                job.job_id,
                normalized,
            )

        except Exception as exc:
            self.job_queue.fail(
                job.job_id,
                f"{type(exc).__name__}: {exc}",
            )
