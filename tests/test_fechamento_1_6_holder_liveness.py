"""
FECHAMENTO 1/6 — prova de liveness do detentor (P1/P1b) + invariante single-host.

1. Windows x POSIX em `_holder_process_alive`:
   Defeito reproduzido no baseline: no Windows, `OpenProcess` NEGADO
   (ERROR_ACCESS_DENIED — o processo EXISTE) devolvia MORTO; um detentor
   vivo podia ter o lease convertido para UNKNOWN_OUTCOME (P1) ou o gate
   reclamado (P1b). No POSIX o mesmo caso (`EPERM`) sempre deu VIVO.
   As decisões do ramo Windows dependem só dos códigos devolvidos pelo
   kernel32, então são verificadas aqui com um kernel32 simulado (roda em
   qualquer SO); a versão com SO real roda quando o SO correspondente
   está disponível.

2. Invariante single-host: o lease durável vive num SQLite em WAL, que
   exige memória compartilhada entre processos (mesmo host). Os testes
   garantem as premissas das quais `holder_pid` (PID local) depende.
"""
import ctypes
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

_ERROR_INVALID_PARAMETER = 87


def _direct_sqlite_connect_calls(source: str) -> list:
    """Linhas com chamada REAL a `connect` do módulo `sqlite3`."""
    import ast

    tree = ast.parse(source)
    module_names = set()
    function_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3":
                    module_names.add(alias.asname or "sqlite3")
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            for alias in node.names:
                if alias.name == "connect":
                    function_names.add(alias.asname or "connect")
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "connect" \
                and isinstance(func.value, ast.Name) and func.value.id in module_names:
            found.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in function_names:
            found.append(node.lineno)
    return found


class _FakeKernel32:
    """kernel32 simulado: o comportamento é definido pelos códigos."""

    def __init__(self, *, handle=0, last_error=0, exit_code=259, exit_ok=True):
        self.handle = handle
        self.last_error = last_error
        self.exit_code = exit_code
        self.exit_ok = exit_ok
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        return self.handle

    def GetExitCodeProcess(self, handle, pointer):
        if not self.exit_ok:
            return 0
        pointer._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def get_last_error(self):
        return self.last_error


def _alive_with(fake):
    return ge._win_process_alive(4242, fake, fake.get_last_error)


class WindowsDecisionTableTests(unittest.TestCase):
    def test_access_denied_is_alive(self):
        self.assertTrue(_alive_with(_FakeKernel32(handle=0, last_error=ge._ERROR_ACCESS_DENIED)))

    def test_nonexistent_pid_is_dead(self):
        self.assertFalse(_alive_with(_FakeKernel32(handle=0, last_error=_ERROR_INVALID_PARAMETER)))

    def test_still_active_is_alive_and_handle_closed(self):
        fake = _FakeKernel32(handle=77, exit_code=ge._STILL_ACTIVE)
        self.assertTrue(_alive_with(fake))
        self.assertEqual(fake.closed, [77])

    def test_exited_process_is_dead_and_handle_closed(self):
        fake = _FakeKernel32(handle=78, exit_code=0)
        self.assertFalse(_alive_with(fake))
        self.assertEqual(fake.closed, [78])

    def test_exit_code_query_failure_with_valid_handle_is_alive(self):
        fake = _FakeKernel32(handle=79, exit_ok=False)
        self.assertTrue(_alive_with(fake))
        self.assertEqual(fake.closed, [79])

    def test_dispatch_uses_windows_branch_and_denied_means_alive(self):
        fake = _FakeKernel32(handle=0, last_error=ge._ERROR_ACCESS_DENIED)
        with mock.patch.object(ge, "_IS_WINDOWS", True), \
                mock.patch.object(ge, "_win_kernel32", return_value=(fake, fake.get_last_error)):
            self.assertTrue(GraphExecutor._holder_process_alive(4242))


class PosixPermissionDeniedRealTests(unittest.TestCase):
    """EPERM REAL: um processo sem privilégio consulta um processo de
    outro usuário (o pai, root) — a negação prova existência => VIVO."""

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                         "requer POSIX executando como root para rebaixar privilégio no filho")
    def test_real_eperm_is_alive(self):
        code = (
            "import os,sys; sys.path.insert(0, sys.argv[1]);"
            "target=int(sys.argv[2]); os.setgid(65534); os.setuid(65534);"
            "from atlas.orchestration.graph_executor import GraphExecutor as G;"
            "\ntry:\n    os.kill(target,0); print('NO_EPERM')\n"
            "except PermissionError:\n    print('EPERM', G._holder_process_alive(target))\n"
        )
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-c", code, root, str(os.getpid())],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "EPERM True", "consulta negada deveria provar processo VIVO")


class WindowsRealOsTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requer Windows real")
    def test_real_windows_own_pid_alive_and_missing_pid_dead(self):
        self.assertTrue(GraphExecutor._holder_process_alive(os.getpid()))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=30)
        self.assertFalse(GraphExecutor._holder_process_alive(proc.pid))


class _DurableLeaseBase(unittest.TestCase):
    TTL = 0.2

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.executor = GraphExecutor(
            mission_tracker=MissionTracker(db_path=self.db_path), job_queue=JobQueue(db_path=self.db_path),
            db_path=self.db_path, gate_in_progress_ttl_seconds=self.TTL,
        )

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _insert_expired(self, status, holder_pid):
        old = time.time() - 60.0
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO graph_executor_task_leases (mission_id, task_id, lease_token, status, leased_at, "
                "generation, holder_pid, holder_seen_at, outcome_json) VALUES ('m', 't', 'tok', ?, ?, 1, ?, ?, ?)",
                (status, old, holder_pid, old, '{"ok": true}' if status == "GATE_IN_PROGRESS" else None),
            )
            conn.commit()
        finally:
            conn.close()

    def _row(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT status, lease_token, holder_pid FROM graph_executor_task_leases").fetchone()
        finally:
            conn.close()


class AccessDeniedHolderNeverConvertedTests(_DurableLeaseBase):
    """P1/P1b ponta a ponta com o ramo Windows e consulta NEGADA."""

    def _denied(self):
        fake = _FakeKernel32(handle=0, last_error=ge._ERROR_ACCESS_DENIED)
        return mock.patch.object(ge, "_IS_WINDOWS", True), \
            mock.patch.object(ge, "_win_kernel32", return_value=(fake, fake.get_last_error))

    def test_p1_in_progress_with_denied_holder_stays_in_progress(self):
        self._insert_expired("IN_PROGRESS", holder_pid=4242)
        a, b = self._denied()
        with a, b:
            status = self.executor._acquire_lease("m", "t")[0]
        self.assertEqual(status, "IN_PROGRESS")
        self.assertEqual(self._row()["status"], "IN_PROGRESS")

    def test_p1b_gate_with_denied_holder_is_never_reclaimed(self):
        self._insert_expired("GATE_IN_PROGRESS", holder_pid=4242)
        a, b = self._denied()
        with a, b:
            status = self.executor._acquire_lease("m", "t")[0]
        self.assertEqual(status, "GATE_IN_PROGRESS")
        row = self._row()
        self.assertEqual((row["status"], row["lease_token"], row["holder_pid"]), ("GATE_IN_PROGRESS", "tok", 4242))

    def test_crash_recovery_preserved_when_holder_truly_gone(self):
        # sensibilidade no sentido oposto: PID inexistente continua MORTO
        self._insert_expired("IN_PROGRESS", holder_pid=4242)
        fake = _FakeKernel32(handle=0, last_error=_ERROR_INVALID_PARAMETER)
        with mock.patch.object(ge, "_IS_WINDOWS", True), \
                mock.patch.object(ge, "_win_kernel32", return_value=(fake, fake.get_last_error)):
            status = self.executor._acquire_lease("m", "t")[0]
        self.assertEqual(status, "UNKNOWN_OUTCOME")


class SingleHostInvariantTests(_DurableLeaseBase):
    """Premissas das quais `holder_pid` (PID local) depende."""

    def test_lease_connections_are_wal(self):
        conn = db_module.get_connection(self.db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        finally:
            conn.close()
        with db_module.connect(self.db_path) as conn2:
            self.assertEqual(conn2.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_graph_executor_never_opens_sqlite_outside_db_module(self):
        # toda conexão da camada de lease passa por `core.models.db`
        # (WAL); uma conexão direta furaria a premissa single-host.
        # Verificação por AST de CHAMADAS reais (não por texto): pega a
        # chamada direta pelo módulo, o módulo importado com alias e a
        # função importada com alias; ignora menções em comentários e
        # strings. (A guarda estática de `test_correction_final_resource_
        # cleanup` conta o padrão por TEXTO — por isso este arquivo não o
        # escreve literalmente em lugar algum.)
        with open(ge.__file__, encoding="utf-8") as f:
            self.assertEqual(_direct_sqlite_connect_calls(f.read()), [])

    def test_holder_identity_is_local_pid_of_acquirer(self):
        status = self.executor._acquire_lease("m2", "t2")[0]
        self.assertEqual(status, "ACQUIRED")
        conn = sqlite3.connect(self.db_path)
        try:
            pid = conn.execute("SELECT holder_pid FROM graph_executor_task_leases WHERE mission_id='m2'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(pid, os.getpid())

    def test_class_documents_single_host_invariant(self):
        self.assertIn("SINGLE-HOST", GraphExecutor.__doc__)


if __name__ == "__main__":
    unittest.main()
