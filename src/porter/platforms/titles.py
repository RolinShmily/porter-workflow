"""Derive safe, concise titles from platform metadata.

A third duplication cluster
---------------------------
``v0.1`` had four title cleaners. Diffing them shows three are the *same*
algorithm with two parameters changed:

============  ==================  ==============
extractor     fallback prefix     max length
============  ==================  ==============
``x``         ``Tweet_by_``       60
``instagram`` ``Instagram_by_``   50
``tiktok``    ``TikTok_by_``      50
============  ==================  ==============

Only Bilibili differs (it strips a ``_哔哩哔哩_bilibili`` suffix instead of
parsing a caption). So there are two real algorithms here, not four.

Titles end up inside a filesystem path (``<video_id>_<safe_title>``), so these
functions are also a security boundary: they strip URLs and cap length before
:func:`porter.utils.text.sanitize_filename` removes path separators.
"""

from __future__ import annotations

import re

__all__ = [
    "bilibili_title",
    "caption_to_title",
    "tweet_to_title",
]

_URL = re.compile(r"https?://\S+")
_LEADING_MENTIONS = re.compile(r"^(@\w+\s*)+")
_TRAILING_HASHTAGS = re.compile(r"(#\w+\s*)+$")
_ANY_HASHTAG = re.compile(r"#\w+")
_BILIBILI_SUFFIX = re.compile(r"(_|\s*-\s*)(哔哩哔哩|bilibili).*", re.IGNORECASE)

#: Longest title accepted from a tweet. Twitter's own limit is 280; a title
#: longer than this is not useful in a filename.
_TWEET_MAX = 60


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        if stripped := line.strip():
            return stripped
    return ""


def caption_to_title(
    caption: str | None,
    uploader: str | None,
    *,
    fallback_prefix: str,
    max_length: int,
) -> str:
    """Turn a social caption into a title.

    Strips URLs, leading ``@mentions`` and trailing ``#hashtags``. If the first
    line is nothing but hashtags, the next line with real content is used. Falls
    back to ``<prefix><uploader>`` when nothing usable remains.

    Args:
        caption: Raw caption or post text.
        uploader: Handle used in the fallback title.
        fallback_prefix: e.g. ``"TikTok_by_"``.
        max_length: Hard cap on the returned title.

    Returns:
        A title of at most ``max_length`` characters, never empty.
    """
    fallback = f"{fallback_prefix}{uploader or 'unknown'}"

    if not caption:
        return fallback

    cleaned = _URL.sub("", caption).strip()
    first_line = _first_nonempty_line(cleaned)
    if not first_line:
        return fallback

    first_line = _LEADING_MENTIONS.sub("", first_line).strip()
    first_line = _TRAILING_HASHTAGS.sub("", first_line).strip()

    if not first_line:
        # The first line was purely hashtags; look for content elsewhere.
        without_tags = _ANY_HASHTAG.sub("", cleaned).strip()
        first_line = _first_nonempty_line(without_tags)

    if not first_line:
        return fallback

    return first_line[:max_length].strip()


def tweet_to_title(
    tweet_text: str | None,
    uploader: str | None,
    status_id: str,
) -> str:
    """Title for an X / Twitter video.

    Like :func:`caption_to_title` but keeps 60 characters, because a tweet's
    first line is usually the whole point of the post.
    """
    return caption_to_title(
        tweet_text,
        uploader or status_id,
        fallback_prefix="Tweet_by_",
        max_length=_TWEET_MAX,
    )


def bilibili_title(raw_title: str | None, bvid: str) -> str:
    """Title for a Bilibili video.

    Bilibili appends ``_哔哩哔哩_bilibili`` (or a ``- bilibili`` variant) to
    titles it reports, which is noise in a filename.
    """
    fallback = f"bilibili_{bvid}"
    if not raw_title:
        return fallback

    cleaned = _BILIBILI_SUFFIX.sub("", raw_title).strip()
    cleaned = _URL.sub("", cleaned).strip()

    if not cleaned:
        return fallback

    return cleaned[:60].strip()
