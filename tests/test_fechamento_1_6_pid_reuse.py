"""
FECHAMENTO 1/6 — defesa contra PID reutilizado (identidade temporal do detentor).

Defeito (baseline): o detentor do lease era identificado só pelo PID. Após
crash/restart, se o SO entregasse o MESMO PID a outro processo (em
contêiner, o app reiniciado recebe tipicamente o mesmo PID), a prova de
liveness aceitava o processo alheio como o detentor anterior e o lease
ficava preso em IN_PROGRESS/GATE_IN_PROGRESS indefinidamente.

Correção: coluna aditiva `holder_proc_start` com a identidade temporal do
processo detentor (instante de criação no relógio do SO), comparada por
igualdade exata. Provas:
  1. PID reutilizado NÃO é aceito como o detentor anterior (reutilização
     REAL de PID no Linux, forçada ciclando o espaço de PIDs);
  2. processo realmente vivo continua reconhecido (P1/P1b preservadas);
  3. restart/crash não prende o lease (inclusive o próprio app reiniciado
     com o mesmo PID);
  4. Windows (kernel32 simulado + teste real quando em Windows) e POSIX;
  5. compatibilidade com bancos e linhas existentes.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from atlas.orchestration import graph_executor as ge
from atlas.orchestration.graph_executor import GraphExecutor
from core.coordination.mission import MissionTracker
from core.jobs.queue import JobQueue
from core.models import db as db_module

_HELPERS = os.path.join(os.path.dirname(__file__), "_helpers")
_CRASH_DURING_RUNNER = os.path.join(_HELPERS, "crash_during_runner_worker.py")
_OCCUPIER = os.path.join(_HELPERS, "pid_reuse_occupier.py")
_IS_LINUX_PROC = sys.platform.startswith("linux") and os.path.exists("/proc/self/stat")


def _pid_max() -> int:
    try:
        with open("/proc/sys/kernel/pid_max", encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


_IDENTITY_SUPPORTED = _IS_LINUX_PROC or os.name == "nt"
_LINUX_RESERVED_PIDS = 300  # kernel Linux: após o wrap, a alocação recomeça em 300


def _require_identity(testcase, identity):
    """Plataforma COM suporte (Linux com /proc, Windows): identidade
    ausente é FALHA, nunca skip — um skip aqui escondeu uma regressão
    real (cache negativo). Só plataformas sem suporte pulam."""
    if identity is None:
        if not _IDENTITY_SUPPORTED:
            testcase.skipTest("plataforma sem identidade temporal (POSIX sem /proc)")
        testcase.fail("identidade temporal ausente numa plataforma que a suporta")
    return identity


def _spawn_sleeper():
    return subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)


def _stop(proc):
    proc.stdin.close()
    proc.wait(timeout=30)


class _LeaseDbBase(unittest.TestCase):
    TTL = 0.3

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _executor(self):
        return GraphExecutor(
            mission_tracker=MissionTracker(db_path=self.db_path), job_queue=JobQueue(db_path=self.db_path),
            db_path=self.db_path, gate_in_progress_ttl_seconds=self.TTL,
        )

    def _insert_expired(self, status, *, holder_pid, proc_start, mission="m"):
        old = time.time() - 60.0
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO graph_executor_task_leases (mission_id, task_id, lease_token, status, leased_at, "
                "generation, holder_pid, holder_seen_at, holder_proc_start, outcome_json) "
                "VALUES (?, 't', 'tok', ?, ?, 1, ?, ?, ?, ?)",
                (mission, status, old, holder_pid, old, proc_start,
                 '{"ok": true}' if status == "GATE_IN_PROGRESS" else None),
            )
            conn.commit()
        finally:
            conn.close()

    def _row(self, mission="m"):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT status, lease_token, holder_pid, holder_proc_start, holder_seen_at, leased_at "
                "FROM graph_executor_task_leases WHERE mission_id = ?", (mission,),
            ).fetchone()
        finally:
            conn.close()


# ---------------------------------------------------------------- identidade
class IdentityReadingTests(unittest.TestCase):
    @unittest.skipUnless(_IS_LINUX_PROC, "requer Linux com /proc")
    def test_linux_identity_is_stable_and_distinguishes_processes(self):
        me = ge._process_start_identity(os.getpid())
        self.assertRegex(me, r"^linux:[0-9a-f-]+:\d+$")
        self.assertEqual(me, ge._process_start_identity(os.getpid()), "identidade deve ser estável")
        proc = _spawn_sleeper()
        try:
            other = ge._process_start_identity(proc.pid)
            self.assertIsNotNone(other)
            self.assertNotEqual(other, me)
        finally:
            _stop(proc)
        self.assertIsNone(ge._process_start_identity(proc.pid), "processo encerrado não tem identidade")

    def test_linux_parser_handles_hostile_comm_and_missing_data(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "sys", "kernel", "random"))
            os.makedirs(os.path.join(root, "77"))
            # campos após o comm: estado + 18 campos, o 20º é o starttime
            rest = "S " + " ".join(str(i) for i in range(2, 20)) + " 987654 1 2 3"
            with open(os.path.join(root, "77", "stat"), "w", encoding="ascii") as f:
                f.write("77 (x) (y) z) " + rest)
            self.assertIsNone(ge._linux_process_start_identity(77, root), "sem boot_id => sem identidade")
            with open(os.path.join(root, "sys", "kernel", "random", "boot_id"), "w", encoding="ascii") as f:
                f.write("abc-123\n")
            self.assertEqual(ge._linux_process_start_identity(77, root), "linux:abc-123:987654")
            self.assertIsNone(ge._linux_process_start_identity(78, root), "PID inexistente => None")
            with open(os.path.join(root, "77", "stat"), "w", encoding="ascii") as f:
                f.write("77 (x) S 1 2")
            self.assertIsNone(ge._linux_process_start_identity(77, root), "stat truncado => None")

    def test_transient_failure_is_not_cached_as_missing_identity(self):
        """Regressão (fechamento 1/6): uma falha transitória na PRIMEIRA
        leitura da própria identidade era guardada em cache como None e
        desligava a defesa contra PID reutilizado pelo resto do processo."""
        with mock.patch.dict(ge._self_identity_cache, clear=True):
            with mock.patch.object(ge, "_process_start_identity", return_value=None):
                self.assertIsNone(ge._current_process_start_identity())
            with mock.patch.object(ge, "_process_start_identity", return_value="linux:b:42"):
                self.assertEqual(ge._current_process_start_identity(), "linux:b:42",
                                 "falha transitória não pode desligar a identidade para sempre")
            with mock.patch.object(ge, "_process_start_identity", return_value="linux:b:99"):
                self.assertEqual(ge._current_process_start_identity(), "linux:b:42",
                                 "identidade obtida com sucesso é estável (cache)")

    def test_posix_without_proc_falls_back_to_pid_only(self):
        with tempfile.TemporaryDirectory() as empty_root, \
                mock.patch.object(ge, "_IS_WINDOWS", False), mock.patch.object(ge, "_PROC_ROOT", empty_root):
            self.assertIsNone(ge._process_start_identity(os.getpid()))
            # identidade registrada existe, a atual é ilegível => sem prova de troca => VIVO
            self.assertTrue(GraphExecutor._holder_identity_alive(os.getpid(), "linux:qualquer:1"))


class DecisionTableTests(unittest.TestCase):
    def _decide(self, *, pid_alive, current, recorded):
        with mock.patch.object(GraphExecutor, "_holder_process_alive", staticmethod(lambda pid: pid_alive)), \
                mock.patch.object(ge, "_process_start_identity", lambda pid: current):
            return GraphExecutor._holder_identity_alive(4242, recorded)

    def test_dead_pid_is_dead(self):
        self.assertFalse(self._decide(pid_alive=False, current="linux:b:1", recorded="linux:b:1"))

    def test_legacy_row_without_identity_uses_pid_only(self):
        self.assertTrue(self._decide(pid_alive=True, current="linux:b:9", recorded=None))

    def test_unreadable_current_identity_is_alive(self):
        self.assertTrue(self._decide(pid_alive=True, current=None, recorded="linux:b:1"))

    def test_same_identity_is_alive(self):
        self.assertTrue(self._decide(pid_alive=True, current="linux:b:1", recorded="linux:b:1"))

    def test_reused_pid_is_not_the_recorded_holder(self):
        self.assertFalse(self._decide(pid_alive=True, current="linux:b:2", recorded="linux:b:1"))

    def test_reused_pid_after_machine_reboot_is_not_the_recorded_holder(self):
        self.assertFalse(self._decide(pid_alive=True, current="linux:boot2:1", recorded="linux:boot1:1"))


# ------------------------------------------------------------------ Windows
class _FakeKernel32Times:
    def __init__(self, *, handle=55, last_error=0, creation=0x01D9ABCD12345678, times_ok=True, exit_code=259):
        self.handle, self.last_error, self.creation = handle, last_error, creation
        self.times_ok, self.exit_code, self.closed = times_ok, exit_code, []

    def OpenProcess(self, access, inherit, pid):
        return self.handle

    def GetProcessTimes(self, handle, creation, exited, kernel, user):
        if not self.times_ok:
            return 0
        creation._obj.dwLowDateTime = self.creation & 0xFFFFFFFF
        creation._obj.dwHighDateTime = self.creation >> 32
        return 1

    def GetExitCodeProcess(self, handle, pointer):
        pointer._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def get_last_error(self):
        return self.last_error


class WindowsIdentityTests(unittest.TestCase):
    def test_creation_filetime_becomes_identity_and_handle_is_closed(self):
        fake = _FakeKernel32Times(creation=0x01D9ABCD12345678)
        self.assertEqual(ge._win_process_start_identity(4242, fake), "nt:%d" % 0x01D9ABCD12345678)
        self.assertEqual(fake.closed, [55])

    def test_unreadable_identity_is_none(self):
        self.assertIsNone(ge._win_process_start_identity(4242, _FakeKernel32Times(handle=0, last_error=5)))
        fake = _FakeKernel32Times(times_ok=False)
        self.assertIsNone(ge._win_process_start_identity(4242, fake))
        self.assertEqual(fake.closed, [55])
        self.assertIsNone(ge._win_process_start_identity(4242, _FakeKernel32Times(creation=0)))

    def _windows(self, fake):
        return mock.patch.object(ge, "_IS_WINDOWS", True), \
            mock.patch.object(ge, "_win_kernel32", return_value=(fake, fake.get_last_error))

    def test_windows_same_process_alive_and_reused_pid_dead(self):
        fake = _FakeKernel32Times(creation=1000)
        a, b = self._windows(fake)
        with a, b:
            self.assertTrue(GraphExecutor._holder_identity_alive(4242, "nt:1000"))
            self.assertFalse(GraphExecutor._holder_identity_alive(4242, "nt:999"), "PID reutilizado")

    def test_windows_access_denied_keeps_holder_alive(self):
        fake = _FakeKernel32Times(handle=0, last_error=ge._ERROR_ACCESS_DENIED)
        a, b = self._windows(fake)
        with a, b:
            self.assertTrue(GraphExecutor._holder_identity_alive(4242, "nt:1000"))

    @unittest.skipUnless(os.name == "nt", "requer Windows real")
    def test_real_windows_identity_stable_and_distinct(self):
        me = ge._process_start_identity(os.getpid())
        self.assertRegex(me, r"^nt:\d+$")
        self.assertEqual(me, ge._process_start_identity(os.getpid()))
        self.assertTrue(GraphExecutor._holder_identity_alive(os.getpid(), me))
        proc = _spawn_sleeper()
        try:
            other = ge._process_start_identity(proc.pid)
            self.assertNotEqual(other, me)
            self.assertTrue(GraphExecutor._holder_identity_alive(proc.pid, other))
            self.assertFalse(GraphExecutor._holder_identity_alive(proc.pid, me))
        finally:
            _stop(proc)


# ---------------------------------------------------------- ponta a ponta
class GenuinePidReuseTests(_LeaseDbBase):
    """Reutilização REAL de PID: o detentor morre por crash; o SO entrega
    o MESMO PID a outro processo; a nova instância não pode aceitá-lo como
    o detentor anterior."""

    TTL = 1.0

    @unittest.skipUnless(_IS_LINUX_PROC and hasattr(os, "fork") and 0 < _pid_max() <= 65536,
                         "requer Linux com /proc e pid_max <= 65536 (ciclo de PIDs em segundos)")
    def test_reused_pid_is_not_accepted_as_previous_holder(self):
        # o PID do detentor precisa ser reutilizável pelo ciclo: fora da
        # faixa reservada (< 300) do kernel Linux.
        adv = subprocess.run([sys.executable, _OCCUPIER, "--advance", str(_LINUX_RESERVED_PIDS), "60"], timeout=90)
        self.assertEqual(adv.returncode, 0, "não foi possível avançar a alocação de PIDs para >= 300")
        crash = subprocess.run([sys.executable, _CRASH_DURING_RUNNER, self.db_path, "m", str(self.TTL)], timeout=60)
        self.assertNotEqual(crash.returncode, 0)
        row = self._row()
        self.assertEqual(row["status"], "IN_PROGRESS")
        dead_pid, recorded = row["holder_pid"], row["holder_proc_start"]
        self.assertGreaterEqual(dead_pid, _LINUX_RESERVED_PIDS, "detentor em PID reservado — reutilização impossível")
        self.assertIsNotNone(recorded, "a aquisição deve gravar a identidade temporal do detentor")

        occupier = subprocess.Popen(
            [sys.executable, _OCCUPIER, str(dead_pid), "60"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True,
        )
        try:
            line = occupier.stdout.readline().strip()
            if line != "HELD %d" % dead_pid:
                self.skipTest("o SO não reutilizou o PID %s dentro do prazo (%r)" % (dead_pid, line))
            # PRÉ-CONDIÇÃO: a reutilização é REAL — só pelo PID, o processo
            # alheio pareceria ser o detentor (o defeito do baseline).
            self.assertTrue(GraphExecutor._holder_process_alive(dead_pid))
            self.assertNotEqual(ge._process_start_identity(dead_pid), recorded)

            time.sleep(self.TTL + 0.2)
            status = self._executor()._acquire_lease("m", "t1")[0]
            self.assertEqual(status, "UNKNOWN_OUTCOME", "PID reutilizado foi aceito como o detentor anterior")
            self.assertEqual(self._row()["status"], "UNKNOWN_OUTCOME")
        finally:
            occupier.stdin.close()
            occupier.wait(timeout=30)
            occupier.stdout.close()


class LiveHolderStillRecognizedTests(_LeaseDbBase):
    """Processo REALMENTE vivo, com sua identidade real registrada e
    heartbeat parado (sinal de vida velho): nunca convertido/reclamado."""

    def test_p1_live_process_with_matching_identity_stays_in_progress(self):
        proc = _spawn_sleeper()
        try:
            identity = _require_identity(self, ge._process_start_identity(proc.pid))
            self._insert_expired("IN_PROGRESS", holder_pid=proc.pid, proc_start=identity)
            self.assertEqual(self._executor()._acquire_lease("m", "t")[0], "IN_PROGRESS")
            self.assertEqual(self._row()["status"], "IN_PROGRESS")
        finally:
            _stop(proc)

    def test_p1b_live_process_with_matching_identity_gate_never_reclaimed(self):
        proc = _spawn_sleeper()
        try:
            identity = _require_identity(self, ge._process_start_identity(proc.pid))
            self._insert_expired("GATE_IN_PROGRESS", holder_pid=proc.pid, proc_start=identity)
            self.assertEqual(self._executor()._acquire_lease("m", "t")[0], "GATE_IN_PROGRESS")
            row = self._row()
            self.assertEqual((row["lease_token"], row["holder_pid"]), ("tok", proc.pid))
        finally:
            _stop(proc)

    def test_live_acquirer_in_same_process_is_recognized(self):
        # dois executores no MESMO processo: o segundo vê o primeiro como vivo
        first = self._executor()
        status, token, _, _ = first._acquire_lease("m3", "t")
        self.assertEqual(status, "ACQUIRED")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE graph_executor_task_leases SET leased_at = ?, holder_seen_at = ? "
                         "WHERE mission_id = 'm3'", (time.time() - 60, time.time() - 60))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._executor()._acquire_lease("m3", "t")[0], "IN_PROGRESS")


class RestartWithSamePidTests(_LeaseDbBase):
    """Cenário de contêiner: o app reinicia com o MESMO PID do detentor
    morto (a encarnação anterior). O lease não pode ficar preso."""

    def _previous_incarnation(self):
        me = _require_identity(self, ge._current_process_start_identity())
        kind, *parts = me.split(":")
        return "%s:%s" % (kind, ":".join(parts[:-1] + [str(int(parts[-1]) - 1)]))

    def test_p1_restart_same_pid_converts_to_unknown_outcome(self):
        self._insert_expired("IN_PROGRESS", holder_pid=os.getpid(), proc_start=self._previous_incarnation())
        self.assertEqual(self._executor()._acquire_lease("m", "t")[0], "UNKNOWN_OUTCOME")

    def test_p1b_restart_same_pid_reclaims_gate_and_records_new_identity(self):
        self._insert_expired("GATE_IN_PROGRESS", holder_pid=os.getpid(), proc_start=self._previous_incarnation())
        status, token, outcome, _ = self._executor()._acquire_lease("m", "t")
        self.assertEqual(status, "OUTCOME_AVAILABLE_PENDING_GATE")
        self.assertEqual(outcome, {"ok": True})
        row = self._row()
        self.assertEqual((row["status"], row["lease_token"]), ("GATE_IN_PROGRESS", token))
        self.assertEqual(row["holder_proc_start"], ge._current_process_start_identity())


# ----------------------------------------------------------- compatibilidade
class CompatibilityTests(_LeaseDbBase):
    _LEGACY_DDL = (
        "CREATE TABLE graph_executor_task_leases (mission_id TEXT NOT NULL, task_id TEXT NOT NULL, "
        "lease_token TEXT NOT NULL, status TEXT NOT NULL, leased_at REAL NOT NULL, resolved_at REAL, "
        "outcome_json TEXT, error TEXT, generation INTEGER NOT NULL DEFAULT 0, holder_pid INTEGER, "
        "holder_seen_at REAL, PRIMARY KEY (mission_id, task_id))"
    )

    def test_existing_database_is_migrated_additively_and_rows_preserved(self):
        db_module.remove_db_file(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(self._LEGACY_DDL)
            conn.execute("INSERT INTO graph_executor_task_leases (mission_id, task_id, lease_token, status, "
                         "leased_at, holder_pid, holder_seen_at) VALUES ('old', 't', 'tk', 'COMPLETED', 1.0, 7, 1.0)")
            conn.commit()
        finally:
            conn.close()
        db_module.init_db(self.db_path)
        db_module.init_db(self.db_path)  # idempotente
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(graph_executor_task_leases)")}
            row = conn.execute("SELECT * FROM graph_executor_task_leases WHERE mission_id='old'").fetchone()
        finally:
            conn.close()
        self.assertIn("holder_proc_start", cols)
        self.assertEqual((row["status"], row["lease_token"], row["holder_pid"]), ("COMPLETED", "tk", 7))
        self.assertIsNone(row["holder_proc_start"])

    def test_legacy_row_with_live_pid_keeps_previous_behavior(self):
        self._insert_expired("IN_PROGRESS", holder_pid=os.getpid(), proc_start=None)
        self.assertEqual(self._executor()._acquire_lease("m", "t")[0], "IN_PROGRESS")

    def test_legacy_row_with_dead_pid_still_recovers(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=30)
        self._insert_expired("IN_PROGRESS", holder_pid=proc.pid, proc_start=None)
        self.assertEqual(self._executor()._acquire_lease("m", "t")[0], "UNKNOWN_OUTCOME")

    def test_new_acquisition_records_holder_identity(self):
        self.assertEqual(self._executor()._acquire_lease("m4", "t")[0], "ACQUIRED")
        row = self._row("m4")
        self.assertEqual(row["holder_pid"], os.getpid())
        self.assertEqual(row["holder_proc_start"], ge._current_process_start_identity())


if __name__ == "__main__":
    unittest.main()
