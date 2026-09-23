"""Timestamp conversion shared by the SRT and ASS serialisers.

Ported verbatim from the v0.1 ``subtitle/formatter.py`` so that byte-for-byte
output compatibility is preserved (see ``docs/REFACTOR_PLAN.md`` §10.2:
regression assertions may not change).
"""

from __future__ import annotations

__all__ = ["ms_to_ass_time", "ms_to_srt_time", "srt_time_to_ms"]


def srt_time_to_ms(time_str: str) -> int:
    """Convert an SRT (``00:01:23,456``) or ASS (``0:01:23.45``) stamp to ms.

    Returns ``0`` for anything unparseable — callers treat that as "no timing".
    """
    time_str = time_str.strip().replace(",", ".")
    parts = time_str.split(":")
    if len(parts) != 3:
        return 0
    h = int(parts[0])
    m = int(parts[1])
    s_parts = parts[2].split(".")
    s = int(s_parts[0])
    ms_str = (s_parts[1] + "000")[:3] if len(s_parts) > 1 else "000"
    ms = int(ms_str)
    return h * 3600000 + m * 60000 + s * 1000 + ms


def ms_to_srt_time(ms: int) -> str:
    """Convert milliseconds to SRT time format ``HH:MM:SS,mmm``."""
    ms = max(ms, 0)
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ms_to_ass_time(ms: int) -> str:
    """Convert milliseconds to ASS time format ``H:MM:SS.cc`` (centiseconds)."""
    ms = max(ms, 0)
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    cs = ms // 10
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
