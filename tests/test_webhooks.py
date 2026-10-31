import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from bridge.execution.webhooks import RetryPolicy, WebhookDispatcher, sign_payload

"""
Testes BLOCO FINAL 2 (item 5 — webhooks/retries): entrega real com
retry real, backoff exponencial real, assinatura HMAC real — sempre
com rede mockada e `sleep_fn` injetado (nunca `time.sleep` real em
testes, disciplina de suite-order-stability).
"""


def _fake_response(status: int = 200):
    response = MagicMock()
    response.status = status
    response.__enter__ = lambda self: response
    response.__exit__ = lambda self, *a: None
    return response


class RetryPolicyTests(unittest.TestCase):
    def test_exponential_backoff_real_growth(self):
        policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=100.0)
        self.assertEqual(policy.delay_for_attempt(1), 1.0)
        self.assertEqual(policy.delay_for_attempt(2), 2.0)
        self.assertEqual(policy.delay_for_attempt(3), 4.0)

    def test_delay_capped_at_max(self):
        policy = RetryPolicy(base_delay_seconds=10.0, max_delay_seconds=15.0)
        self.assertEqual(policy.delay_for_attempt(5), 15.0)

    def test_invalid_attempt_number_rejected(self):
        policy = RetryPolicy()
        with self.assertRaises(ValueError):
            policy.delay_for_attempt(0)


class WebhookDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.dispatcher = WebhookDispatcher(
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=1.0), sleep_fn=self.sleeps.append,
        )

    def test_success_on_first_attempt_no_retry(self):
        with patch("urllib.request.urlopen", return_value=_fake_response(200)):
            result = self.dispatcher.deliver(url="https://x.example/webhook", payload={"e": 1})
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(self.sleeps, [])

    def test_failure_never_exceeds_max_attempts(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("recusado")):
            result = self.dispatcher.deliver(url="https://x.example/webhook", payload={"e": 1})
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 3)

    def test_backoff_delays_match_policy(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("recusado")):
            self.dispatcher.deliver(url="https://x.example/webhook", payload={"e": 1})
        self.assertEqual(self.sleeps, [1.0, 2.0])  # nunca dorme apos a ultima tentativa

    def test_succeeds_after_transient_failure(self):
        responses = [urllib.error.URLError("transitorio"), _fake_response(200)]

        def side_effect(*args, **kwargs):
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = self.dispatcher.deliver(url="https://x.example/webhook", payload={"e": 1})
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    def test_non_2xx_status_treated_as_failure(self):
        with patch("urllib.request.urlopen", return_value=_fake_response(500)):
            result = self.dispatcher.deliver(url="https://x.example/webhook", payload={"e": 1})
        self.assertFalse(result.success)


class WebhookSignatureTests(unittest.TestCase):
    def test_valid_signature_verifies(self):
        sig = sign_payload(payload_bytes=b"dados", secret="segredo")
        self.assertTrue(WebhookDispatcher.verify_signature(payload_bytes=b"dados", secret="segredo", received_signature=sig))

    def test_wrong_secret_fails_verification(self):
        sig = sign_payload(payload_bytes=b"dados", secret="segredo")
        self.assertFalse(WebhookDispatcher.verify_signature(payload_bytes=b"dados", secret="outro", received_signature=sig))

    def test_tampered_payload_fails_verification(self):
        sig = sign_payload(payload_bytes=b"dados originais", secret="segredo")
        self.assertFalse(WebhookDispatcher.verify_signature(payload_bytes=b"dados alterados", secret="segredo", received_signature=sig))


if __name__ == "__main__":
    unittest.main()
