"""TikTok.

Defaults to a vertical frame because TikTok's metadata frequently omits usable
dimensions, and every TikTok video is 9:16 unless it was explicitly uploaded
otherwise. Also retries on rate limits, which the platform applies aggressively.
"""

from __future__ import annotations

import re

from porter.platforms.spec import PlatformSpec, SubtitleSource
from porter.platforms.titles import caption_to_title

__all__ = ["SPEC"]

SPEC = PlatformSpec(
    name="tiktok",
    display_name="TikTok",
    url_patterns=(
        re.compile(
            r"^https?://(?:(?:www|m)\.)?tiktok\.com/@(?P<user>[\w\.-]+)/video/(?P<id>\d+)"
        ),
        re.compile(
            r"^https?://(?:(?:www|m)\.)?tiktok\.com/@(?P<user>[\w\.-]+)/photo/(?P<id>\d+)"
        ),
        re.compile(
            r"^https?://(?:(?:www|m)\.)?tiktok\.com/(?:embed(?:/v2)?|share/video|v)/(?P<id>\d+)"
        ),
        re.compile(r"^https?://(?:vm|vt)\.tiktok\.com/(?P<id>[\w-]+)"),
        re.compile(r"^https?://(?:(?:www|m)\.)?tiktok\.com/t/(?P<id>[\w-]+)"),
    ),
    url_hosts=("tiktok.com", "tiktokv.com", "vm.tiktok.com", "vt.tiktok.com"),
    video_id_group="id",
    default_vertical=True,
    format_selector="bestvideo*+bestaudio/best",
    clean_title=lambda raw, vid: caption_to_title(
        raw, None, fallback_prefix="TikTok_by_", max_length=50
    ),
    subtitles=SubtitleSource(remote=True, prefer_existing_chinese=False),
    retries_on_rate_limit=True,
    notes="Image-only slideshows carry no video stream and are rejected explicitly.",
)
