"""The transcription chain: platform subtitles when they exist, ASR otherwise.

Implements :class:`~porter.ports.Transcriber` by taking the platform's own track
if PREPARE fetched one, and otherwise walking an ordered list of
:class:`~porter.asr.base.AsrBackend` values until one produces cues.

Why the platform track wins: it is the author's text. Correct spelling, correct
punctuation, correct proper nouns, and free. ASR on the same audio yields mangled
names and no punctuation. Preferring it is a quality decision, not a speed one —
and v0.1 buried it inside a 329-line function that also held the fallback order,
the config lookup and the file writing.

## The fallback rules, and why the obvious ones are wrong

* **An empty result from an available backend is a failure, not a success.**
  Bcut answers HTTP 200 with zero utterances when its quota is exhausted. A chain
  that treats "returned no cues" as done writes an empty subtitle file and reports
  the job DONE.
* **``available()`` is advisory.** Backends probe cheap local facts (a key is set,
  a binary is on PATH), which cannot predict a remote 429. ``available()`` is used
  for ordering and reporting, and the chain still tolerates a backend that fails
  after claiming availability.
* **Cancellation is not a backend failure.**
  :class:`~porter.errors.JobCancelled` must escape the ``except`` that advances to
  the next backend, or Ctrl-C costs the user five more network round-trips. The
  P2 suite caught exactly this bug in the platform fetcher, so it is guarded here
  explicitly rather than left to fall out of the structure.
"""

from __future__ import annotations

import time
from pathlib import Path

from porter.asr.base import AsrBackend, AsrBackendError, AsrOutcome, coerce_items
from porter.asr.platform_subs import load_platform_subtitles
from porter.context import RunContext
from porter.errors import JobCancelled, PorterError
from porter.events import Phase
from porter.logging import get_logger
from porter.models.materials import RawMaterials
from porter.models.subtitle import SubtitleItem, SubtitleSet
from porter.subtitles.phrasing import (
    align_bilingual_items,
    has_chinese_translation,
    normalize_subtitle_items,
)
from porter.subtitles.srt import generate_zh_srt

__all__ = ["AsrChain"]

_logger = get_logger(__name__)

#: Output names inside ``cooked/``. The ``.ass`` pair is written by the translate
#: phase, because ASS carries the bilingual layout and needs target text. The
#: paths are still put on the model by TRANSCRIBE so TRANSLATE can run alone.
SOURCE_SRT_NAME = "subtitle.srt"
BILINGUAL_SRT_NAME = "subtitle_bilingual.srt"
ZH_SRT_NAME = "subtitle_zh.srt"
BILINGUAL_ASS_NAME = "subtitle_bilingual.ass"
ZH_ASS_NAME = "subtitle_zh.ass"
TRANSCRIPT_JSON_NAME = "transcript.json"
TRANSCRIPT_TXT_NAME = "transcript.txt"


class AsrChain:
    """Platform track, then ASR backends in order.

    ``name`` is ``"chain"`` so log lines and MCP results can name the transcriber
    without enumerating backends; per-attempt detail goes to the log records.
    """

    name = "chain"

    def __init__(self, backends: list[AsrBackend] | None = None) -> None:
        self.backends: list[AsrBackend] = list(backends or [])

    def add(self, backend: AsrBackend) -> AsrChain:
        """Append a backend. Returns self, so assembly is one expression."""
        self.backends.append(backend)
        return self

    # -- Transcriber --------------------------------------------------------

    def available(self, ctx: RunContext) -> bool:
        """Whether any ASR engine is usable.

        Says nothing about the platform track: that does not exist until PREPARE
        has run, and this is called at assembly time. A ``False`` here means
        source subtitles will only be possible if the platform provides a track.
        """
        return any(_probe(backend, ctx) for backend in self.backends)

    def availability(self, ctx: RunContext) -> list[tuple[str, bool]]:
        """Each backend's name and whether its probe succeeds, in chain order.

        Exists so a caller can *report* the chain without re-deriving it. The
        planned execution path (``docs/REFACTOR_PLAN.md`` §8.1's
        ``porter_plan``) has to say which engine would run; reimplementing the
        ordering and the availability rule there would let the two drift, and a
        plan that describes a pipeline nobody runs is worse than no plan.

        Uses :func:`_probe`, so a backend that breaks the never-raises contract
        is reported unavailable rather than taking the whole chain down.
        """
        return [(backend.name, _probe(backend, ctx)) for backend in self.backends]

    def transcribe(self, raw: RawMaterials, ctx: RunContext) -> SubtitleSet:
        """Produce source cues, preferring the platform's track over ASR."""
        ctx.check_cancelled()
        ctx.progress(Phase.TRANSCRIBE, 0.0, "preparing source subtitles")

        items = load_platform_subtitles(raw.subtitle_src)
        if items:
            ctx.logger.info("using the platform's own subtitle track (%d cues)", len(items))
            ctx.progress(Phase.TRANSCRIBE, 1.0, "using the platform subtitle track")

            # A platform Chinese track is aligned here rather than in TRANSLATE,
            # because it is *source material* that PREPARE fetched, not something
            # an engine produced. TRANSLATE then sees cues that already carry
            # target text and skips every backend — free and exact, where ASR plus
            # machine translation is neither.
            if _align_platform_chinese(raw, items, ctx):
                ctx.progress(Phase.TRANSCRIBE, 1.0, "aligned the platform Chinese track")

            # used_asr=False: nothing was recognised. Reporting this as ASR would
            # mislabel the provenance, and "did this job pay for Whisper?" is a
            # question the operator asks.
            return self._write(
                raw, items, AsrOutcome(items=items, used_asr=False, origin="platform"), ctx
            )

        outcome = self._run(_best_audio(raw), ctx)
        return self._write(raw, outcome.items, outcome, ctx)

    # -- internals ----------------------------------------------------------

    def _run(self, audio: Path, ctx: RunContext) -> AsrOutcome:
        """Try each backend until one returns cues. Raises if none does."""
        failures: list[str] = []
        attempted = 0

        for backend in self.backends:
            ctx.check_cancelled()

            if not _probe(backend, ctx):
                _logger.debug("skipping unavailable ASR backend %s", backend.name)
                continue

            attempted += 1
            started = time.monotonic()

            try:
                outcome = backend.transcribe(audio, ctx)
            except JobCancelled:
                # Not a backend failure: the user asked to stop, and trying the
                # remaining engines would ignore that.
                raise
            except (AsrBackendError, PorterError) as exc:
                failures.append(f"{backend.name}: {exc}")
                ctx.logger.warning("ASR backend %s failed: %s", backend.name, exc)
                continue

            elapsed = time.monotonic() - started
            items = coerce_items(outcome.items)

            if not items:
                # HTTP 200 with an empty body is the usual shape of a quota
                # error. Treating it as success is how a job reports DONE with an
                # empty subtitle file.
                failures.append(f"{backend.name}: returned no cues")
                ctx.logger.warning(
                    "ASR backend %s returned no cues after %.1fs, trying next",
                    backend.name,
                    elapsed,
                )
                continue

            ctx.logger.info(
                "ASR backend %s produced %d cues in %.1fs", backend.name, len(items), elapsed
            )
            ctx.progress(Phase.TRANSCRIBE, 1.0, f"transcribed via {backend.name}")

            origin = outcome.origin or backend.name
            return AsrOutcome(items=items, used_asr=outcome.used_asr, origin=origin)

        raise PorterError(
            "every speech-to-text backend failed",
            phase=Phase.TRANSCRIBE.value,
            attempted=attempted,
            failures=failures,
        )

    def _write(
        self,
        raw: RawMaterials,
        items: list[SubtitleItem],
        outcome: AsrOutcome,
        ctx: RunContext,
    ) -> SubtitleSet:
        """Write the source SRT and build the partial :class:`SubtitleSet`."""
        clean = normalize_subtitle_items(coerce_items(items))
        if not clean:
            raise PorterError(
                "no speech-to-text backend produced any cues",
                phase=Phase.TRANSCRIBE.value,
                backends=[backend.name for backend in self.backends],
            )

        # Measured where possible. `raw.info` comes from PREPARE, which probes the
        # real file, so this is a measurement rather than the (1920, 1080) that
        # v0.1 fabricated when probing failed -- the value that made vertical
        # videos render with horizontal margins.
        info = raw.info
        width = info.width if info is not None else None
        height = info.height if info is not None else None

        cooked = raw.layout.cooked_dir
        cooked.mkdir(parents=True, exist_ok=True)

        # generate_zh_srt renders target_text when present and falls back to
        # source_text otherwise, so it writes the source track correctly while
        # target text is still empty.
        (cooked / SOURCE_SRT_NAME).write_text(generate_zh_srt(clean), encoding="utf-8")

        return SubtitleSet(
            subtitle_bilingual_srt=cooked / BILINGUAL_SRT_NAME,
            subtitle_bilingual_ass=cooked / BILINGUAL_ASS_NAME,
            subtitle_zh_srt=cooked / ZH_SRT_NAME,
            subtitle_zh_ass=cooked / ZH_ASS_NAME,
            items=clean,
            transcript_json_path=cooked / TRANSCRIPT_JSON_NAME,
            transcript_txt_path=cooked / TRANSCRIPT_TXT_NAME,
            sentences=[],
            used_asr=outcome.used_asr,
            video_width=width,
            video_height=height,
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _probe(backend: AsrBackend, ctx: RunContext) -> bool:
    """``available()`` that cannot abort the chain.

    The protocol says ``available()`` never raises. A backend that breaks that
    contract is a bug in the backend, so it is logged at error level *with the
    traceback* — visible, not swallowed — while the chain skips that one backend
    and keeps the four others working. Letting it propagate would turn one broken
    probe into a job that cannot transcribe at all.
    """
    try:
        return bool(backend.available(ctx))
    except Exception:
        ctx.logger.error("ASR backend %s probe raised", backend.name, exc_info=True)
        return False


def _align_platform_chinese(
    raw: RawMaterials, items: list[SubtitleItem], ctx: RunContext
) -> bool:
    """Attach the platform's own Chinese track to ``items``. Returns whether it did.

    A missing or unusable track is normal — most videos have no Chinese captions —
    so this reports rather than raises, and TRANSLATE falls back to a backend.
    """
    chinese = load_platform_subtitles(raw.subtitle_zh)
    if not chinese:
        return False

    align_bilingual_items(items, chinese)
    if not has_chinese_translation(items):
        ctx.logger.warning(
            "the platform Chinese track had %d cues but aligned none of them",
            len(chinese),
        )
        return False

    ctx.logger.info("aligned %d platform Chinese cues", len(chinese))
    return True


def _best_audio(raw: RawMaterials) -> Path:
    """Prefer the ASR-enhanced WAV, falling back to the raw extraction.

    Enhancement is best-effort (see :mod:`porter.media.enhance`), so its output
    may not exist; the unenhanced WAV always does.
    """
    if raw.audio_enhanced is not None and raw.audio_enhanced.exists():
        return raw.audio_enhanced
    return raw.audio
