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
from typing import Protocol, runtime_checkable

from porter.context import RunContext
from porter.models.subtitle import SubtitleItem

__all__ = [
    "MAX_TEXTS_PER_REQUEST",
    "TranslationBackend",
    "TranslationBackendError",
    "TranslationOutcome",
]

#: Google's key-free endpoint starts returning truncated results above this, and
#: Bing rate-limits aggressively. Both are per-request ceilings, not per-job.
MAX_TEXTS_PER_REQUEST = 15


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


def apply_texts_to_items(items: list[SubtitleItem], texts: list[str]) -> None:
    """Write translated strings onto cues, positionally.

    ``strict=True`` because a length mismatch here means a bug in the chain, not a
    normal outcome. Silently zipping to the shorter list would produce a partially
    translated file, which looks like a translation quality problem and hides the
    real cause until someone counts the cues.
    """
    for item, text in zip(items, texts, strict=True):
        item.target_text = text.strip()
