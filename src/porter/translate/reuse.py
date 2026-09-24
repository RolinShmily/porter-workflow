"""Caching the translation, so a styling change does not cost a network round-trip.

TRANSLATE was the one phase with no reuse, and the reason recorded for that was
sound as far as it went: its output includes the four rendered subtitle files,
which depend on the subtitle style, so reusing *those* would pin the styling. But
that conflates two things with very different costs:

* **The translation** — one network call per sentence, or a paid LLM call. The
  expensive part, and the part a re-run is trying to avoid.
* **The rendering** — SRT and ASS generation from text already in memory.
  Milliseconds, local, and the only part the style touches.

So this caches the **text** and never the files. A re-run over unchanged cues
skips the backends and still re-renders, which makes editing ``style.*`` and
re-running do what a person expects: a new look, no new translation.

## Why a content hash rather than an mtime

The TRANSCRIBE cache compares mtimes, which is a proxy for "did the input change".
A proxy is needed there because the input is an audio file on disk. Here the input
is the sentence list, already in memory, so the fingerprint can be exact: a hash
of the text that would be sent to the translator. That also sidesteps filesystem
timestamp granularity — measured on Windows, two writes a moment apart can share
an mtime exactly, which is enough to make an mtime comparison wrong.

## What invalidates it

The fingerprint covers everything that determines the translated text: the source
sentences, the target language, and the identity of the engine that would serve
them (which backend, which model, which endpoint). Change the model, point
``llm.api_base`` elsewhere, or edit the cues, and it misses. Change ``style.*``
and it hits — which is the entire point.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard

from porter.logging import get_logger
from porter.translate.base import TranslationOutcome

if TYPE_CHECKING:
    from porter.context import RunContext
    from porter.models.subtitle import TranscriptSentence

__all__ = ["CACHE_NAME", "fingerprint", "load", "save"]

_logger = get_logger(__name__)

#: Sidecar in ``cooked/``, alongside ``.transcribe.json``. The leading dot keeps
#: it visibly internal in a directory whose other contents are the deliverables.
CACHE_NAME = ".translate.json"

#: Bumped when the meaning of the cached payload changes -- a new field, or a
#: change to how sentences are cut back into cues. Without it, old code's cache
#: would be read by new code and produce subtitles from a superseded algorithm.
_CACHE_VERSION = 1


def fingerprint(
    sentences: list[TranscriptSentence], target_lang: str, ctx: RunContext
) -> str:
    """A hash of everything that determines the translated text.

    ``effective_llm_model`` rather than ``ctx.config.llm.model``, so the value
    hashed is the same one the LLM backend will actually use -- ``--llm-model``
    overrides the config, and hashing the config alone would treat two different
    models as one.
    """
    from porter.translate.llm import effective_llm_model

    payload = {
        "v": _CACHE_VERSION,
        "target_lang": target_lang,
        # Which engine would run. Most backends have no settings worth hashing,
        # but the LLM's do: a different model or endpoint is a different
        # translation, and reusing across that change would be silently wrong.
        "translator": ctx.options.translator or "",
        "llm_model": effective_llm_model(ctx),
        "llm_base": ctx.config.llm.api_base or "",
        "inputs": [sentence.en_text for sentence in sentences],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load(cooked_dir: Path, expected: str, count: int) -> TranslationOutcome | None:
    """The cached translation, or ``None`` when there is none or it does not match.

    Every failure mode is a miss rather than an error. This is an optimisation: a
    missing, unreadable, corrupt or simply stale sidecar must not fail a job that
    could just translate again.

    ``count`` is the number of sentences the cache is being asked to cover, and
    the length check is not optional. ``texts`` is positionally aligned against
    the sentences, so a truncated list is worse than no cache at all: the cues it
    does cover look perfectly translated and the tail is silently left in the
    source language. A fingerprint cannot catch that on its own -- the *inputs* are
    unchanged; it is the cached answer that is short.
    """
    path = cooked_dir / CACHE_NAME
    if not path.is_file():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _logger.warning("could not read %s (%s); translating again", path, exc)
        return None

    if not isinstance(payload, dict) or payload.get("fingerprint") != expected:
        return None

    texts = payload.get("texts")
    if not _is_str_list(texts) or len(texts) != count:
        return None

    sources = payload.get("sources")
    # ``None`` means "no opinion" and is valid; anything else must be a list of
    # strings of the same length, for the same positional reason. A wrong-shaped
    # ``sources`` is a miss rather than ignored, because the bilingual track would
    # otherwise silently show the uncorrected English the translation was not made
    # from.
    if sources is not None and (not _is_str_list(sources) or len(sources) != count):
        return None
    return TranslationOutcome(
        texts=texts,
        origin=str(payload.get("origin") or "cache"),
        sources=sources,
    )


def save(cooked_dir: Path, fingerprint_value: str, outcome: TranslationOutcome) -> None:
    """Record a translation so the next run can skip the backends.

    Written to a temporary file and renamed into place. A crash mid-write must not
    leave something that parses as a valid but truncated cache: the texts are
    positionally aligned against the sentences, so a short list would produce
    subtitles with the tail silently untranslated.
    """
    path = cooked_dir / CACHE_NAME
    payload = {
        "fingerprint": fingerprint_value,
        "origin": outcome.origin,
        "texts": outcome.texts,
        "sources": outcome.sources,
    }
    temp = path.with_name(f".tmp_{path.name}")
    try:
        cooked_dir.mkdir(parents=True, exist_ok=True)
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(path)
    except OSError as exc:
        _logger.warning(
            "could not write %s (%s); the next run will translate again", path, exc
        )
        # A half-written temp file is harmless -- the next attempt overwrites it
        # -- but leaving it behind costs nothing to avoid.
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    """Whether ``value`` is a list of strings.

    A ``TypeGuard`` so the caller gets a narrowed ``list[str]`` from the same
    check that decides whether the cache is usable, instead of validating and
    then re-asserting the type with a cast.
    """
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
