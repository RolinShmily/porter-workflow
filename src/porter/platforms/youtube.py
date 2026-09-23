"""YouTube.

The heaviest platform: multiple URL shapes, optional human subtitles plus
auto-generated captions, and (since yt-dlp 2025.11.12) a JavaScript challenge
that needs an external runtime. See :mod:`porter.platforms.ydl`.
"""

from __future__ import annotations

import re

from porter.platforms.spec import PlatformSpec, SubtitleSource

__all__ = ["SPEC"]

_ID = r"(?P<id>[a-zA-Z0-9_-]{11})"

SPEC = PlatformSpec(
    name="youtube",
    display_name="YouTube",
    url_patterns=(
        re.compile(rf"^https?://(?:www\.)?youtube\.com/watch\?v={_ID}"),
        re.compile(rf"^https?://(?:www\.)?youtube\.com/shorts/{_ID}"),
        re.compile(rf"^https?://(?:www\.)?youtube\.com/embed/{_ID}"),
        re.compile(rf"^https?://(?:www\.)?youtube\.com/v/{_ID}"),
        re.compile(rf"^https?://youtu\.be/{_ID}"),
    ),
    url_hosts=("youtube.com", "youtu.be"),
    video_id_group="id",
    default_vertical=False,
    format_selector="bestvideo*[height<=1080]+bestaudio/best[height<=1080]/best",
    player_clients=(
        "web_embedded",
        "web",
        "mweb",
        "android_vr",
        "ios",
        "android",
    ),
    subtitles=SubtitleSource(remote=True, prefer_existing_chinese=True),
    # `t=15s` is a start offset here, not share tracking.
    keep_query_params=frozenset({"t"}),
    notes=(
        "Requires an external JavaScript runtime (Deno recommended, Node >= 20 "
        "also works) so yt-dlp can solve YouTube's JS challenges."
    ),
)
