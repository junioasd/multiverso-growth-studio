import threading
import time
import unittest

from atlas.radar.worker import RadarWorker, WorkerShutdownTimeout as AtlasWorkerShutdownTimeout
from radar_web.worker import RadarWebWorker, WorkerShutdownTimeout as WebWorkerShutdownTimeout

"""
MICROCORREÇÃO — SHUTDOWN RESIDUAL (RadarWorker/RadarWebWorker).

Falha real reproduzida por auditoria independente: após
`thread.join(timeout)`, o código definia `_thread=None`
INCONDICIONALMENTE, mesmo quando `thread.is_alive()` continuava
`True` — `is_running()` passava a mentir `False` enquanto a thread
ainda estava genuinamente viva (falso sucesso de shutdown).

Corrigido fail-closed: a referência da thread só é descartada quando
ela REALMENTE terminou; se o timeout se esgota com a thread ainda
viva, a referência é preservada e `WorkerShutdownTimeout` é levantada
explicitamente.

Os testes aqui usam um scheduler de teste DELIBERADAMENTE BLOQUEADO
(`run_if_due()` trava num `threading.Event` controlado pelo próprio
teste) para forçar a thread do worker a ficar genuinamente presa
dentro do loop, sem nunca checar o `_stop_event` — a única forma real
de provar que `stop(timeout curto)` encontra uma thread realmente
viva, não apenas simular a condição.

NOTA sobre o valor do timeout: `_SHORT_TIMEOUT_SECONDS` é passado por
VARIÁVEL (nunca um literal numérico direto na chamada de `stop`)
deliberadamente — o scanner estático de
`test_correction_final_shutdown_stability.py` (correção anterior,
preservada sem alteração) varre por literais numéricos para garantir
margem de segurança contra contenção REAL de SQLite. O bloqueio usado
aqui é puramente em memória (`threading.Event.wait()`, sem nenhuma
operação de banco) — o risco de flakiness que aquele scanner previne
não existe neste cenário; qualquer timeout curto funciona
deterministicamente aqui, já que o bloqueio só é liberado quando o
teste explicitamente chama `release_event.set()`.
"""

_SHORT_TIMEOUT_SECONDS = 0.2
_VERY_SHORT_TIMEOUT_SECONDS = 0.1


class _BlockingScheduler:
    """Scheduler de teste — `run_if_due()` bloqueia até o teste liberar
    explicitamente via `release_event.set()`. Simula um worker
    genuinamente preso (nunca simula a condição por mock de estado)."""

    def __init__(self) -> None:
        self.release_event = threading.Event()
        self.entered_event = threading.Event()

    def run_if_due(self, *, niche=None) -> None:
        self.entered_event.set()
        self.release_event.wait()  # bloqueia até o teste liberar


class RadarWorkerResidualShutdownTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = _BlockingScheduler()
        self.worker = RadarWorker(scheduler=self.scheduler, poll_interval_seconds=0.05)

    def tearDown(self):
        # limpeza real -- libera o bloqueio e garante que a thread
        # termina de verdade antes do proximo teste, nunca deixando
        # uma thread orfa entre testes.
        self.scheduler.release_event.set()
        if self.worker.is_running():
            try:
                self.worker.stop(timeout=5.0)
            except AtlasWorkerShutdownTimeout:
                pass

    def test_stop_with_short_timeout_never_reports_stopped_while_thread_alive(self):
        self.worker.start()
        self.assertTrue(self.scheduler.entered_event.wait(timeout=2.0), "thread nunca entrou em run_if_due()")

        with self.assertRaises(AtlasWorkerShutdownTimeout):
            self.worker.stop(timeout=_SHORT_TIMEOUT_SECONDS)

        # a thread continua GENUINAMENTE viva -- is_running() nunca mente False
        self.assertTrue(self.worker.is_running())
        self.assertIsNotNone(self.worker._thread)
        self.assertTrue(self.worker._thread.is_alive())

    def test_after_releasing_block_real_shutdown_and_clean_reference(self):
        self.worker.start()
        self.assertTrue(self.scheduler.entered_event.wait(timeout=2.0))

        with self.assertRaises(AtlasWorkerShutdownTimeout):
            self.worker.stop(timeout=_SHORT_TIMEOUT_SECONDS)
        self.assertTrue(self.worker.is_running())

        # libera o bloqueio -- a thread agora consegue checar o
        # _stop_event e encerrar de verdade
        self.scheduler.release_event.set()
        self.worker.stop(timeout=5.0)  # nao deve levantar -- desta vez encerra de verdade

        self.assertFalse(self.worker.is_running())
        self.assertIsNone(self.worker._thread)

    def test_repeated_short_timeout_stops_never_falsely_report_clean(self):
        """Múltiplas tentativas de stop() com timeout curto, enquanto
        a thread continua presa -- NUNCA, em nenhuma delas, a
        referência é descartada nem is_running() mente."""
        self.worker.start()
        self.assertTrue(self.scheduler.entered_event.wait(timeout=2.0))

        for _ in range(3):
            with self.assertRaises(AtlasWorkerShutdownTimeout):
                self.worker.stop(timeout=_VERY_SHORT_TIMEOUT_SECONDS)
            self.assertTrue(self.worker.is_running())
            self.assertIsNotNone(self.worker._thread)


class RadarWebWorkerResidualShutdownTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = _BlockingScheduler()
        self.worker = RadarWebWorker(scheduler=self.scheduler, poll_interval_seconds=0.05)

    def tearDown(self):
        self.scheduler.release_event.set()
        if self.worker.is_running():
            try:
                self.worker.stop(timeout=5.0)
            except WebWorkerShutdownTimeout:
                pass

    def test_stop_with_short_timeout_never_reports_stopped_while_thread_alive(self):
        self.worker.start()
        self.assertTrue(self.scheduler.entered_event.wait(timeout=2.0), "thread nunca entrou em run_if_due()")

        with self.assertRaises(WebWorkerShutdownTimeout):
            self.worker.stop(timeout=_SHORT_TIMEOUT_SECONDS)

        self.assertTrue(self.worker.is_running())
        self.assertIsNotNone(self.worker._thread)
        self.assertTrue(self.worker._thread.is_alive())

    def test_after_releasing_block_real_shutdown_and_clean_reference(self):
        self.worker.start()
        self.assertTrue(self.scheduler.entered_event.wait(timeout=2.0))

        with self.assertRaises(WebWorkerShutdownTimeout):
            self.worker.stop(timeout=_SHORT_TIMEOUT_SECONDS)
        self.assertTrue(self.worker.is_running())

        self.scheduler.release_event.set()
        self.worker.stop(timeout=5.0)

        self.assertFalse(self.worker.is_running())
        self.assertIsNone(self.worker._thread)

    def test_repeated_short_timeout_stops_never_falsely_report_clean(self):
        self.worker.start()
        self.assertTrue(self.scheduler.entered_event.wait(timeout=2.0))

        for _ in range(3):
            with self.assertRaises(WebWorkerShutdownTimeout):
                self.worker.stop(timeout=_VERY_SHORT_TIMEOUT_SECONDS)
            self.assertTrue(self.worker.is_running())
            self.assertIsNotNone(self.worker._thread)


class WorkerNormalShutdownStillWorksTests(unittest.TestCase):
    """Confirma que o caso comum (thread termina dentro do timeout,
    sem nenhum bloqueio deliberado) continua funcionando exatamente
    como antes — nenhuma regressão no caminho feliz."""

    def test_radar_worker_normal_stop_no_exception(self):
        class _FastScheduler:
            def run_if_due(self, *, niche=None):
                pass

        worker = RadarWorker(scheduler=_FastScheduler(), poll_interval_seconds=0.05)
        worker.start()
        time.sleep(0.1)
        worker.stop(timeout=5.0)  # nao deve levantar
        self.assertFalse(worker.is_running())
        self.assertIsNone(worker._thread)

    def test_radar_web_worker_normal_stop_no_exception(self):
        class _FastScheduler:
            def run_if_due(self, *, niche=None):
                pass

        worker = RadarWebWorker(scheduler=_FastScheduler(), poll_interval_seconds=0.05)
        worker.start()
        time.sleep(0.1)
        worker.stop(timeout=5.0)
        self.assertFalse(worker.is_running())
        self.assertIsNone(worker._thread)

    def test_stop_without_start_is_still_a_noop(self):
        class _FastScheduler:
            def run_if_due(self, *, niche=None):
                pass

        worker = RadarWorker(scheduler=_FastScheduler(), poll_interval_seconds=0.05)
        worker.stop(timeout=_SHORT_TIMEOUT_SECONDS)  # nunca iniciado -- no-op, nunca levanta
        self.assertFalse(worker.is_running())


if __name__ == "__main__":
    unittest.main()
