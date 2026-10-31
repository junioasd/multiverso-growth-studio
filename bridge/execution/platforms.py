"""
bridge.execution.platforms
==============================
BLOCO FINAL 2 (item 2/4) — adapters REAIS por plataforma. Cada classe
usa a estrutura de endpoint REAL e publicamente documentada da API
oficial correspondente — nunca uma estrutura inventada:

- YouTube Data API v3 (developers.google.com/youtube/v3)
- Meta Graph API (developers.facebook.com/docs/graph-api) — cobre
  Facebook e Instagram (Instagram Graph API é uma extensão do mesmo
  Graph API)
- TikTok API for Developers (developers.tiktok.com)
"""

from __future__ import annotations

from bridge.execution.platform_adapters import PlatformActionResult, PlatformApiAdapter


class YouTubeAdapter(PlatformApiAdapter):
    platform = "youtube"
    base_url = "https://www.googleapis.com/youtube/v3"

    def get_channel_info(self, *, channel_id: str) -> PlatformActionResult:
        return self._request(
            action="GET_CHANNEL_INFO", method="GET", path="/channels",
            params={"part": "snippet,statistics", "id": channel_id},
        )

    def list_videos(self, *, channel_id: str, max_results: int = 25) -> PlatformActionResult:
        return self._request(
            action="LIST_VIDEOS", method="GET", path="/search",
            params={"part": "snippet", "channelId": channel_id, "maxResults": max_results, "type": "video"},
        )

    def update_video_metadata(self, *, video_id: str, title: str, description: str) -> PlatformActionResult:
        return self._request(
            action="UPDATE_VIDEO_METADATA", method="PUT", path="/videos", params={"part": "snippet"},
            body={"id": video_id, "snippet": {"title": title, "description": description}},
        )

    def get_video_statistics(self, *, video_id: str) -> PlatformActionResult:
        return self._request(
            action="GET_VIDEO_STATISTICS", method="GET", path="/videos",
            params={"part": "statistics", "id": video_id},
        )


class MetaGraphAdapter(PlatformApiAdapter):
    """Cobre Facebook e Instagram — ambos servidos pelo mesmo Meta
    Graph API. `platform` é ajustado por instância (`facebook` ou
    `instagram`) para isolamento correto de rate limit/auditoria."""

    base_url = "https://graph.facebook.com/v19.0"

    def __init__(self, *, sub_platform: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.platform = sub_platform  # "facebook" ou "instagram"

    def get_page_info(self, *, page_id: str) -> PlatformActionResult:
        return self._request(
            action="GET_PAGE_INFO", method="GET", path=f"/{page_id}", params={"fields": "id,name,fan_count"},
        )

    def publish_post(self, *, page_id: str, message: str, link: str | None = None) -> PlatformActionResult:
        body = {"message": message}
        if link:
            body["link"] = link
        return self._request(action="PUBLISH_POST", method="POST", path=f"/{page_id}/feed", body=body)

    def get_post_insights(self, *, post_id: str) -> PlatformActionResult:
        return self._request(
            action="GET_POST_INSIGHTS", method="GET", path=f"/{post_id}/insights",
            params={"metric": "post_impressions,post_engaged_users"},
        )

    def get_instagram_media_info(self, *, media_id: str) -> PlatformActionResult:
        return self._request(
            action="GET_INSTAGRAM_MEDIA_INFO", method="GET", path=f"/{media_id}",
            params={"fields": "id,caption,like_count,comments_count"},
        )


class TikTokAdapter(PlatformApiAdapter):
    platform = "tiktok"
    base_url = "https://open.tiktokapis.com/v2"

    def get_user_info(self) -> PlatformActionResult:
        return self._request(
            action="GET_USER_INFO", method="GET", path="/user/info/",
            params={"fields": "open_id,display_name,follower_count"},
        )

    def list_videos(self, *, max_count: int = 20) -> PlatformActionResult:
        return self._request(
            action="LIST_VIDEOS", method="POST", path="/video/list/", params={"fields": "id,title,view_count"},
            body={"max_count": max_count},
        )

    def get_video_analytics(self, *, video_ids: list) -> PlatformActionResult:
        return self._request(
            action="GET_VIDEO_ANALYTICS", method="POST", path="/video/query/",
            params={"fields": "id,like_count,comment_count,share_count,view_count"},
            body={"filters": {"video_ids": video_ids}},
        )
