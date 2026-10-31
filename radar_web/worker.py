"""
radar_web.worker
====================
CORREÇÃO (item 3 — RADAR WEB COMERCIAL: execução contínua real).

Mesmo padrão já hardened do `atlas.radar.worker.RadarWorker` — thread
stdlib, `threading.Event.wait()` (nunca busy loop), backoff
exponencial em falhas consecutivas, `start()`/`stop()` idempotentes,
nunca deixa thread órfã.
"""

from __future__ import annotations

import threading

from radar_web.scheduler import RadarWebScheduler


class WorkerShutdownTimeout(Exception):
    """MICROCORREÇÃO (shutdown residual — falha real reproduzida por
    auditoria independente): levantada por `RadarWebWorker.stop()`
    quando `thread.join(timeout)` esgota o prazo e a thread AINDA está
    viva. Nunca finge sucesso — o chamador precisa saber explicitamente
    que o worker não parou a tempo, nunca inferir isso indiretamente
    de um `is_running()` que mentiria `False` para uma thread ainda
    viva."""


class RadarWebWorker:
    def __init__(
        self, *, scheduler: RadarWebScheduler, poll_interval_seconds: float = 1.0,
        niche: str | None = None, max_backoff_seconds: float = 300.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds precisa ser positivo")
        self.scheduler = scheduler
        self.poll_interval_seconds = poll_interval_seconds
        self.niche = niche
        self.max_backoff_seconds = max_backoff_seconds

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._poll_count = 0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._consecutive_failures = 0
            self._thread = threading.Thread(target=self._run_loop, name="RadarWebWorker", daemon=True)
            self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        """MICROCORREÇÃO (shutdown residual — fail-closed): a
        referência da thread SÓ é descartada (`self._thread = None`)
        quando ela REALMENTE terminou (`thread.is_alive()` é `False`
        após o `join`). Antes, `self._thread = None` era atribuído
        incondicionalmente após o `join`, mesmo quando o timeout se
        esgotava com a thread ainda viva — isso fazia `is_running()`
        mentir `False` para uma thread genuinamente ainda em execução
        (falso sucesso de shutdown). Agora, se o timeout se esgota com
        a thread ainda viva, a referência é MANTIDA (nunca descartada)
        e `WorkerShutdownTimeout` é levantada explicitamente — o
        chamador nunca precisa inferir a falha indiretamente."""
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
        thread.join(timeout=timeout)
        with self._lock:
            if thread.is_alive():
                # CORREÇÃO CRÍTICA: nunca descarta a referência de uma
                # thread viva — `self._thread` permanece exatamente
                # como estava, para que `is_running()` continue
                # reportando `True` corretamente.
                raise WorkerShutdownTimeout(
                    f"RadarWebWorker não encerrou dentro de {timeout}s — thread ainda viva, referência preservada"
                )
            if self._thread is thread:
                self._thread = None

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def poll_count(self) -> int:
        return self._poll_count

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            sleep_for = self.poll_interval_seconds
            try:
                self.scheduler.run_if_due(niche=self.niche)
                self._consecutive_failures = 0
                self._last_error = None
            except Exception as exc:
                self._consecutive_failures += 1
                self._last_error = str(exc)
                backoff = self.poll_interval_seconds * (2 ** self._consecutive_failures)
                sleep_for = min(backoff, self.max_backoff_seconds)
            finally:
                self._poll_count += 1
            self._stop_event.wait(timeout=sleep_for)
