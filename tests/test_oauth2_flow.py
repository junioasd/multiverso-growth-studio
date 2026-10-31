import json
import urllib.error
import unittest
from unittest.mock import MagicMock, patch

from bridge.execution.oauth2 import OAuth2Flow, OAuth2ProviderConfig

"""
Testes BLOCO FINAL 2 (item 1 — OAuth2/scopes): construção real de URL
de autorização (via oauthlib), troca/renovação real de token — SEMPRE
com `urllib.request.urlopen` mockado (nunca dependência de rede real
em testes, disciplina estabelecida na correção de suite-order
stability).
"""


def _fake_response(payload: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__ = lambda self: response
    response.__exit__ = lambda self, *a: None
    return response


class OAuth2FlowTests(unittest.TestCase):
    def setUp(self):
        self.config = OAuth2ProviderConfig(
            platform="youtube", authorization_endpoint="https://accounts.google.com/o/oauth2/auth",
            token_endpoint="https://oauth2.googleapis.com/token", client_id="cid", client_secret="csecret",
            redirect_uri="https://app.example/callback", scopes=["youtube.readonly"],
        )
        self.flow = OAuth2Flow(self.config)

    def test_authorization_url_contains_real_parameters(self):
        url, state = self.flow.build_authorization_url()
        self.assertIn("accounts.google.com", url)
        self.assertIn("client_id=cid", url)
        self.assertIn(state, url)

    def test_state_is_unique_per_call(self):
        _, state1 = self.flow.build_authorization_url()
        _, state2 = self.flow.build_authorization_url()
        self.assertNotEqual(state1, state2)

    def test_explicit_state_preserved(self):
        _, state = self.flow.build_authorization_url(state="meu_state_fixo")
        self.assertEqual(state, "meu_state_fixo")

    def test_successful_token_exchange(self):
        with patch("urllib.request.urlopen", return_value=_fake_response({
            "access_token": "tok_real", "refresh_token": "ref_real", "expires_in": 3600,
        })):
            result = self.flow.exchange_code_for_token(authorization_code="code123")
        self.assertTrue(result.success)
        self.assertEqual(result.access_token, "tok_real")
        self.assertIsNotNone(result.expires_at)

    def test_missing_access_token_never_treated_as_success(self):
        with patch("urllib.request.urlopen", return_value=_fake_response({"token_type": "bearer"})):
            result = self.flow.exchange_code_for_token(authorization_code="code123")
        self.assertFalse(result.success)

    def test_provider_error_response_reported(self):
        with patch("urllib.request.urlopen", return_value=_fake_response({"error": "invalid_grant"})):
            result = self.flow.exchange_code_for_token(authorization_code="codigo_invalido")
        self.assertFalse(result.success)
        self.assertIn("invalid_grant", result.reason)

    def test_real_network_error_path_handled(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("recusado (simulado)")):
            result = self.flow.exchange_code_for_token(authorization_code="code123")
        self.assertFalse(result.success)
        self.assertIn("falha real de rede", result.reason)

    def test_refresh_token_flow(self):
        with patch("urllib.request.urlopen", return_value=_fake_response({
            "access_token": "novo_tok", "expires_in": 1800,
        })):
            result = self.flow.refresh_access_token(refresh_token="ref_antigo")
        self.assertTrue(result.success)
        self.assertEqual(result.access_token, "novo_tok")

    def test_invalid_json_response_handled(self):
        response = MagicMock()
        response.read.return_value = b"nao e json valido {{{"
        response.__enter__ = lambda self: response
        response.__exit__ = lambda self, *a: None
        with patch("urllib.request.urlopen", return_value=response):
            result = self.flow.exchange_code_for_token(authorization_code="x")
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
