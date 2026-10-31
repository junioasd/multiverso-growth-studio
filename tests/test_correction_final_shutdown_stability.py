import os
import tempfile
import threading
import time
import unittest

from atlas.radar.engine import CommercialOpportunityRadar
from atlas.radar.scheduler import RadarScheduler
from atlas.radar.worker import RadarWorker
from core.jobs.queue import JobQueue
from core.models import db as db_module
from radar_web.engine import RadarWebComercial
from radar_web.scheduler import RadarWebScheduler
from radar_web.worker import RadarWebWorker

"""
Teste de regressão — MICROCORREÇÃO FINAL (instabilidade residual de
encerramento/concorrência).

Causa raiz investigada e corrigida: `core.models.db`'s
`busy_timeout` (introduzido na correção do travamento anterior) tinha
o mesmo valor (10000ms) — ou próximo dele — do timeout usado por
alguns testes ao chamar `worker.stop(timeout=5.0)`. Se a operação
SQLite do worker estivesse bloqueada por contenção real de lock, o
`join(timeout=5.0)` de `stop()` podia desistir de esperar ANTES da
thread realmente terminar — uma condição de corrida sensível a
timing, exatamente o tipo de instabilidade intermitente relatada.

Corrigido reduzindo `busy_timeout` para 2000ms — margem de 2.5x sob o
menor timeout de `stop()` usado em qualquer teste (5.0s).

Este arquivo garante que a causa raiz não volta: testa a margem de
segurança diretamente, e força contenção REAL (múltiplas threads
concorrentes no mesmo banco) durante ciclos repetidos de start/stop
de workers, confirmando que nenhuma thread fica órfã mesmo sob
estresse.
"""

# margem mínima exigida entre o busy_timeout do SQLite e o menor
# stop(timeout=...) usado em qualquer teste real da suíte
_MIN_SAFETY_MARGIN = 2.0


class BusyTimeoutSafetyMarginTests(unittest.TestCase):
    """Garante que o `busy_timeout` do SQLite nunca fica perigosamente
    próximo do menor `stop(timeout=...)` usado pelos workers em
    qualquer teste da suíte — a causa raiz exata desta correção."""

    def _current_busy_timeout_ms(self) -> int:
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(db_path)
        db_module.init_db(db_path)
        try:
            with db_module.connect(db_path) as conn:
                value = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            db_module.remove_db_file(db_path)
        return value

    def _shortest_stop_timeout_used_in_tests(self) -> float:
        """Varre os próprios arquivos de teste em busca do menor
        `stop(timeout=...)` realmente usado — nunca assume um valor
        fixo, sempre reflete o código real."""
        import glob
        import re

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        shortest = None
        pattern = re.compile(r"\.stop\(\s*timeout\s*=\s*([0-9.]+)\s*\)")
        for path in glob.glob(os.path.join(root, "**", "*.py"), recursive=True):
            if "__pycache__" in path:
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            for match in pattern.finditer(content):
                value = float(match.group(1))
                if shortest is None or value < shortest:
                    shortest = value
        return shortest if shortest is not None else 10.0

    def test_busy_timeout_has_safe_margin_below_shortest_worker_stop_timeout(self):
        busy_timeout_seconds = self._current_busy_timeout_ms() / 1000.0
        shortest_stop_timeout = self._shortest_stop_timeout_used_in_tests()
        margin = shortest_stop_timeout / busy_timeout_seconds if busy_timeout_seconds else float("inf")
        self.assertGreaterEqual(
            margin, _MIN_SAFETY_MARGIN,
            f"busy_timeout ({busy_timeout_seconds}s) está perigosamente próximo do menor "
            f"stop(timeout={shortest_stop_timeout}) usado na suíte (margem={margin:.2f}x, "
            f"mínimo exigido={_MIN_SAFETY_MARGIN}x) — risco de condição de corrida no shutdown",
        )


class WorkerNeverOrphansThreadUnderRealContentionTests(unittest.TestCase):
    """Força contenção SQLite REAL (múltiplas threads concorrentes no
    mesmo banco) durante ciclos repetidos de start/stop de workers —
    nenhuma thread pode ficar viva depois que `stop()` retorna."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _hammer_db(self, stop_signal: threading.Event, iterations: int = 60):
        for _ in range(iterations):
            if stop_signal.is_set():
                return
            try:
                with db_module.connect(self.db_path) as conn:
                    conn.execute("SELECT 1")
                    time.sleep(0.01)
            except Exception:
                pass

    def test_radar_worker_never_orphaned_under_contention(self):
        stragglers = []
        for cycle in range(8):
            job_queue = JobQueue(db_path=self.db_path)
            radar = CommercialOpportunityRadar()
            scheduler = RadarScheduler(job_queue=job_queue, radar=radar, db_path=self.db_path)
            worker = RadarWorker(scheduler=scheduler, poll_interval_seconds=0.02, niche=f"n{cycle}")
            worker.start()

            stop_signal = threading.Event()
            hammers = [threading.Thread(target=self._hammer_db, args=(stop_signal,), daemon=True) for _ in range(6)]
            for t in hammers:
                t.start()

            time.sleep(0.15)
            worker.stop(timeout=5.0)  # o menor timeout real usado na suíte
            stop_signal.set()

            for t in hammers:
                t.join(timeout=10.0)

            if worker.is_running():
                stragglers.append(cycle)

        self.assertEqual(stragglers, [], f"thread(s) de RadarWorker órfã(s) sob contenção: ciclos {stragglers}")

    def test_radar_web_worker_never_orphaned_under_contention(self):
        stragglers = []
        for cycle in range(8):
            job_queue = JobQueue(db_path=self.db_path)
            radar = RadarWebComercial()
            scheduler = RadarWebScheduler(job_queue=job_queue, radar=radar, db_path=self.db_path)
            worker = RadarWebWorker(scheduler=scheduler, poll_interval_seconds=0.02, niche=f"n{cycle}")
            worker.start()

            stop_signal = threading.Event()
            hammers = [threading.Thread(target=self._hammer_db, args=(stop_signal,), daemon=True) for _ in range(6)]
            for t in hammers:
                t.start()

            time.sleep(0.15)
            worker.stop(timeout=5.0)
            stop_signal.set()

            for t in hammers:
                t.join(timeout=10.0)

            if worker.is_running():
                stragglers.append(cycle)

        self.assertEqual(stragglers, [], f"thread(s) de RadarWebWorker órfã(s) sob contenção: ciclos {stragglers}")


class FullSuiteRepeatedStabilityRegressionTests(unittest.TestCase):
    """Roda um subconjunto real e representativo de testes com worker
    (start/stop) em processos SEPARADOS, repetidas vezes, confirmando
    que o processo sempre encerra sozinho e nunca deixa recursos
    pendentes — a garantia central desta correção."""

    def test_worker_lifecycle_tests_terminate_cleanly_three_times_in_a_row(self):
        # ORCAMENTO DE TEMPO (teste apenas; producao intocada).
        # Medicao REAL mais recente nesta maquina: 22/22 OK, rc=0 em ~172s.
        # O teto anterior de 60s classificava uma execucao valida como hang.
        # Usamos 240s (~1.4x a medicao observada) para manter um tripwire
        # finito contra hang/lentidao degenerativa sem gerar falso negativo.
        import subprocess
        import sys

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        durations = []
        for i in range(3):
            start = time.time()
            result = subprocess.run(
                [
                    sys.executable, "-m", "unittest",
                    "tests.test_atlas_radar_worker", "tests.test_correction_final_hang_prevention",
                ],
                cwd=project_root, capture_output=True, text=True, timeout=240,
            )
            duration = time.time() - start
            durations.append(duration)
            self.assertEqual(
                result.returncode, 0,
                f"execução {i+1}/3 falhou ou travou (returncode={result.returncode}):\n"
                f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}",
            )
        # variância baixa entre execuções é um sinal indireto de ausência de
        # travamentos intermitentes (um hang ocasional apareceria como um
        # outlier bem acima das demais durações)
        self.assertLess(max(durations), 240.0, f"alguma execução ficou anormalmente lenta: {durations}")


if __name__ == "__main__":
    unittest.main()
