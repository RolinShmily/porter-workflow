"""The narrow contract every ASR engine implements.

:class:`~porter.ports.AsrBackend` in ``porter.ports`` is the *pipeline's* view;
this module is the *backend author's* view, plus the shared error type and the
one helper every backend needs.

## Why a separate error type

The chain has to answer "should I try the next backend?" and it cannot do that
from an arbitrary exception. A backend that returns an empty list, one that
raises ``TimeoutError``, and one that parses a response into nothing all look the
same from outside, and the difference decides whether falling through is right.

So backends raise :class:`AsrBackendError` for *expected* failure — endpoint
down, quota exhausted, malformed response — and let genuinely unexpected
exceptions (a bug in our own code) propagate. The chain catches the first and
logs the second, which keeps a typo from being silently swallowed by a fallback
loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from porter.context import RunContext
from porter.models.subtitle import SubtitleItem

__all__ = [
    "CHUNK_SECONDS",
    "MAX_AUDIO_BYTES",
    "AsrBackend",
    "AsrBackendError",
    "AsrOutcome",
    "coerce_items",
    "parse_srt_items",
]

#: Upload chunk ceiling used by the cloud backends. Bilibili's endpoint rejects
#: anything larger; the others are happy with it.
MAX_AUDIO_BYTES = 100 * 1024 * 1024

#: Long audio is sliced rather than uploaded whole, because the free endpoints
#: time out on a 40-minute file and the failure looks like a network problem.
CHUNK_SECONDS = 480.0


class AsrBackendError(Exception):
    """An expected backend failure. The chain moves on to the next engine.

    Carries ``backend`` so the log line names the engine that failed rather than
    reporting a bare message, which matters when five backends have each failed
    for a different reason.

    ## The rule every backend must obey

    **Every expected failure becomes an ``AsrBackendError``.** The chain catches
    exactly ``AsrBackendError`` and ``PorterError``; anything else propagates and
    kills the process with a traceback.

    That is deliberate -- a chain-wide ``except Exception`` would swallow real
    bugs (a typo, a bad unpack) and report them as "backend unavailable", which is
    the hardest kind of failure to diagnose. But it puts the burden on the
    backend, and transport layers are where it gets missed: ``requests`` raises
    ``RequestException``, ``urllib`` raises ``http.client.HTTPException`` (a
    truncated chunked response is ``IncompleteRead``, which is neither an
    ``OSError`` nor a ``RequestError``), and a library may raise something else
    entirely. Map them all.
    """

    def __init__(self, backend: str, message: str, /, **details: object) -> None:
        super().__init__(message)
        self.backend = backend
        self.message = message
        self.details = details

    def __str__(self) -> str:
        base = f"[{self.backend}] {self.message}"
        if self.details:
            rendered = ", ".join(f"{key}={value!r}" for key, value in self.details.items())
            return f"{base} ({rendered})"
        return base


@dataclass(frozen=True)
class AsrOutcome:
    """What a backend produced. Cues plus an optional provenance note.

    ``used_asr`` is on the outcome rather than inferred by the chain because only
    the backend knows: a platform-subtitle backend did no recognition at all, and
    reporting that as "ASR was used" would mislabel the source for the operator.
    """

    items: list[SubtitleItem]
    used_asr: bool = True
    #: Free-text provenance for the log and the job report, e.g. "whisper-1".
    origin: str = ""


@runtime_checkable
class AsrBackend(Protocol):
    """One speech-to-text engine.

    ``available()`` must be cheap and must never raise — it is called for every
    backend on every job to decide the chain, so a backend that raises there
    breaks the chain's ability to skip it.
    """

    name: str

    #: Whether this engine's wire format has been verified against the live
    #: service.
    #:
    #: ``available()`` cannot answer this: it probes *local* facts (a key is set, a
    #: binary is on PATH), which say nothing about whether the remote endpoint
    #: still speaks the protocol this code was written for. ``bcut`` and
    #: ``google_web`` are reverse-engineered and answered with empty results on
    #: every probe made while building this port, so they report ``available`` and
    #: still cannot transcribe.
    #:
    #: ``False`` is not "broken" -- it is "nobody has checked", which is exactly
    #: what a caller needs to know before promising a user that a job will finish.
    endpoint_verified: bool

    def available(self, ctx: RunContext) -> bool:
        """Whether this engine can run right now. Never raises."""
        ...

    def transcribe(self, audio: Path, ctx: RunContext) -> AsrOutcome:
        """Recognise ``audio``.

        Raises:
            AsrBackendError: For expected failure. The chain tries the next
                backend.
        """
        ...


def coerce_items(items: list[SubtitleItem]) -> list[SubtitleItem]:
    """Renumber cues 1..n and drop empties.

    Every backend needs this and each v0.1 copy did it slightly differently — one
    kept the original indices, one renumbered, one left blank cues in. Renumbering
    is the correct choice: the generators write ``index`` into the SRT, and a
    duplicated or gapped index produces a file that some players refuse.
    """
    cleaned = [
        item
        for item in items
        if item.source_text.strip() and item.end_ms > item.start_ms
    ]
    for position, item in enumerate(cleaned, start=1):
        item.index = position
    return cleaned


def parse_srt_items(text: str) -> list[SubtitleItem]:
    """Parse SRT into cues. Returns ``[]`` for anything unparseable.

    Deliberately total: a backend that returns a malformed body should fall
    through the chain, not raise a parse error that reads like a bug in porter.
    """
    from porter.subtitles.srt import parse_srt

    try:
        return coerce_items(parse_srt(text))
    except (ValueError, IndexError):  # pragma: no cover - parse_srt is total
        return []
