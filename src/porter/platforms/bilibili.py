"""Bilibili.

Its CC subtitle track arrives as JSON with cue times in seconds, which
:func:`porter.subtitles.srt.bilibili_json_to_srt` converts to SRT. Titles arrive
with a ``_哔哩哔哩_bilibili`` suffix to strip.

The CC track is requested from yt-dlp like any other platform's, and **found
nothing without cookies** in every probe made here. It used to be marked
``remote=False`` on the theory that the track was fetched over HTTP instead;
nothing implemented that, so bilibili silently discarded its own exact Chinese
track and ran ASR on every job. Requesting it costs nothing when there is nothing
to find, and it is the only way the track can be used when cookies make it
visible.
"""

from __future__ import annotations

import re

from porter.platforms.spec import PlatformSpec, SubtitleSource
from porter.platforms.titles import bilibili_title

__all__ = ["SPEC"]

SPEC = PlatformSpec(
    name="bilibili",
    display_name="Bilibili",
    url_patterns=(
        # Videos: bilibili.com/video/BV... or av..., plus festival deep links
        re.compile(
            r"^https?://(?:(?:www|m)\.)?bilibili\.com/(?:video/|festival/[^/?#]+\?"
            r"(?:[^#]*&)?bvid=)(?P<id>[a-zA-Z0-9]+)"
        ),
        re.compile(
            r"^https?://(?:(?:www|m)\.)?bilibili\.com/bangumi/play/(?P<id>(?:ep|ss)\d+)"
        ),
        re.compile(
            r"^https?://(?:(?:www|m)\.)?bilibili\.com/cheese/play/(?P<id>(?:ep|ss)\d+)"
        ),
        re.compile(r"^https?://(?:www\.)?b23\.tv/(?P<id>[a-zA-Z0-9]+)"),
        re.compile(
            r"^https?://(?:www\.)?bili(?:bili\.tv|intl\.com)/(?:[a-zA-Z]{2}/)?"
            r"(?:play|video)/(?P<id>\d+)"
        ),
        re.compile(
            r"^https?://(?:t\.bilibili\.com|(?:www\.)?bilibili\.com/opus)/(?P<id>\d+)"
        ),
    ),
    url_hosts=("bilibili.com", "b23.tv", "bilibili.tv", "biliintl.com"),
    video_id_group="id",
    default_vertical=False,
    format_selector="bestvideo*+bestaudio/best",
    clean_title=lambda raw, vid: bilibili_title(raw, vid),
    subtitles=SubtitleSource(remote=True, prefer_existing_chinese=True),
    retries_on_rate_limit=True,
    notes="CC subtitles arrive as JSON; yt-dlp returns none without cookies.",
)
