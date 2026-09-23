"""Phase orchestration.

The pipeline owns *sequencing*, nothing else. Each phase is delegated to a port
(:mod:`porter.ports`), so the same orchestration runs against real backends in
production and against fakes in tests.

Compared with v0.1's ``run_pipeline()``, this changes four behaviours the MCP
frontend requires (see ``docs/REFACTOR_PLAN.md`` §5.4):

==========================  ==========================  ==============================
v0.1                        v0.2                        why
==========================  ==========================  ==============================
``on_progress(str, int)``   typed ``Event`` stream      CLI and MCP both need structure
blocking, no cancel         ``ctx.check_cancelled()``   an MCP client may disconnect
no checkpoints              ``ctx.stage_cached()``      resume after a timeout
unconditional ``doctor``    per-phase capability check  ``--burn skip`` needs no libass
==========================  ==========================  ==============================
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from porter.context import RunContext
from porter.errors import JobCancelled, PorterError
from porter.events import (
    ArtifactKind,
    ArtifactReady,
    ErrorInfo,
    JobState,
    LogRecord,
    Phase,
    PhaseCompleted,
    PhaseFailed,
    PhaseStarted,
)
from porter.models.materials import RawMaterials
from porter.models.request import BurnMode, BurnResult, JobRequest, JobResult
from porter.models.subtitle import SubtitleSet
from porter.ports import (
    Downloader,
    LocalPreparer,
    Renderer,
    Transcriber,
    Translator,
)

__all__ = ["Pipeline"]

#: VideoCaptioner's engine names. Setting one as ``asr.engine`` is the only way to
#: ask for the external CLI, and v0.1 ran it *first* in that case — the opposite of
#: where it sits otherwise, because naming an engine is an explicit request.
_VIDEOCAPTIONER_ENGINES = frozenset({"bijian", "jianying", "whisper-cpp"})

_T = TypeVar("_T")


@dataclass
class Pipeline:
    """Orchestrates the four phases against injected ports.

    Construct directly with fakes in tests, or use :meth:`default` in
    production.
    """

    downloader: Downloader
    transcriber: Transcriber
    translator: Translator
    renderer: Renderer
    #: PREPARE producer for a file already on disk. Separate from ``downloader``
    #: because a path is not a URL -- see :class:`~porter.ports.LocalPreparer`.
    #:
    #: The factory is a lambda rather than ``_default_local`` directly because
    #: ``field(default_factory=...)`` evaluates its argument while the class body
    #: runs, and the assembly helpers deliberately live below the class. The
    #: lambda defers the lookup to first construction, which also keeps the lazy
    #: import lazy.
    local: LocalPreparer = field(default_factory=lambda: _default_local())

    # -- individual phases --------------------------------------------------

    def prepare(self, request: JobRequest, ctx: RunContext) -> RawMaterials:
        """Phase PREPARE: obtain media, extract audio, fetch cover/subtitles.

        The request already says which kind of source it carries -- the two
        fields are mutually exclusive and validated -- so this is a dispatch, not
        a guess.
        """
        if request.local_video is not None:
            return self.local.prepare(request.local_video, ctx)
        if request.url is None:  # pragma: no cover - JobRequest rejects this
            raise PorterError("the request carries neither a URL nor a local video")
        return self.downloader.fetch(request.url, ctx)

    def transcribe(self, raw: RawMaterials, ctx: RunContext) -> SubtitleSet:
        """Phase TRANSCRIBE: platform subtitle if present, ASR otherwise."""
        return self.transcriber.transcribe(raw, ctx)

    def translate(self, subtitles: SubtitleSet, ctx: RunContext) -> SubtitleSet:
        """Phase TRANSLATE: produce bilingual cues and the ASS/SRT files."""
        return self.translator.translate(
            subtitles, ctx.options.target_lang, ctx
        )

    def burn(
        self,
        raw: RawMaterials,
        subtitles: SubtitleSet,
        ctx: RunContext,
    ) -> BurnResult:
        """Phase BURN: hard-sub the requested variants."""
        return self.renderer.render(raw, subtitles, ctx.options.burn, ctx)

    # -- orchestration ------------------------------------------------------

    def run(self, request: JobRequest, ctx: RunContext) -> JobResult:
        """Execute the phases selected by ``request.options``.

        Cancellation is checked between phases and propagated as
        :class:`~porter.errors.JobCancelled`, which the CLI maps to exit code 130
        and the MCP frontend maps to ``JobState.CANCELLED``.

        Never raises for an expected failure: errors are captured into
        ``JobResult.error`` so the MCP frontend can report them as data.
        """
        started = time.monotonic()
        ctx.logger.info("job %s starting", ctx.job_id)

        # Bind the request's options onto the context before anything reads them.
        #
        # `JobOptions` lives on both the request (what was asked for) and the
        # context (what is running), and the pipeline read them from different
        # places: `phases_for` from the request, `burn()` and the renderer from
        # the context. A frontend that set them differently therefore got
        # `--burn zh_only` to *select* the BURN phase and then burn the wrong
        # variants -- silently, because both objects were individually valid. A
        # real end-to-end test caught it; no unit test could, since every unit
        # test passed the same object to both.
        #
        # The request wins because it is the unit of work. Binding here makes
        # disagreement unrepresentable rather than merely unlikely, and covers
        # `force` and `target_lang` for free -- they had the same defect.
        ctx.options = request.options

        raw: RawMaterials | None = None
        subtitles: SubtitleSet | None = None
        burn: BurnResult | None = None
        phase = Phase.PREPARE

        try:
            for phase in self.phases_for(request):
                ctx.check_cancelled()
                ctx.emit(PhaseStarted(phase=phase))
                ctx.emit(LogRecord(level="info", message=f"phase {phase.value} starting"))

                if phase is Phase.PREPARE:
                    raw = self.prepare(request, ctx)
                    self._announce(ctx, ArtifactKind.VIDEO, raw.video, phase)
                elif phase is Phase.TRANSCRIBE:
                    raw = self._require(raw, "PREPARE", phase)
                    subtitles = self.transcribe(raw, ctx)
                elif phase is Phase.TRANSLATE:
                    subtitles = self._require(subtitles, "TRANSCRIBE", phase)
                    subtitles = self.translate(subtitles, ctx)
                elif phase is Phase.BURN:
                    raw = self._require(raw, "PREPARE", phase)
                    subtitles = self._require(subtitles, "TRANSLATE", phase)
                    burn = self.burn(raw, subtitles, ctx)

                ctx.emit(PhaseCompleted(phase=phase))

        except JobCancelled:
            ctx.logger.warning("job %s cancelled during %s", ctx.job_id, phase.value)
            return self._result(ctx, JobState.CANCELLED, started, raw, subtitles, burn, None)

        except PorterError as exc:
            ctx.logger.error("job %s failed during %s: %s", ctx.job_id, phase.value, exc)
            info = ErrorInfo.from_exception(exc)
            ctx.emit(PhaseFailed(phase=phase, error=info))
            return self._result(ctx, JobState.FAILED, started, raw, subtitles, burn, info)

        elapsed = time.monotonic() - started
        ctx.logger.info("job %s finished in %.1fs", ctx.job_id, elapsed)
        return self._result(ctx, JobState.DONE, started, raw, subtitles, burn, None)

    # -- helpers ------------------------------------------------------------

    def phases_for(self, request: JobRequest) -> list[Phase]:
        """Which phases to run, honouring ``only_phase`` and ``burn``.

        ``only_phase`` means "**stop after** this phase", so the phases before it
        still run. It originally meant "run exactly this phase", which was
        structurally impossible for anything but PREPARE: each phase consumes the
        previous one's in-memory output, so TRANSCRIBE with ``only_phase`` set
        failed with "requires output from PREPARE" every time. Resuming a single
        phase from disk is a separate feature (that is what ``force`` is for) and
        is not implemented; until it is, running the prerequisites is the only
        behaviour that can work.

        ``burn`` is applied first, so ``--only-phase burn --burn skip`` runs
        PREPARE, TRANSCRIBE and TRANSLATE and then stops -- which is what someone
        asking for subtitles without a video actually wants.
        """
        phases = [Phase.PREPARE, Phase.TRANSCRIBE, Phase.TRANSLATE]
        if request.options.burn is not BurnMode.SKIP:
            phases.append(Phase.BURN)

        stop_after = request.options.only_phase
        if stop_after is None:
            return phases

        if stop_after not in phases:
            # e.g. only_phase=BURN with burn=SKIP. Nothing to do, and silently
            # running the other three would be worse than doing nothing.
            return []

        return phases[: phases.index(stop_after) + 1]

    @staticmethod
    def _require(value: _T | None, producer: str, phase: Phase) -> _T:
        """Assert a prerequisite phase produced its output."""
        if value is None:
            raise PorterError(
                f"phase {phase.value} requires output from {producer}, "
                f"which did not run or did not complete",
                phase=phase.value,
                producer=producer,
            )
        return value

    @staticmethod
    def _announce(ctx: RunContext, kind: ArtifactKind, path: Path, phase: Phase) -> None:
        ctx.emit(ArtifactReady(kind=kind, path=path, phase=phase))

    @staticmethod
    def _result(
        ctx: RunContext,
        state: JobState,
        started: float,
        raw: RawMaterials | None,
        subtitles: SubtitleSet | None,
        burn: BurnResult | None,
        error: ErrorInfo | None,
    ) -> JobResult:
        return JobResult(
            job_id=ctx.job_id,
            state=state,
            task_dir=raw.layout.task_dir if raw is not None else None,
            raw=raw,
            subtitles=subtitles,
            burn=burn,
            error=error,
            duration_seconds=time.monotonic() - started,
        )

    # -- assembly -----------------------------------------------------------

    @classmethod
    def default(
        cls,
        ctx: RunContext,
        *,
        downloader: Downloader | None = None,
        renderer: Renderer | None = None,
        local: LocalPreparer | None = None,
    ) -> Pipeline:
        """Assemble the production pipeline.

        Every concrete implementation is imported *inside* this method. Importing
        :mod:`porter.pipeline` must stay cheap: it is pulled in by the CLI's
        argument parser, and paying the yt-dlp and openai import cost for
        ``porter --help`` is a visible delay on a cold start.

        ``downloader``, ``renderer`` and ``local`` are injectable so the CLI and
        MCP can pass an already-constructed one (and tests can pass fakes).
        """
        # `is not None`, never `or`. PlatformDownloader defines __len__, so a
        # downloader holding an empty registry is *falsy* and `or` would silently
        # discard the caller's instance and build a real one -- which is exactly
        # what happened, and only the assembly test caught it.
        return cls(
            downloader=_default_downloader() if downloader is None else downloader,
            transcriber=_default_transcriber(ctx),
            translator=_default_translator(ctx),
            renderer=_default_renderer(ctx) if renderer is None else renderer,
            local=_default_local() if local is None else local,
        )


# ----------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------


def _default_local() -> LocalPreparer:
    """The local-file PREPARE producer.

    Imported lazily for the same reason as the downloader: it pulls in the ffmpeg
    layer, which ``porter --help`` should not pay for.
    """
    from porter.platforms.local import LocalFileDownloader

    return LocalFileDownloader()
def _default_downloader() -> Downloader:
    from porter.platforms.downloader import PlatformDownloader

    return PlatformDownloader()


def _default_transcriber(ctx: RunContext) -> Transcriber:
    """Build the ASR chain in v0.1's order.

    ======================  ===================================================
    order                   condition
    ======================  ===================================================
    1. VideoCaptioner CLI   only when ``asr.engine`` names one of its engines
    2. Whisper API          needs an OpenAI-compatible key
    3. Bcut                 key-free, unverified
    4. Google Web           key-free, unverified
    5. VideoCaptioner CLI   otherwise, as the last resort
    ======================  ===================================================

    The CLI appears at two positions rather than one because v0.1 did that
    deliberately: an explicitly configured engine is a request to use it, while an
    unconfigured one is a fallback of last resort. Preserved rather than tidied,
    because the behaviour is user-visible.
    """
    from porter.asr.chain import AsrChain

    chain = AsrChain()

    from porter.asr.bcut import BcutBackend
    from porter.asr.google_web import GoogleWebBackend
    from porter.asr.videocaptioner import VideoCaptionerBackend
    from porter.asr.whisper_api import WhisperApiBackend

    configured = (ctx.config.asr.engine or "").strip().lower()
    cli = VideoCaptionerBackend()

    if configured in _VIDEOCAPTIONER_ENGINES:
        chain.add(cli)

    chain.add(WhisperApiBackend())
    chain.add(BcutBackend())
    chain.add(GoogleWebBackend())

    if configured not in _VIDEOCAPTIONER_ENGINES:
        chain.add(cli)

    return chain


def _default_translator(ctx: RunContext) -> Translator:
    """Build the translation chain: LLM, then the key-free endpoints, then the CLI.

    Order rationale, unchanged from v0.1:

    * **LLM first** because it is the only engine that translates with context, and
      quality is the reason someone would configure an API key at all.
    * **The key-free endpoints next**, cheapest first.
    * **The VideoCaptioner CLI last**: it is an external GPL-3.0 process, so it is
      the heaviest dependency and the one most likely to be absent.

    The chain's own CJK self-check then applies to whichever engine runs, which is
    what makes the ordering safe: a key-free endpoint that echoes its input is
    rejected and the next one is tried, rather than producing an English subtitle
    labelled as Chinese.
    """
    from porter.translate.bing import BingTranslateBackend
    from porter.translate.chain import TranslationChain
    from porter.translate.google import GoogleTranslateBackend
    from porter.translate.llm import LLMTranslationBackend
    from porter.translate.mymemory import MyMemoryBackend
    from porter.translate.videocaptioner import (
        VideocaptionerBackend,
        VideocaptionerLLMBackend,
    )

    return (
        TranslationChain()
        .add(LLMTranslationBackend())
        .add(BingTranslateBackend())
        .add(GoogleTranslateBackend())
        .add(MyMemoryBackend())
        .add(VideocaptionerLLMBackend())
        .add(VideocaptionerBackend())
    )


def _default_renderer(ctx: RunContext) -> Renderer:
    """The production renderer.

    Imported lazily for the same reason as the other concrete ports: this module
    is pulled in by the CLI's argument parser, and the media layer drags in
    subprocess machinery that ``porter --help`` should not pay for.
    """
    from porter.media.burn import FfmpegRenderer

    return FfmpegRenderer(config=ctx.config.ffmpeg)
