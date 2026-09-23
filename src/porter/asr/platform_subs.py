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

from porter.logging import get_logger
from porter.models.subtitle import SubtitleItem
from porter.subtitles.srt import parse_srt

__all__ = ["load_platform_subtitles"]

_logger = get_logger(__name__)


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
