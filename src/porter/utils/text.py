"""Text helpers: filesystem-safe naming and CJK detection."""

from __future__ import annotations

import re

__all__ = ["is_cjk", "sanitize_filename", "truncate"]

#: Characters that are illegal in a filename on Windows or POSIX, plus the
#: control characters that make a path unusable on either.
#:
#: A C0/DEL range rather than an explicit ``\r\n\t`` list, because the title
#: comes from remote metadata and so is untrusted. NUL makes ``open()`` raise
#: ``ValueError: embedded null byte`` on Linux while some paths silently
#: truncate at it; ESC lets a title drive the terminal, so a crafted video title
#: could repaint ``porter inspect`` output. Stripping the whole range fixes
#: both without enumerating what abuses them.
#:
#: v0.1 matched only ``[\\/*?:"<>|\r\n\t]``, so NUL and ESC passed through.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/*?:"<>|\x00-\x1f\x7f]')
_RUNS_OF_SPACE_OR_UNDERSCORE = re.compile(r"[\s_]+")

_CJK_START = "\u4e00"
_CJK_END = "\u9fff"


def is_cjk(text: str) -> bool:
    """Return ``True`` if ``text`` contains at least one CJK ideograph.

    Used for the "did the translation actually happen" self-check and for
    choosing CJK-aware line-breaking. Deliberately narrow (CJK Unified
    Ideographs block only) to match v0.1 behaviour.
    """
    return any(_CJK_START <= char <= _CJK_END for char in text)


def sanitize_filename(name: str, max_length: int = 80) -> str:
    """Make ``name`` safe as a cross-platform file or directory name.

    Ported verbatim from v0.1 ``extractors/base.py``: illegal characters become
    underscores, runs of whitespace/underscores collapse to a single ``_``, and
    the result is trimmed to ``max_length`` without a trailing ``.``/``_``/space.
    Falls back to ``"video"`` for empty input.
    """
    sanitized = _ILLEGAL_FILENAME_CHARS.sub("_", name)
    sanitized = _RUNS_OF_SPACE_OR_UNDERSCORE.sub("_", sanitized).strip("._ ")
    if not sanitized:
        sanitized = "video"
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length].rstrip("._ ")
    return sanitized


def truncate(text: str, limit: int, ellipsis: str = "...") -> str:
    """Collapse whitespace, then shorten ``text`` to ``limit`` characters."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    if limit <= len(ellipsis):
        return collapsed[:limit]
    return collapsed[: limit - len(ellipsis)].rstrip() + ellipsis
