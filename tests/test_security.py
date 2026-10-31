import time
import unittest

from core.security.auth import (
    AuthError,
    OwnerAuthService,
    RateLimitExceeded,
    hash_password,
    verify_password,
)
from core.security.rbac import AccessDenied, Permission, Principal, Role, require_owner, require_permission
from core.security.sanitize import assert_no_secrets, sanitize_dict


class PasswordHashingTests(unittest.TestCase):
    def test_hash_and_verify_roundtrip(self):
        h = hash_password("SuperSecret123")
        self.assertTrue(verify_password("SuperSecret123", h))

    def test_wrong_password_fails(self):
        h = hash_password("SuperSecret123")
        self.assertFalse(verify_password("WrongPassword", h))

    def test_short_password_rejected(self):
        with self.assertRaises(ValueError):
            hash_password("short")

    def test_hash_never_stores_plaintext(self):
        h = hash_password("SuperSecret123")
        self.assertNotIn("SuperSecret123", h)


class OwnerLoginFlowTests(unittest.TestCase):
    def setUp(self):
        from tests._auth_test_helpers import fast_hash_password

        self.password_hash = fast_hash_password("CorrectHorseBattery1")
        self.service = OwnerAuthService(owner_email="owner@example.com", stored_password_hash=self.password_hash)

    def test_successful_login_returns_token(self):
        token = self.service.login("owner@example.com", "CorrectHorseBattery1")
        self.assertIsInstance(token, str)
        session = self.service.current_session(token)
        self.assertEqual(session["role"], "OWNER")

    def test_login_wrong_password_denied(self):
        with self.assertRaises(AuthError):
            self.service.login("owner@example.com", "wrongpass")

    def test_login_without_configured_hash_denied(self):
        svc = OwnerAuthService(owner_email="owner@example.com", stored_password_hash=None)
        with self.assertRaises(AuthError):
            svc.login("owner@example.com", "whatever12345")

    def test_logout_revokes_session(self):
        token = self.service.login("owner@example.com", "CorrectHorseBattery1")
        self.service.logout(token)
        with self.assertRaises(AuthError):
            self.service.current_session(token)

    def test_rate_limit_after_repeated_failures(self):
        for _ in range(5):
            try:
                self.service.login("owner@example.com", "wrong")
            except AuthError:
                pass
        with self.assertRaises(RateLimitExceeded):
            self.service.login("owner@example.com", "wrong")


class RBACTests(unittest.TestCase):
    def test_owner_has_all_permissions(self):
        p = Principal(subject="owner@example.com", role=Role.OWNER)
        require_permission(p, Permission.MANAGE_CREDENTIALS)  # não deve levantar

    def test_agent_cannot_manage_credentials(self):
        p = Principal(subject="agent-1", role=Role.AGENT)
        with self.assertRaises(AccessDenied):
            require_permission(p, Permission.MANAGE_CREDENTIALS)

    def test_require_owner_denies_non_owner(self):
        p = Principal(subject="viewer-1", role=Role.VIEWER)
        with self.assertRaises(AccessDenied):
            require_owner(p)


class SanitizationTests(unittest.TestCase):
    def test_password_key_redacted(self):
        data = {"email": "a@b.com", "password": "hunter2"}
        safe = sanitize_dict(data)
        self.assertEqual(safe["password"], "[REDACTED]")
        self.assertEqual(safe["email"], "a@b.com")

    def test_nested_secret_redacted(self):
        data = {"user": {"api_key": "abc123"}}
        safe = sanitize_dict(data)
        self.assertEqual(safe["user"]["api_key"], "[REDACTED]")

    def test_assert_no_secrets_detects_offenders(self):
        offenders = assert_no_secrets({"token": "abc", "name": "ok"})
        self.assertIn("token", offenders)
        self.assertNotIn("name", offenders)


if __name__ == "__main__":
    unittest.main()
