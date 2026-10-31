import http.cookiejar
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from tests._auth_test_helpers import fast_hash_password

from api.bootstrap import build_app
from api.server import run_server
from core.models import db as db_module
from core.security.auth import AuthError, OwnerAuthService

"""
Testes de regressão — MICROCORREÇÃO 3, item 3
(REVOGAÇÃO DURÁVEL DE SESSÕES).

Falha original: uma sessão encerrada por logout voltava a ser aceita
por uma NOVA instância do serviço de autenticação usando a mesma chave
de assinatura — a revogação vivia só em memória do processo.

Usa uma chave de assinatura SINTÉTICA e ESTÁVEL (via SESSION_SECRET
fixo no ambiente do teste) para garantir que duas instâncias validem o
mesmo token — sem depender de nenhuma credencial real.
"""


class PersistenceTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            db_module.remove_db_file(self.db_path)


class SessionStoreDurabilityTests(PersistenceTestBase):
    def test_revocation_visible_in_same_instance(self):
        svc = OwnerAuthService(
            owner_email="owner@x.test", stored_password_hash=fast_hash_password("SyntheticPass123"),
            db_path=self.db_path,
        )
        token = svc.login("owner@x.test", "SyntheticPass123")
        svc.current_session(token)  # não deve levantar
        svc.logout(token)
        with self.assertRaises(AuthError):
            svc.current_session(token)

    def test_revocation_visible_in_new_instance_same_db(self):
        svc1 = OwnerAuthService(
            owner_email="owner@x.test", stored_password_hash=fast_hash_password("SyntheticPass123"),
            db_path=self.db_path,
        )
        token = svc1.login("owner@x.test", "SyntheticPass123")
        svc1.logout(token)

        # Nova instância — NUNCA chamou logout() diretamente, mas
        # compartilha o mesmo db_path.
        svc2 = OwnerAuthService(
            owner_email="owner@x.test", stored_password_hash=fast_hash_password("SyntheticPass123"),
            db_path=self.db_path,
        )
        with self.assertRaises(AuthError):
            svc2.current_session(token)

    def test_revocation_visible_in_instance_created_before_revoke(self):
        # svc2 é criada ANTES da revogação — seu cache local de
        # revoked_jti nasce vazio; mesmo assim deve consultar o banco
        # na hora e recusar o token.
        svc1 = OwnerAuthService(
            owner_email="owner@x.test", stored_password_hash=fast_hash_password("SyntheticPass123"),
            db_path=self.db_path,
        )
        svc2 = OwnerAuthService(
            owner_email="owner@x.test", stored_password_hash=fast_hash_password("SyntheticPass123"),
            db_path=self.db_path,
        )

        token = svc1.login("owner@x.test", "SyntheticPass123")
        self.assertEqual(len(svc2.sessions._revoked_jti), 0)  # cache local de svc2 realmente vazio

        svc1.logout(token)
        with self.assertRaises(AuthError):
            svc2.current_session(token)

    def test_non_revoked_session_still_works_across_instances(self):
        svc1 = OwnerAuthService(
            owner_email="owner@x.test", stored_password_hash=fast_hash_password("SyntheticPass123"),
            db_path=self.db_path,
        )
        token = svc1.login("owner@x.test", "SyntheticPass123")

        svc2 = OwnerAuthService(
            owner_email="owner@x.test", stored_password_hash=fast_hash_password("SyntheticPass123"),
            db_path=self.db_path,
        )
        session = svc2.current_session(token)  # não deve levantar
        self.assertEqual(session["sub"], "owner@x.test")

    def test_revocation_survives_full_restart_simulation(self):
        svc1 = OwnerAuthService(
            owner_email="owner@x.test", stored_password_hash=fast_hash_password("SyntheticPass123"),
            db_path=self.db_path,
        )
        token = svc1.login("owner@x.test", "SyntheticPass123")
        svc1.logout(token)
        del svc1  # descarta a instância — simula fim do processo

        svc_after_restart = OwnerAuthService(
            owner_email="owner@x.test", stored_password_hash=fast_hash_password("SyntheticPass123"),
            db_path=self.db_path,
        )
        with self.assertRaises(AuthError):
            svc_after_restart.current_session(token)


class HttpLogoutCookieTests(PersistenceTestBase):
    def setUp(self):
        super().setUp()
        self.app = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=False,
        )
        self.server = run_server(host="127.0.0.1", port=0, app=self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        time.sleep(0.15)
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_logout_clears_cookie_and_blocks_further_access(self):
        req = urllib.request.Request(
            self._url("/api/login"),
            data=json.dumps({"email": "owner@test.com", "password": "TestPassword123"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.opener.open(req)
        self.assertTrue(any(c.name == "mgs_session" for c in self.cj))

        req = urllib.request.Request(self._url("/api/logout"), data=b"{}", headers={"Content-Type": "application/json"})
        self.opener.open(req)

        # o cookie deve ter sido limpo (Max-Age=0) — não deve mais estar no jar
        remaining = [c for c in self.cj if c.name == "mgs_session"]
        self.assertEqual(len(remaining), 0)

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.opener.open(self._url("/api/dashboard"))
        self.assertEqual(ctx.exception.code, 401)

    def test_logout_revocation_persists_across_new_server_instance(self):
        req = urllib.request.Request(
            self._url("/api/login"),
            data=json.dumps({"email": "owner@test.com", "password": "TestPassword123"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.opener.open(req)
        token = next(c.value for c in self.cj if c.name == "mgs_session")

        req = urllib.request.Request(self._url("/api/logout"), data=b"{}", headers={"Content-Type": "application/json"})
        self.opener.open(req)

        self.server.shutdown()
        self.server.server_close()

        # sobe um SEGUNDO servidor/app, mesmo db_path — nunca viu o logout diretamente
        app2 = build_app(
            owner_email="owner@test.com", owner_password_hash=fast_hash_password("TestPassword123"),
            db_path=self.db_path, init_database=False,
        )
        server2 = run_server(host="127.0.0.1", port=0, app=app2)
        port2 = server2.server_address[1]
        threading.Thread(target=server2.serve_forever, daemon=True).start()
        time.sleep(0.15)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port2}/api/dashboard")
            req.add_header("Cookie", f"mgs_session={token}")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 401)
        finally:
            server2.shutdown()
            server2.server_close()


if __name__ == "__main__":
    unittest.main()
