"""X / Twitter.

The title is the tweet text, which is usually the point of the post, so the
cleaner keeps more of it than the other caption-based platforms.
"""

from __future__ import annotations

import re

from porter.platforms.spec import PlatformSpec, SubtitleSource
from porter.platforms.titles import tweet_to_title

__all__ = ["SPEC"]

SPEC = PlatformSpec(
    name="x",
    display_name="X / Twitter",
    url_patterns=(
        re.compile(
            r"^https?://(?:(?:www|m|mobile)\.)?(?:twitter|x)\.com/"
            r"(?:(?:i/web|[^/]+)/status|statuses)/(?P<id>\d+)"
        ),
        re.compile(
            r"^https?://(?:(?:www|m|mobile)\.)?(?:twitter|x)\.com/i/status/(?P<id>\d+)"
        ),
        re.compile(r"^https?://t\.co/(?P<id>[a-zA-Z0-9_-]+)"),
    ),
    url_hosts=("x.com", "twitter.com", "t.co"),
    video_id_group="id",
    default_vertical=False,
    format_selector="bestvideo*+bestaudio/best",
    clean_title=lambda raw, vid: tweet_to_title(raw, None, vid),
    # X exposes no subtitle tracks; the ASR chain always runs.
    subtitles=SubtitleSource(remote=False, prefer_existing_chinese=False),
    notes="Age-restricted and protected posts need cookies.",
)
