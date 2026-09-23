"""Instagram.

Reels, posts, IGTV and stories. Carousel posts are playlists in yt-dlp terms,
so the first entry carrying a video stream wins.
"""

from __future__ import annotations

import re

from porter.platforms.spec import PlatformSpec, SubtitleSource
from porter.platforms.titles import caption_to_title

__all__ = ["SPEC"]

_INSTAGRAM = r"(?:instagram\.com|instagr\.am)"

SPEC = PlatformSpec(
    name="instagram",
    display_name="Instagram",
    url_patterns=(
        re.compile(
            rf"^https?://(?:(?:www|m)\.)?{_INSTAGRAM}"
            r"/(?:(?:share/)?reels?)/(?P<id>[a-zA-Z0-9_-]+)"
        ),
        re.compile(
            rf"^https?://(?:(?:www|m)\.)?{_INSTAGRAM}"
            r"/(?:(?:share/)?(?:p|tv))/(?P<id>[a-zA-Z0-9_-]+)"
        ),
        re.compile(
            rf"^https?://(?:(?:www|m)\.)?{_INSTAGRAM}"
            r"/stories/(?P<user>[^/?#]+)/(?P<id>\d+)"
        ),
        re.compile(
            rf"^https?://(?:(?:www|m)\.)?{_INSTAGRAM}"
            r"/(?!share/)[^/?#]+/(?:p|tv|reels?)/(?P<id>[a-zA-Z0-9_-]+)"
        ),
        re.compile(r"^https?://ig\.me/(?P<id>[a-zA-Z0-9_-]+)"),
    ),
    url_hosts=("instagram.com", "instagr.am", "ig.me"),
    video_id_group="id",
    default_vertical=False,
    format_selector="bestvideo*+bestaudio/best",
    clean_title=lambda raw, vid: caption_to_title(
        raw, None, fallback_prefix="Instagram_by_", max_length=50
    ),
    subtitles=SubtitleSource(remote=False, prefer_existing_chinese=False),
    notes="Login walls are common; an embed fallback and cookies improve reliability.",
)
