import os
import tempfile
import threading
import time
import unittest

from core.evidence.authority_registry import AuthorityRegistry, SCOPE_REGISTER_VERIFIER
from core.evidence.owner_principal import issue_owner_principal
from core.evidence.verifier_registry import TrustLevel, VerifierRegistration, VerifierRegistry
from core.capabilities.registry import CapabilityHealth
from core.models import db as db_module
from core.security.auth import create_canonical_auth_service, hash_password

"""
Teste de regressão — MICROCORREÇÃO FINAL (travamento reproduzível da
regressão monolítica).

Causa raiz identificada e corrigida: `core.models.db.get_connection()`/
`get_autocommit_connection()` usavam o modo padrão ("rollback
journal") do SQLite, sem `busy_timeout` explícito — escrita exige lock
EXCLUSIVO e bloqueia todos os leitores, e sob contenção (múltiplas
conexões/threads tocando o mesmo arquivo, incluindo as novas rotinas
de persistência de AuthorityRegistry/VerifierRegistry/SourceRegistry)
o tempo de espera podia se somar de forma perceptível como
travamento. Corrigido com modo WAL (leitores concorrentes com um
único escritor) + `busy_timeout` explícito e limitado (nunca espera
indefinidamente).

Efeito colateral encontrado e corrigido: modo WAL cria arquivos
auxiliares `-wal`/`-shm` — `os.remove(db_path)` sozinho os deixava
órfãos em `/tmp`. Corrigido com `db_module.remove_db_file()`, usado em
todos os testes que antes chamavam `os.remove()` diretamente.

Este arquivo garante que a causa raiz não volta: acesso concorrente
real (múltiplas threads, mesma conexão de banco) sempre termina em
tempo limitado, nunca trava; e nenhum arquivo/recurso fica órfão.
"""


class ConcurrentSQLiteAccessNeverHangsTests(unittest.TestCase):
    """Estresse real de concorrência no MESMO arquivo SQLite — a causa
    raiz do travamento relatado."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_many_concurrent_writers_same_db_never_hang(self):
        """N threads escrevendo simultaneamente no MESMO banco —
        precisa terminar em tempo limitado (nunca travar), mesmo sob
        contenção real de lock."""
        errors = []
        results = []

        def writer(worker_id):
            try:
                with db_module.connect(self.db_path) as conn:
                    for i in range(20):
                        conn.execute(
                            "INSERT INTO audit_events (event_id, timestamp, actor, action, resource, outcome, details) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (f"ev-{worker_id}-{i}", time.time(), f"worker-{worker_id}", "TEST_WRITE", "x", "OK", "{}"),
                        )
                results.append(worker_id)
            except Exception as exc:  # nunca deixa uma falha silenciar as outras -- registra e segue
                errors.append((worker_id, exc))

        threads = [threading.Thread(target=writer, args=(i,), daemon=True) for i in range(12)]
        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)  # NUNCA espera indefinidamente
        duration = time.time() - start

        still_alive = [t for t in threads if t.is_alive()]
        self.assertEqual(len(still_alive), 0, "threads não terminaram dentro do timeout -- possível travamento")
        self.assertLess(duration, 30, "concorrência real levou tempo demais -- possível contenção não tratada")
        # sob WAL + busy_timeout, o esperado é que TODOS terminem com sucesso
        # (contenção real é absorvida pelo busy_timeout, não causa falha nem trava)
        self.assertEqual(len(errors), 0, f"erros inesperados sob concorrência: {errors}")
        self.assertEqual(len(results), 12)

    def test_authority_registry_concurrent_grant_issuance_never_hangs(self):
        """Reproduz o padrão exato introduzido na rodada anterior:
        múltiplas threads emitindo/persistindo grants no MESMO
        AuthorityRegistry (mesmo db_path) simultaneamente."""
        auth = create_canonical_auth_service(
            owner_email="owner@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
        )
        token = auth.login("owner@x.com", "SenhaForte123")
        principal = issue_owner_principal(auth_service=auth, session_token=token)
        ar = AuthorityRegistry(db_path=self.db_path)
        root = ar.bootstrap_root_authority(owner_principal=principal, auth_service=auth, bootstrap_reason="setup")

        errors = []

        def issue_and_revoke(worker_id):
            try:
                grant = ar.issue_grant(
                    issuer_grant_id=root.grant_id, subject_actor_id=f"worker-{worker_id}", authority_level="OWNER",
                    scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="teste concorrente",
                )
                ar.revoke_grant(grant.grant_id, revoked_by="owner@x.com", reason="limpeza de teste")
            except Exception as exc:
                errors.append((worker_id, exc))

        threads = [threading.Thread(target=issue_and_revoke, args=(i,), daemon=True) for i in range(8)]
        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        duration = time.time() - start

        self.assertEqual(len([t for t in threads if t.is_alive()]), 0, "thread presa -- travamento")
        self.assertLess(duration, 30)
        self.assertEqual(len(errors), 0, f"erros inesperados: {errors}")

    def test_verifier_registration_concurrent_persistence_never_hangs(self):
        """Mesmo padrão para VerifierRegistry.register() persistindo
        concorrentemente."""
        auth = create_canonical_auth_service(
            owner_email="owner2@x.com", stored_password_hash=hash_password("SenhaForte123"), db_path=self.db_path,
        )
        token = auth.login("owner2@x.com", "SenhaForte123")
        principal = issue_owner_principal(auth_service=auth, session_token=token)
        ar = AuthorityRegistry(db_path=self.db_path)
        root = ar.bootstrap_root_authority(owner_principal=principal, auth_service=auth, bootstrap_reason="setup")
        grant = ar.issue_grant(
            issuer_grant_id=root.grant_id, subject_actor_id="owner2@x.com", authority_level="OWNER",
            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",
        )
        vr = VerifierRegistry(authority_registry=ar, db_path=self.db_path)
        errors = []

        def register(worker_id):
            try:
                vr.register(
                    VerifierRegistration(
                        verifier_id=f"v-{worker_id}", verifier_type="x", authorized=True,
                        health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD,
                    ),
                    authority_grant_id=grant.grant_id, registration_reason="teste concorrente",
                )
            except Exception as exc:
                errors.append((worker_id, exc))

        threads = [threading.Thread(target=register, args=(i,), daemon=True) for i in range(10)]
        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        duration = time.time() - start

        self.assertEqual(len([t for t in threads if t.is_alive()]), 0)
        self.assertLess(duration, 30)
        self.assertEqual(len(errors), 0, f"erros inesperados: {errors}")
        for i in range(10):
            self.assertTrue(vr.is_authorized(f"v-{i}"))


class NoOrphanedWalShmFilesTests(unittest.TestCase):
    """`remove_db_file()` remove o banco principal E os auxiliares de
    WAL (`-wal`/`-shm`) — nunca deixa órfãos."""

    def test_remove_db_file_cleans_wal_and_shm(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(db_path)
        db_module.init_db(db_path)

        # forca a criacao de arquivos -wal/-shm com uma escrita real
        with db_module.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO audit_events (event_id, timestamp, actor, action, resource, outcome, details) "
                "VALUES ('ev1', 0, 'x', 'x', 'x', 'OK', '{}')"
            )

        self.assertTrue(os.path.exists(db_path))
        db_module.remove_db_file(db_path)
        self.assertFalse(os.path.exists(db_path))
        self.assertFalse(os.path.exists(db_path + "-wal"))
        self.assertFalse(os.path.exists(db_path + "-shm"))

    def test_remove_db_file_idempotent_when_nothing_exists(self):
        """Nunca levanta exceção mesmo quando não há nada para remover."""
        db_module.remove_db_file("/tmp/arquivo-que-nunca-existiu-12345.db")


class FullSuiteResourceCleanlinessRegressionTests(unittest.TestCase):
    """Confirma que a causa raiz corrigida (contenção SQLite sem
    busy_timeout) não deixa NENHUM vestígio de recurso pendente após um
    ciclo intenso de uso concorrente + persistência real."""

    def test_repeated_concurrent_cycles_leave_no_resources_behind(self):
        import gc
        import warnings

        from core.diagnostics import dump_resources

        captured = []
        warnings.simplefilter("always", ResourceWarning)
        original = warnings.showwarning

        def capture(message, category, filename, lineno, file=None, line=None):
            if category is ResourceWarning:
                captured.append(str(message))

        warnings.showwarning = capture
        db_paths = []
        try:
            for cycle in range(3):
                fd, db_path = tempfile.mkstemp(suffix=".db")
                os.close(fd)
                os.remove(db_path)
                db_module.init_db(db_path)
                db_paths.append(db_path)

                auth = create_canonical_auth_service(
                    owner_email=f"owner{cycle}@x.com", stored_password_hash=hash_password("SenhaForte123"),
                    db_path=db_path,
                )
                token = auth.login(f"owner{cycle}@x.com", "SenhaForte123")
                principal = issue_owner_principal(auth_service=auth, session_token=token)
                ar = AuthorityRegistry(db_path=db_path)
                root = ar.bootstrap_root_authority(owner_principal=principal, auth_service=auth, bootstrap_reason="x")

                threads = []
                for i in range(6):
                    def work(idx=i):
                        grant = ar.issue_grant(
                            issuer_grant_id=root.grant_id, subject_actor_id=f"w{idx}", authority_level="OWNER",
                            scopes=[SCOPE_REGISTER_VERIFIER], issuance_reason="x",
                        )
                        ar.revoke_grant(grant.grant_id, revoked_by="owner", reason="x")
                    t = threading.Thread(target=work, daemon=True)
                    threads.append(t)
                    t.start()
                for t in threads:
                    t.join(timeout=30)
                self.assertEqual(len([t for t in threads if t.is_alive()]), 0)

                db_module.remove_db_file(db_path)

            gc.collect()
            gc.collect()
        finally:
            warnings.showwarning = original
            for p in db_paths:
                db_module.remove_db_file(p)

        self.assertEqual(len(captured), 0, f"ResourceWarnings inesperados: {captured}")
        dump = dump_resources()
        self.assertTrue(dump["clean"], f"threads não-daemon remanescentes: {dump['non_daemon_non_main_alive']}")


if __name__ == "__main__":
    unittest.main()
