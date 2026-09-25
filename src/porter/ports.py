"""Ports: the seams between the pipeline and its implementations.

The pipeline depends only on these protocols, never on a concrete extractor,
ASR backend, translator or renderer. That is what makes the engine testable
without network or ffmpeg, and what lets ``videocaptioner`` (GPL-3.0) live
behind a process boundary as just another optional implementation.

One port per pipeline phase. ``Transcriber`` and ``Translator`` are *chains*:
they own the fallback ordering and expose an ``available()`` probe so the
assembly step can drop unusable backends. The individual engines behind them
implement the narrower protocols in ``porter.asr.base`` and
``porter.translate.base``.

Implementations are selected by ``Pipeline.default()`` at assembly time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from porter.context import RunContext
from porter.models.materials import RawMaterials
from porter.models.metadata import VideoMetadata
from porter.models.request import BurnMode, BurnResult
from porter.models.subtitle import SubtitleItem, SubtitleSet, TranscriptSentence

__all__ = [
    "AsrBackend",
    "Downloader",
    "LocalPreparer",
    "Renderer",
    "Transcriber",
    "TranscriptRefiner",
    "Translator",
]


@runtime_checkable
class Downloader(Protocol):
    """Phase PREPARE for a **URL**: fetch and standardise the raw assets."""

    name: str

    def can_handle(self, url: str) -> bool:
        """Whether this downloader claims ``url``."""
        ...

    def probe(self, url: str, ctx: RunContext) -> VideoMetadata:
        """Cheap metadata-only inspection. Must not download media."""
        ...

    def fetch(self, url: str, ctx: RunContext) -> RawMaterials:
        """Download media, extract audio, fetch cover and platform subtitles."""
        ...


@runtime_checkable
class LocalPreparer(Protocol):
    """Phase PREPARE for a **file already on disk**.

    Separate from :class:`Downloader` rather than folded into it, because the two
    take different things. ``Downloader`` is keyed by URL -- ``can_handle``
    matches a pattern and ``probe`` parses one -- and a filesystem path is not a
    URL: ``can_handle("/home/me/video.mp4")`` has no meaning, and ``probe`` would
    have to re-derive "is this a file?" before it could do anything.

    Widening ``Downloader`` to take a generic "source" would erase the
    distinction for every platform extractor and the registry, which is a lot of
    churn to avoid one extra field on :class:`~porter.pipeline.Pipeline`.

    There is deliberately no ``can_handle``: a :class:`~porter.models.request.JobRequest`
    already says which kind of source it carries, so there is nothing to guess.
    """

    name: str

    def prepare(self, path: Path, ctx: RunContext) -> RawMaterials:
        """Standardise ``path`` into ``raw/`` and return the raw materials."""
        ...


@runtime_checkable
class AsrBackend(Protocol):
    """A single speech-to-text engine.

    Backends are cheap to construct and must never raise from ``available()``.
    """

    name: str

    def available(self, ctx: RunContext) -> bool:
        """Whether this engine can run right now (binary present, key set, ...)."""
        ...

    def transcribe(self, audio: Path, ctx: RunContext) -> list[SubtitleItem]:
        """Return timed cues, or raise to let the chain try the next backend."""
        ...


@runtime_checkable
class Transcriber(Protocol):
    """Phase TRANSCRIBE: produce source-language subtitles.

    Prefers a platform-provided subtitle track when one exists and only falls
    back to ASR otherwise — the "smart fallback" behaviour of v0.1, now with a
    name that says so.
    """

    name: str

    def available(self, ctx: RunContext) -> bool:
        """Whether any path to source subtitles is available."""
        ...

    def transcribe(self, raw: RawMaterials, ctx: RunContext) -> SubtitleSet:
        """Return source cues plus the on-disk subtitle files."""
        ...


@runtime_checkable
class TranscriptRefiner(Protocol):
    """Refine transcript sentences (fix ASR typos, punctuation, homophones)."""

    name: str

    def available(self, ctx: RunContext) -> bool:
        """Whether this refiner can run right now."""
        ...

    def refine(
        self,
        sentences: list[TranscriptSentence],
        ctx: RunContext,
    ) -> list[TranscriptSentence]:
        """Refine each sentence's text and populate ``refined_en_text``."""
        ...


@runtime_checkable
class Translator(Protocol):
    """Phase TRANSLATE: turn source cues into bilingual cues.

    Owns the fallback ordering (LLM → Bing → Google → MyMemory → CLI) and is
    responsible for the CJK self-check that rejects a fake "translation".
    """

    name: str

    def available(self, ctx: RunContext) -> bool:
        """Whether any translation backend is usable."""
        ...

    def translate(
        self,
        subtitles: SubtitleSet,
        target_lang: str,
        ctx: RunContext,
    ) -> SubtitleSet:
        """Return ``subtitles`` with target text populated and ASS/SRT written."""
        ...


@runtime_checkable
class Renderer(Protocol):
    """Phase BURN: hard-sub the requested variants into the master video."""

    name: str

    def render(
        self,
        raw: RawMaterials,
        subtitles: SubtitleSet,
        mode: BurnMode,
        ctx: RunContext,
    ) -> BurnResult:
        """Burn each requested variant and return the produced paths."""
        ...
