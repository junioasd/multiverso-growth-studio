import unittest

from core.security.sanitize import assert_no_secrets, sanitize_dict, sanitize_text


class SecretSanitizationTests(unittest.TestCase):
    def test_redacts_common_secret_keys(self):
        data = {
            "password": "x", "senha": "x", "token": "x", "api_key": "x",
            "client_secret": "x", "private_key": "x", "session_secret": "x", "cookie": "x",
        }
        safe = sanitize_dict(data)
        for key in data:
            self.assertEqual(safe[key], "[REDACTED]", f"chave não redigida: {key}")

    def test_preserves_non_sensitive_keys(self):
        data = {"channel_id": "chan_1", "niche": "saude"}
        safe = sanitize_dict(data)
        self.assertEqual(safe, data)

    def test_deeply_nested_secret_redacted(self):
        data = {"level1": {"level2": {"api_key": "leak"}}}
        safe = sanitize_dict(data)
        self.assertEqual(safe["level1"]["level2"]["api_key"], "[REDACTED]")

    def test_assert_no_secrets_empty_for_clean_data(self):
        offenders = assert_no_secrets({"channel_id": "chan_1"})
        self.assertEqual(offenders, [])

    def test_sanitize_text_redacts_inline_pattern(self):
        text = "login failed for password=hunter2"
        redacted = sanitize_text(text)
        self.assertNotIn("hunter2", redacted)


if __name__ == "__main__":
    unittest.main()
