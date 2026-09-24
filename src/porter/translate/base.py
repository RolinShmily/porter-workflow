"""The narrow contract every translation engine implements.

Mirrors :mod:`porter.asr.base` deliberately: same error discipline, same
"available() is a cheap probe that never raises" rule, same reason for a dedicated
error type.

## The unit of work is the string batch

``translate_texts`` takes a list of independent strings and returns a positionally
aligned list. All five HTTP backends are per-string APIs, so batching them into one
request is where the parsing complexity lives and where this contract earns its
keep.

**Known gap.** v0.1 also had a sentence-level path: ``reconstruct_sentences_from_fragments``
rebuilt whole sentences from fragmented ASR cues so the LLM would translate with
context, then ``fragment_indices`` mapped the result back onto cues. That is a
quality feature, not a structural one, and it is deliberately not part of this
port — it needs the sentence reconstruction in :mod:`porter.subtitles.phrasing`
first. Translating cue-by-cue is measurably worse for Chinese word order, so this
is a real regression against v0.1 that should be closed before release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from porter.context import RunContext

__all__ = [
    "MAX_ATTEMPTS",
    "MAX_TEXTS_PER_REQUEST",
    "TranslationBackend",
    "TranslationBackendError",
    "TranslationOutcome",
    "retry_after_seconds",
    "retry_delay",
    "retryable_status",
    "wait_for_retry",
]

#: Google's key-free endpoint starts returning truncated results above this, and
#: Bing rate-limits aggressively. Both are per-request ceilings, not per-job.
MAX_TEXTS_PER_REQUEST = 15

#: Seconds multiplied by the attempt number to get the backoff.
#:
#: Module-level so tests can zero it instead of sleeping for real -- a retry test
#: that actually waits 4.5 s is a test nobody runs.
BACKOFF_BASE_SECONDS = 1.5

#: Longest single wait, whatever the server's ``Retry-After`` asks for.
#:
#: A ``Retry-After: 3600`` would otherwise hang the job for an hour, and the
#: chain has four other backends that can finish the work now.
MAX_DELAY_SECONDS = 8.0

#: How many times one request is attempted before the backend gives up.
#:
#: Both key-free endpoints refused on a measured run -- Google with HTTP 429 on
#: both of its clients, Bing with a body it would not translate. A single attempt
#: turns a temporary throttle into "this backend is broken", which is exactly what
#: the operator then reads in the job report.
MAX_ATTEMPTS = 3


def retryable_status(status: int) -> bool:
    """Whether an HTTP status is worth another attempt.

    Only 429 and 5xx. A 403 or a 404 is a statement about the request itself, and
    repeating it just spends the retry budget on an answer that will not change.
    """
    return status == 429 or 500 <= status < 600


def retry_delay(attempt: int, *, retry_after: float | None = None) -> float:
    """Seconds to wait before attempt ``attempt + 1``.

    Linear in the attempt number, as in :mod:`porter.platforms.inspector`: the
    point is to clear a short throttle, not to outlast a long ban.

    A server's ``Retry-After`` **replaces** the computed backoff -- it is the
    server telling us when it will accept us again, which beats our guess -- but
    it is still capped. ``Retry-After: 3600`` would otherwise hang the job for an
    hour while four other backends could finish the work right now.
    """
    if retry_after is not None:
        return max(0.0, min(retry_after, MAX_DELAY_SECONDS))
    return min(BACKOFF_BASE_SECONDS * attempt, MAX_DELAY_SECONDS)


def wait_for_retry(ctx: RunContext, seconds: float) -> None:
    """Wait between attempts without blocking cancellation.

    ``ctx.cancel.wait`` rather than ``time.sleep``: a cancelled job must stop
    immediately instead of finishing its nap first.
    """
    if seconds <= 0:
        return
    if ctx.cancel.wait(seconds):
        ctx.check_cancelled()


def retry_after_seconds(response: Any) -> float | None:
    """The server's ``Retry-After`` hint, when it is a plain number of seconds.

    The HTTP-date form is ignored rather than parsed: a wrong guess at the clock
    is worse than falling back to the backoff.
    """
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class TranslationBackendError(Exception):
    """An expected backend failure. The chain moves on to the next engine."""

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
class TranslationOutcome:
    """What a backend produced, plus provenance.

    ``texts`` is positional against the input: ``out.texts[i]`` is the translation
    of ``inputs[i]``. Every backend must preserve that, including for empty or
    untranslatable inputs, where it returns the input unchanged rather than
    dropping the element. Dropping is the tempting shortcut and it silently
    misaligns every subsequent subtitle.

    The chain verifies this invariant rather than trusting it, because an
    off-by-one here produces subtitles that are all plausibly translated and all
    matched to the wrong moment.

    ``sources`` is the same invariant applied to the *input* side: an optional,
    positionally-aligned list of corrected source strings. Only a backend that
    reads the whole sentence can produce one -- the LLM is asked to fix ASR and
    punctuation errors as part of translating, and v0.1 used its corrected English
    in the bilingual track. A backend that cannot correct the source leaves this
    ``None``, and the chain keeps the original text. ``None`` and an empty list
    are different: ``None`` means "no opinion", ``[]`` would be a length
    mismatch and is rejected.
    """

    texts: list[str]
    origin: str = ""
    sources: list[str] | None = None


@runtime_checkable
class TranslationBackend(Protocol):
    """One translation engine.

    ``available()`` must be cheap and must never raise — it is called for every
    backend on every job.
    """

    name: str

    #: Whether this engine's wire format has been verified against the live
    #: service. See :attr:`porter.asr.base.AsrBackend.endpoint_verified`.
    #:
    #: ``llm`` and ``mymemory`` speak documented APIs; ``bing`` and ``google`` are
    #: reverse-engineered. Both work as of this writing, which is why the flag is
    #: about verification and not about function.
    endpoint_verified: bool

    def available(self, ctx: RunContext) -> bool:
        """Whether this engine can run right now. Never raises."""
        ...

    def translate_texts(
        self,
        texts: list[str],
        target_lang: str,
        ctx: RunContext,
    ) -> TranslationOutcome:
        """Translate independent strings, preserving order and length.

        Raises:
            TranslationBackendError: For expected failure.
        """
        ...
