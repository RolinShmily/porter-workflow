"""Reading the platform's own subtitle track.

Not an :class:`~porter.asr.base.AsrBackend`. The distinction is not cosmetic:

* A backend is an *engine* with an ``available()`` probe and a fallback position.
* This is a **file that PREPARE already fetched**, or nothing. There is no
  engine to probe and no ordering question — the track either exists or it does
  not, and :class:`~porter.asr.chain.AsrChain` checks for it before touching any
  engine.

Modelling it as a backend forced an ``available(ctx)`` that could not see the
materials it needed (the protocol only receives a context), so the previous draft
had to smuggle the decision into a ``isinstance`` branch inside the chain. That
is the kind of thing that reads as an engine but behaves as a special case.

Why the track wins when it exists: it is the author's text. Correct spelling,
correct punctuation, correct proper nouns, and free. ASR on the same audio yields
mangled names and no punctuation. Preferring it is a quality decision.
"""

from __future__ import annotations

from pathlib import Path

from porter.errors import PorterError
from porter.logging import get_logger
from porter.models.subtitle import SubtitleItem
from porter.subtitles.srt import parse_srt, vtt_to_srt

__all__ = ["SUPPORTED_SUBTITLE_SUFFIXES", "load_platform_subtitles", "load_supplied_subtitles"]

_logger = get_logger(__name__)

#: Formats ``--subtitle-file`` accepts. SRT and WebVTT are what subtitle tools and
#: platforms actually hand out, and WebVTT is a small transform away from SRT.
SUPPORTED_SUBTITLE_SUFFIXES = (".srt", ".vtt")


def load_platform_subtitles(path: Path | None) -> list[SubtitleItem]:
    """Parse a fetched platform track into cues. ``[]`` when there is none.

    PREPARE converts WebVTT to SRT before writing ``raw/subtitle.srt`` (see
    :mod:`porter.platforms.base`), so SRT is the only format to handle here.

    Total by design: an unreadable or empty track means "fall back to ASR", which
    is a normal outcome and not an error. Raising would turn a missing caption
    file into a failed job.
    """
    if path is None:
        return []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _logger.warning("platform subtitle %s could not be read: %s", path, exc)
        return []

    if not text.strip():
        return []

    items = parse_srt(text)
    if not items:
        # Non-empty but unparseable: a truncated download or a track in a format
        # the platform serves that we did not expect. Worth a warning, because
        # the job silently becomes more expensive (ASR) and less accurate.
        _logger.warning(
            "platform subtitle %s produced no cues (%d bytes); falling back to ASR",
            path,
            len(text),
        )
    return items


def load_supplied_subtitles(path: Path) -> list[SubtitleItem]:
    """Parse a source track the user named explicitly. Raises when unusable.

    The counterpart to :func:`load_platform_subtitles`, and deliberately *not*
    total. That one returns ``[]`` on anything unreadable, because "no platform
    track" is a normal outcome that means "fall back to ASR". Here the user asked
    for this exact file: silently falling through to speech recognition would hide
    a typo behind minutes of work and a worse transcript, and silently producing
    no subtitles at all would be worse still. So every failure is an error, and
    each one names what to do about it.

    This is also the escape hatch §13.29 left open. A ``.srt`` sitting beside a
    local video is still **not** picked up automatically -- it could be the source
    or the translation, and guessing wrong either skips ASR for no reason or
    overwrites the user's file. Naming the file removes the ambiguity instead of
    resolving it by guesswork.
    """
    if not path.is_file():
        raise PorterError(
            f"subtitle file not found: {path}",
            path=str(path),
            hint="--subtitle-file must point at an existing .srt or .vtt file",
        )

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUBTITLE_SUFFIXES:
        raise PorterError(
            f"unsupported subtitle format {suffix!r}: {path}",
            path=str(path),
            supported=list(SUPPORTED_SUBTITLE_SUFFIXES),
        )

    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise PorterError(
            f"subtitle file could not be read: {path}", path=str(path), reason=str(exc)
        ) from exc

    items = parse_srt(vtt_to_srt(text) if suffix == ".vtt" else text)
    if not items:
        raise PorterError(
            f"subtitle file contains no cues: {path}",
            path=str(path),
            bytes=len(text),
            hint="an empty or truncated subtitle file would produce a job with no text",
        )

    _logger.info("using the supplied subtitle file %s (%d cues)", path, len(items))
    return items
