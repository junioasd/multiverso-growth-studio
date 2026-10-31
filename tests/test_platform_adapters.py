import json
import unittest
from unittest.mock import MagicMock, patch

from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.execution.platforms import MetaGraphAdapter, TikTokAdapter, YouTubeAdapter
from bridge.execution.vault import TokenType, TokenVault

"""
Testes BLOCO FINAL 2 (item 2/4 — NAVIGATOR REAL / integrações
comerciais): base de adapter real, credencial verificada no cofre,
estrutura real de endpoint por plataforma — sempre com
`urllib.request.urlopen` mockado (nunca dependência de rede real).
"""


def _fake_response(payload: dict, status: int = 200):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.status = status
    response.__enter__ = lambda self: response
    response.__exit__ = lambda self, *a: None
    return response


class _FakeAdapter(PlatformApiAdapter):
    platform = "fake"
    base_url = "https://api.fake.example/v1"


class PlatformApiAdapterBaseTests(unittest.TestCase):
    def test_no_vault_reports_unavailable(self):
        adapter = _FakeAdapter(account_reference_id="conta1")
        result = adapter._request(action="X", method="GET", path="/ping")
        self.assertFalse(result.success)
        self.assertIn("nenhuma credencial real", result.reason)

    def test_vault_without_stored_token_reports_unavailable(self):
        vault = TokenVault()
        adapter = _FakeAdapter(vault=vault, account_reference_id="conta_sem_token")
        result = adapter._request(action="X", method="GET", path="/ping")
        self.assertFalse(result.success)

    def test_successful_call_with_credential(self):
        vault = TokenVault()
        ref = vault.store_token(platform="fake", token_type=TokenType.API_KEY, value="tok")
        adapter = _FakeAdapter(vault=vault, account_reference_id=ref)
        with patch("urllib.request.urlopen", return_value=_fake_response({"ok": True})):
            result = adapter._request(action="X", method="GET", path="/ping")
        self.assertTrue(result.success)
        self.assertEqual(result.data, {"ok": True})

    def test_rate_limit_enforced(self):
        from bridge.execution.rate_limiter import RateLimiter

        vault = TokenVault()
        ref = vault.store_token(platform="fake", token_type=TokenType.API_KEY, value="tok")
        limiter = RateLimiter()
        limiter.configure(key="fake:" + ref, capacity=1, refill_rate_per_second=0.01)
        adapter = _FakeAdapter(vault=vault, account_reference_id=ref, rate_limiter=limiter)
        with patch("urllib.request.urlopen", return_value=_fake_response({"ok": True})):
            r1 = adapter._request(action="X", method="GET", path="/ping")
            r2 = adapter._request(action="X", method="GET", path="/ping")
        self.assertTrue(r1.success)
        self.assertFalse(r2.success)
        self.assertIn("rate limit", r2.reason)

    def test_http_error_reported_with_status_code(self):
        import urllib.error

        vault = TokenVault()
        ref = vault.store_token(platform="fake", token_type=TokenType.API_KEY, value="tok")
        adapter = _FakeAdapter(vault=vault, account_reference_id=ref)
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)):
            result = adapter._request(action="X", method="GET", path="/ping")
        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 401)


class YouTubeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.vault = TokenVault()
        self.ref = self.vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
        self.adapter = YouTubeAdapter(vault=self.vault, account_reference_id=self.ref)

    def test_get_channel_info_uses_real_endpoint_structure(self):
        with patch("urllib.request.urlopen", return_value=_fake_response({"items": []})) as mock_urlopen:
            self.adapter.get_channel_info(channel_id="UC123")
            called_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("googleapis.com/youtube/v3/channels", called_url)
        self.assertIn("id=UC123", called_url)

    def test_update_video_metadata_uses_put(self):
        with patch("urllib.request.urlopen", return_value=_fake_response({"id": "v1"})) as mock_urlopen:
            self.adapter.update_video_metadata(video_id="v1", title="Novo Titulo", description="desc")
            request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "PUT")


class MetaGraphAdapterTests(unittest.TestCase):
    def test_facebook_and_instagram_isolated_by_platform(self):
        vault = TokenVault()
        ref_fb = vault.store_token(platform="facebook", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok_fb")
        ref_ig = vault.store_token(platform="instagram", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok_ig")
        fb = MetaGraphAdapter(sub_platform="facebook", vault=vault, account_reference_id=ref_fb)
        ig = MetaGraphAdapter(sub_platform="instagram", vault=vault, account_reference_id=ref_ig)
        self.assertEqual(fb.platform, "facebook")
        self.assertEqual(ig.platform, "instagram")
        self.assertNotEqual(fb._rate_limit_key(), ig._rate_limit_key())

    def test_publish_post_uses_real_graph_endpoint(self):
        vault = TokenVault()
        ref = vault.store_token(platform="facebook", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
        fb = MetaGraphAdapter(sub_platform="facebook", vault=vault, account_reference_id=ref)
        with patch("urllib.request.urlopen", return_value=_fake_response({"id": "post1"})) as mock_urlopen:
            fb.publish_post(page_id="page123", message="Ola mundo")
            called_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("graph.facebook.com", called_url)
        self.assertIn("page123/feed", called_url)


class TikTokAdapterTests(unittest.TestCase):
    def test_get_user_info_uses_real_endpoint(self):
        vault = TokenVault()
        ref = vault.store_token(platform="tiktok", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="tok")
        tk = TikTokAdapter(vault=vault, account_reference_id=ref)
        with patch("urllib.request.urlopen", return_value=_fake_response({"data": {}})) as mock_urlopen:
            tk.get_user_info()
            called_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("open.tiktokapis.com", called_url)


if __name__ == "__main__":
    unittest.main()
