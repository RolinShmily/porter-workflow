"""Normalise a user-supplied source string into either a URL or a local path.

Lives in :mod:`porter.core.utils` (the leaf layer) so both
:mod:`porter.core.models.request` and :mod:`porter.core.workflow.probe` share
one classification rule instead of drifting.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["KNOWN_URL_HOSTS", "normalize_source_input"]

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_EMBEDDED_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_TRAILING_PUNCT = ".,;:!?)]}】》）。，！？'\""  # noqa: RUF001 - intentional CJK share punctuation

#: Known platform and shortener hostnames that users frequently paste without
#: an ``https://`` scheme.
KNOWN_URL_HOSTS: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "bilibili.com",
    "b23.tv",
    "bilibili.tv",
    "biliintl.com",
    "x.com",
    "twitter.com",
    "t.co",
    "tiktok.com",
    "tiktokv.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "instagram.com",
    "instagr.am",
    "ig.me",
)


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def _looks_like_bare_url(text: str) -> bool:
    if " " in text or text.startswith(("/", "~", ".")):
        return False
    # Windows drive letter such as C:\video.mp4 or C:/video.mp4.
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        return False
    lowered = text.lower()
    host = lowered.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if host.startswith("www.") and "." in host[4:]:
        return True
    return any(host == known or host.endswith(f".{known}") for known in KNOWN_URL_HOSTS)


def _looks_like_local_path_prefix(text: str) -> bool:
    if text.startswith(("/", "~", ".", "\\")):
        return True
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def normalize_source_input(raw: str | Path) -> tuple[str, bool]:
    """Return ``(normalised_source, is_url)`` for a user-supplied source.

    Rules, in order:

    1. Strip surrounding whitespace and matching quotes.
    2. ``file://`` is always an explicit local file URI (``is_url=False``).
    3. Any other ``scheme://`` prefix is a URL (``is_url=True``).
    4. An existing path on disk is always a local file (``is_url=False``).
    5. Share text containing an embedded ``http(s)://`` URL (for example
       mobile app copy-paste like ``【标题】 https://b23.tv/abc``) extracts
       that URL (``is_url=True``).
    6. A bare host such as ``youtu.be/abc`` or ``www.youtube.com/watch?v=...``
       is prefixed with ``https://`` (``is_url=True``).
    7. Everything else is treated as a local path (``is_url=False``).
    """
    text = _strip_quotes(str(raw))
    if not text:
        return "", False

    if text.startswith("file://"):
        return text, False

    if _SCHEME_RE.match(text):
        return text, True

    # Never misclassify an actual file on disk (even if weirdly named).
    try:
        if Path(text).expanduser().exists():
            return text, False
    except OSError:
        pass

    # Mobile share copy: "【Title】 https://b23.tv/xxx"
    if not _looks_like_local_path_prefix(text):
        match = _EMBEDDED_URL_RE.search(text)
        if match:
            extracted = match.group(0).rstrip(_TRAILING_PUNCT)
            if extracted:
                return extracted, True

    if _looks_like_bare_url(text):
        return f"https://{text}", True

    return text, False
