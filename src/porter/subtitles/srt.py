"""Subtitle format conversion and language selection.

Pure functions, no I/O, no logging side effects beyond warnings.

Why this module exists
----------------------
``v0.1`` reimplemented these three helpers inside every extractor that needed
them. Diffing the copies on ``main`` showed:

* ``_convert_vtt_to_srt``            -- 3 copies, identical to the byte
* ``_select_source_subtitle_lang``   -- 3 copies, identical to the byte
* ``_select_chinese_subtitle_lang``  -- 3 copies, identical to the byte

Three byte-identical copies of a parser is how a format bug gets fixed in one
place and stays broken in two. They are collapsed here into the single
implementation, and the per-platform extractors delegate to it.

Behavioral compatibility
------------------------
The algorithms are ports, not rewrites. ``tests/regression/`` holds the v0.1
assertions verbatim, so any accidental change in output fails the suite.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from porter.logging import get_logger
from porter.models.subtitle import SubtitleItem
from porter.subtitles.phrasing import normalize_subtitle_items
from porter.utils.text import is_cjk
from porter.utils.time import ms_to_srt_time, srt_time_to_ms

__all__ = [
    "CHINESE_LANG_PRIORITY",
    "SOURCE_LANG_PRIORITY",
    "bilibili_json_to_srt",
    "generate_bilingual_srt",
    "generate_zh_srt",
    "parse_srt",
    "select_chinese_lang",
    "select_source_lang",
    "vtt_to_srt",
]

_logger = get_logger(__name__)

#: Language preference for the *source* (original speech) track, best first.
#: English leads because the pipeline's normal direction is en -> zh.
SOURCE_LANG_PRIORITY: tuple[str, ...] = (
    "en",
    "en-US",
    "en-GB",
    "en-CA",
    "zh-Hans",
    "zh-CN",
    "zh-Hans-CN",
    "zh",
    "zh-Hant",
    "zh-TW",
    "zh-HK",
    "ja",
    "ko",
    "es",
    "fr",
    "de",
    "ru",
)

#: Language preference for an existing Chinese track, best first. Simplified
#: before traditional, because that is what the default target language is.
CHINESE_LANG_PRIORITY: tuple[str, ...] = (
    "zh-Hans",
    "zh-CN",
    "zh-Hans-CN",
    "zh",
    "zh-Hant",
    "zh-TW",
    "zh-HK",
)

#: Suffixes yt-dlp uses to mark a track as matching the original audio.
_ORIGINAL_SUFFIXES = ("-orig", "-original")

#: Live-caption pseudo-tracks; never usable as a subtitle source.
_LIVE_PREFIX = "live_"

_VTT_TIMESTAMP = re.compile(
    r"(\d{2}:)?(\d{2}):(\d{2})[\.,](\d{3})\s*-->\s*(\d{2}:)?(\d{2}):(\d{2})[\.,](\d{3})"
)
_VTT_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def vtt_to_srt(vtt_content: str) -> str:
    """Convert WebVTT to SubRip (SRT).

    Multi-line cues are collapsed onto a single line, inline markup such as
    ``<c>`` / ``<v Name>`` is stripped, and timestamps are normalised to
    ``HH:MM:SS,mmm`` (VTT allows the hour field to be absent).

    Args:
        vtt_content: Full WebVTT document text.

    Returns:
        An SRT document. Empty string if no cues were found.
    """
    lines = vtt_content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    srt_blocks: list[str] = []
    block_num = 1

    i = 0
    while i < len(lines):
        match = _VTT_TIMESTAMP.search(lines[i].strip())
        if not match:
            i += 1
            continue

        parts = match.groups()
        start_h = parts[0][:-1] if parts[0] else "00"
        end_h = parts[4][:-1] if parts[4] else "00"
        start_time = f"{int(start_h):02d}:{parts[1]}:{parts[2]},{parts[3]}"
        end_time = f"{int(end_h):02d}:{parts[5]}:{parts[6]},{parts[7]}"

        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            clean = _VTT_TAG.sub("", lines[i].strip())
            if clean:
                text_lines.append(clean)
            i += 1

        if text_lines:
            single_line = _WHITESPACE.sub(" ", " ".join(text_lines)).strip()
            if single_line:
                srt_blocks.append(f"{block_num}\n{start_time} --> {end_time}\n{single_line}")
                block_num += 1

    return "\n\n".join(srt_blocks) + ("\n" if srt_blocks else "")


def bilibili_json_to_srt(json_content: str) -> str:
    """Convert a Bilibili CC subtitle JSON body to SubRip (SRT).

    Bilibili returns cue times as seconds (float) rather than timestamps. An
    end time is forced to be at least 100 ms after the start so that zero-length
    cues remain visible.

    Malformed input yields an empty string rather than raising: a broken CC
    track should degrade to "no subtitles available" and let the ASR fallback
    chain run, not abort the whole job. The failure is logged.
    """
    try:
        data = json.loads(json_content)
    except (json.JSONDecodeError, TypeError) as exc:
        _logger.warning("discarding malformed Bilibili CC payload: %s", exc)
        return ""

    if not isinstance(data, dict):
        _logger.warning("Bilibili CC payload is not a JSON object; ignoring")
        return ""

    body = data.get("body") or []
    if not isinstance(body, list):
        return ""

    srt_blocks: list[str] = []
    block_idx = 1
    for item in body:
        if not isinstance(item, dict):
            continue
        start_val = item.get("from")
        end_val = item.get("to")
        content = str(item.get("content") or "").strip()
        if start_val is None or end_val is None or not content:
            continue

        start_ms = max(0, int(float(start_val) * 1000))
        end_ms = max(start_ms + 100, int(float(end_val) * 1000))
        srt_blocks.append(
            f"{block_idx}\n{ms_to_srt_time(start_ms)} --> {ms_to_srt_time(end_ms)}\n{content}"
        )
        block_idx += 1

    return "\n\n".join(srt_blocks) + ("\n" if srt_blocks else "")


def _first_present(subtitles: Mapping[str, Any], candidates: Sequence[str]) -> str | None:
    """Return the first candidate that maps to a truthy entry, else None."""
    for lang in candidates:
        if subtitles.get(lang):
            return lang
    return None


def select_source_lang(
    subtitles: Mapping[str, Any],
    *,
    is_auto: bool = False,
) -> str | None:
    """Choose the best track for the original spoken language.

    Selection order:

    1. A track yt-dlp marked as the original audio (``*-orig`` / ``*-original``).
    2. The first language in :data:`SOURCE_LANG_PRIORITY` that is present.
    3. For human-authored tracks only (``is_auto=False``), the first usable track
       in document order. Auto-generated tracks are excluded from this fallback
       because an arbitrary machine transcription is a poor source.

    Returns:
        A language code, or None when nothing suitable exists.
    """
    if not subtitles:
        return None

    for key in subtitles:
        if key.endswith(_ORIGINAL_SUFFIXES):
            return key

    preferred = _first_present(subtitles, SOURCE_LANG_PRIORITY)
    if preferred is not None:
        return preferred

    if not is_auto:
        for lang, formats in subtitles.items():
            if formats and not lang.startswith(_LIVE_PREFIX):
                return lang

    return None


def select_chinese_lang(subtitles: Mapping[str, Any]) -> str | None:
    """Choose the best existing Chinese track, or None if there is none.

    Used to reuse a platform-provided Chinese subtitle instead of paying for
    translation.
    """
    if not subtitles:
        return None
    return _first_present(subtitles, CHINESE_LANG_PRIORITY)


def parse_srt(srt_content: str) -> list[SubtitleItem]:
    """Parse SRT text into cues.

    Handles the two shapes that actually occur: monolingual cues, and the
    bilingual ones ``generate_bilingual_srt`` writes (source and target on
    separate lines). For a two-line cue it decides which line is Chinese by
    looking for CJK characters rather than by trusting the order, because both
    orders are produced by different tools.

    Total, not strict: a malformed block is skipped rather than raising. This
    parses the output of remote services, and one bad cue should not discard the
    other two hundred.
    """
    if not srt_content or not srt_content.strip():
        return []

    normalized = srt_content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n\s*\n", normalized)
    time_pattern = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{3})"
    )

    items: list[SubtitleItem] = []
    index = 1

    for block in blocks:
        lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
        if not lines:
            continue

        timestamp_at = -1
        match: re.Match[str] | None = None
        for position, line in enumerate(lines):
            match = time_pattern.search(line)
            if match:
                timestamp_at = position
                break

        if match is None or timestamp_at == -1:
            continue

        start_ms = srt_time_to_ms(match.group(1))
        end_ms = srt_time_to_ms(match.group(2))
        text_lines = lines[timestamp_at + 1 :]

        if not text_lines:
            continue

        if len(text_lines) == 1:
            items.append(
                SubtitleItem(
                    index=index,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    source_text=text_lines[0].strip(),
                    target_text="",
                )
            )
        else:
            first_is_cjk = is_cjk(text_lines[0])
            second_is_cjk = is_cjk(text_lines[1])
            if first_is_cjk and not second_is_cjk:
                items.append(
                    SubtitleItem(
                        index=index,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        source_text=" ".join(line.strip() for line in text_lines[1:]),
                        target_text=text_lines[0].strip(),
                    )
                )
            elif not first_is_cjk and second_is_cjk:
                items.append(
                    SubtitleItem(
                        index=index,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        source_text=text_lines[0].strip(),
                        target_text=" ".join(line.strip() for line in text_lines[1:]),
                    )
                )
            else:
                # Monolingual wrapped text. Joined without a space for CJK,
                # because a space between Chinese characters is visible.
                separator = "" if first_is_cjk else " "
                items.append(
                    SubtitleItem(
                        index=index,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        source_text=separator.join(
                            line.strip() for line in text_lines if line.strip()
                        ),
                        target_text="",
                    )
                )

        index += 1

    return normalize_subtitle_items(items)


def generate_bilingual_srt(items: list[SubtitleItem]) -> str:
    """Render bilingual SRT: target language first, source second.

    Target first because the viewer's language is the one they are reading; the
    source line is the reference underneath.
    """
    blocks: list[str] = []
    for item in items:
        if item.target_text and item.source_text and item.target_text != item.source_text:
            text = f"{item.target_text}\n{item.source_text}"
        elif item.target_text:
            text = item.target_text
        else:
            text = item.source_text

        blocks.append(f"{item.index}\n{item.start_srt} --> {item.end_srt}\n{text}")

    return "\n\n".join(blocks) + "\n" if blocks else ""


def generate_zh_srt(items: list[SubtitleItem]) -> str:
    """Render single-language SRT, falling back to the source when untranslated."""
    blocks: list[str] = []
    for item in items:
        text = item.target_text if item.target_text else item.source_text
        blocks.append(f"{item.index}\n{item.start_srt} --> {item.end_srt}\n{text}")

    return "\n\n".join(blocks) + "\n" if blocks else ""
