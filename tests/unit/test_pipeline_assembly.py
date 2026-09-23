"""Pipeline assembly: which implementation each port gets, and in what order.

``Pipeline.default()`` is the one place where concrete backends are chosen, so it
is where a wrong order or a missing port would be invisible until a real job ran.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from porter.config import PorterConfig
from porter.context import RunContext
from porter.events import Phase
from porter.models.request import BurnMode, BurnResult, JobOptions, JobRequest
from porter.pipeline import Pipeline
from porter.platforms.downloader import PlatformDownloader
from porter.platforms.registry import PlatformRegistry, registry
from porter.ports import Downloader, Renderer, Transcriber, Translator


def _ctx(
    tmp_path, config: PorterConfig | None = None, events=None
) -> RunContext:
    return RunContext(
        job_id="assembly",
        options=JobOptions(output_dir=tmp_path / "out"),
        config=config or PorterConfig(),
        **({"events": events} if events is not None else {}),
    )


# ----------------------------------------------------------------------
# Ports
# ----------------------------------------------------------------------


class TestDefaultWiring:
    def test_every_port_is_populated(self, tmp_path) -> None:
        pipeline = Pipeline.default(_ctx(tmp_path))

        assert isinstance(pipeline.downloader, Downloader)
        assert isinstance(pipeline.transcriber, Transcriber)
        assert isinstance(pipeline.translator, Translator)
        assert isinstance(pipeline.renderer, Renderer)

    def test_the_transcriber_is_an_ordered_chain(self, tmp_path) -> None:
        pipeline = Pipeline.default(_ctx(tmp_path))
        assert [backend.name for backend in pipeline.transcriber.backends]

    def test_an_injected_downloader_is_used(self, tmp_path) -> None:
        """The seam the CLI and MCP need, and the one tests use.

        The injected instance holds an EMPTY registry on purpose. PlatformDownloader
        defines __len__, so an empty one is falsy -- and `downloader or default()`
        silently replaced it with a real downloader. Only comparing identity
        catches that, so this test asserts `is`, not `isinstance`.
        """
        sentinel = PlatformDownloader(PlatformRegistry())
        assert not sentinel, "precondition: an empty downloader is falsy"

        pipeline = Pipeline.default(_ctx(tmp_path), downloader=sentinel)
        assert pipeline.downloader is sentinel

    def test_an_empty_registry_is_not_replaced_by_the_default(self, tmp_path) -> None:
        """The bug the identity assertion above exists to catch."""
        pipeline = Pipeline.default(
            _ctx(tmp_path), downloader=PlatformDownloader(PlatformRegistry())
        )
        assert len(pipeline.downloader) == 0

    def test_an_injected_renderer_is_used(self, tmp_path) -> None:
        class FakeRenderer:
            name = "fake"

            def render(self, raw, subtitles, mode, ctx):
                raise AssertionError("not called")

        fake = FakeRenderer()
        pipeline = Pipeline.default(_ctx(tmp_path), renderer=fake)
        assert pipeline.renderer is fake

    def test_assembling_does_not_import_yt_dlp(self) -> None:
        """``porter --help`` must not pay for yt-dlp.

        yt-dlp pulls in a large dependency tree; importing it from the CLI's
        entry point makes every invocation slower, including commands that never
        touch the network.
        """
        script = (
            "import sys; import porter.pipeline; "
            "print('yt_dlp' in sys.modules or 'yt_dlp' in ''.join(sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"


# ----------------------------------------------------------------------
# ASR chain order
# ----------------------------------------------------------------------


class TestAsrChainOrder:
    """v0.1 ran the VideoCaptioner CLI first only when explicitly asked for.

    Naming one of its engines is a request; leaving the field empty makes the same
    binary a fallback of last resort. Both behaviours are user-visible, so both are
    pinned here rather than tidied into one position.
    """

    def test_the_default_order_is_whisper_then_free_then_cli(self, tmp_path) -> None:
        pipeline = Pipeline.default(_ctx(tmp_path))
        assert [backend.name for backend in pipeline.transcriber.backends] == [
            "whisper-api",
            "bcut",
            "google-web",
            "videocaptioner",
        ]

    @pytest.mark.parametrize("engine", ["bijian", "jianying", "whisper-cpp"])
    def test_a_configured_cli_engine_moves_it_first(self, tmp_path, engine: str) -> None:
        ctx = _ctx(tmp_path, PorterConfig(asr={"engine": engine}))
        pipeline = Pipeline.default(ctx)

        assert pipeline.transcriber.backends[0].name == "videocaptioner"

    def test_a_configured_cli_engine_does_not_duplicate_the_cli(self, tmp_path) -> None:
        """It appears once, at the front, not at both ends."""
        ctx = _ctx(tmp_path, PorterConfig(asr={"engine": "jianying"}))
        names = [backend.name for backend in Pipeline.default(ctx).transcriber.backends]

        assert names.count("videocaptioner") == 1

    def test_an_unrecognised_engine_does_not_move_the_cli_first(self, tmp_path) -> None:
        """A typo in the config must not silently change the chain order."""
        ctx = _ctx(tmp_path, PorterConfig(asr={"engine": "whisperx"}))
        assert Pipeline.default(ctx).transcriber.backends[0].name == "whisper-api"

    def test_the_free_endpoints_sit_between_whisper_and_the_cli(self, tmp_path) -> None:
        names = [b.name for b in Pipeline.default(_ctx(tmp_path)).transcriber.backends]
        assert names.index("whisper-api") < names.index("bcut") < names.index("videocaptioner")


# ----------------------------------------------------------------------
# Translation chain order
# ----------------------------------------------------------------------


class TestTranslationChainOrder:
    def test_the_order_matches_v0_1(self, tmp_path) -> None:
        """LLM first (it is the only context-aware engine), free endpoints next,
        the external GPL-3.0 process last."""
        pipeline = Pipeline.default(_ctx(tmp_path))
        assert [backend.name for backend in pipeline.translator.backends] == [
            "llm",
            "bing",
            "google",
            "mymemory",
            "videocaptioner-llm",
            "videocaptioner",
        ]

    def test_the_llm_is_first_because_quality_is_the_point_of_a_key(self, tmp_path) -> None:
        assert Pipeline.default(_ctx(tmp_path)).translator.backends[0].name == "llm"


# ----------------------------------------------------------------------
# The unwired renderer
# ----------------------------------------------------------------------


class TestRendererWiring:
    """BURN used to be a stub that raised. Now it is the real renderer.

    The stub existed so that ``--burn skip`` and ``--only-phase`` worked while
    hardsubbing did not. Those still matter, but the stub's own tests -- which
    asserted ``CapabilityMissingError`` -- were about a placeholder, so they were
    replaced rather than deleted.
    """

    @pytest.fixture(autouse=True)
    def _no_trial_encode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Answer the encoder question without invoking real ffmpeg.

        This class is about wiring, and about a render failure becoming data
        rather than a traceback. Those tests do need the *real* ``FfmpegRenderer``
        -- the stubs below never write the ASS files, so the genuine validation
        is what refuses the burn -- but they do not need the encoder trial
        encode. Without this stub ``Pipeline.default`` probes the hardware first
        and, on a machine without ffmpeg (as on CI), fails with
        ``capability_missing`` before the renderer is ever consulted.

        Stubbing ``select`` to the software profile is enough: it needs no
        device and ``needs_trial`` is False, so no ffmpeg binary is required.
        Real encoder selection is covered by ``tests/unit/test_encode.py``; real
        encoding by ``tests/regression/test_synthesizer_port.py``, which runs
        actual ffmpeg.
        """
        from porter.media.encode import EncoderSelector, software_profile_for

        monkeypatch.setattr(EncoderSelector, "select", lambda self: software_profile_for())

    def test_default_wires_the_real_renderer(self, tmp_path) -> None:
        from porter.media.burn import FfmpegRenderer
        from porter.ports import Renderer

        pipeline = Pipeline.default(_ctx(tmp_path))

        assert isinstance(pipeline.renderer, FfmpegRenderer)
        assert isinstance(pipeline.renderer, Renderer)
        assert pipeline.renderer.name == "ffmpeg"

    def test_burn_skip_never_reaches_the_renderer(self, tmp_path) -> None:
        """Still the thing that makes subtitle-only jobs cheap."""
        ctx = _ctx(tmp_path)
        pipeline = Pipeline.default(ctx)
        request = _request(tmp_path, burn=BurnMode.SKIP)

        assert Phase.BURN not in pipeline.phases_for(request)

    def test_burn_on_does_reach_the_renderer(self, tmp_path) -> None:
        pipeline = Pipeline.default(_ctx(tmp_path))

        assert Phase.BURN in pipeline.phases_for(_request(tmp_path, burn=BurnMode.DUAL))

    def test_a_burn_failure_surfaces_as_a_failed_job_not_a_traceback(
        self, tmp_path
    ) -> None:
        """A renderer error must be captured as data, like every other phase.

        The stubs never write the ASS files, so the real renderer refuses to
        burn -- which is the honest outcome and must not escape as an exception.
        """
        from porter.events import JobState, PhaseFailed, collect

        events: list[object] = []
        ctx = _ctx(tmp_path, events=collect(events))
        pipeline = Pipeline.default(ctx)
        pipeline.downloader = _StubDownloader(tmp_path)
        pipeline.transcriber = _StubTranscriber()
        pipeline.translator = _StubTranslator()

        result = pipeline.run(_request(tmp_path, burn=BurnMode.DUAL), ctx)

        assert result.state is JobState.FAILED
        assert result.error is not None
        assert result.error.code == "render_error"
        assert "was not written" in result.error.message

        # The failure must be attributed to BURN. An operator reading the log needs
        # to know which phase gave up, and a bare message does not say.
        failures = [e for e in events if isinstance(e, PhaseFailed)]
        assert [e.phase for e in failures] == [Phase.BURN]

    def test_a_failed_burn_does_not_publish_a_release_video(self, tmp_path) -> None:
        """The failure above must leave ``cooked/`` without a release video."""
        ctx = _ctx(tmp_path)
        pipeline = Pipeline.default(ctx)
        pipeline.downloader = _StubDownloader(tmp_path)
        pipeline.transcriber = _StubTranscriber()
        pipeline.translator = _StubTranslator()

        result = pipeline.run(_request(tmp_path, burn=BurnMode.DUAL), ctx)

        assert result.burn is None
        cooked = result.task_dir / "cooked"
        assert not list(cooked.glob("video_*.mp4"))


# ----------------------------------------------------------------------
# The downloader dispatcher
# ----------------------------------------------------------------------


class TestPlatformDownloader:
    def test_it_dispatches_to_the_matching_extractor(self) -> None:
        downloader = PlatformDownloader()
        assert downloader.can_handle("https://www.youtube.com/watch?v=abc")
        assert not downloader.can_handle("https://example.com/video")

    def test_an_unsupported_url_raises_by_identifying_the_platform(self) -> None:
        from porter.errors import UnsupportedPlatformError

        with pytest.raises(UnsupportedPlatformError) as excinfo:
            PlatformDownloader().fetch("https://example.com/video", None)

        assert "youtube" in excinfo.value.supported

    def test_it_reports_the_platforms_it_can_serve(self) -> None:
        assert len(PlatformDownloader()) >= 5

    def test_an_empty_registry_serves_nothing(self) -> None:
        """The registry is injectable so tests never need the real platforms."""
        assert len(PlatformDownloader(PlatformRegistry())) == 0

    def test_a_handler_without_fetch_fails_cleanly(self) -> None:
        """A matching-only handler must not produce an AttributeError."""

        class Matcher:
            name = "matcher"

            def can_handle(self, url: str) -> bool:
                return True

        platforms = PlatformRegistry()
        platforms.register(Matcher())
        downloader = PlatformDownloader(platforms)

        assert downloader.can_handle("https://anything") is True

        from porter.errors import UnsupportedPlatformError

        with pytest.raises(UnsupportedPlatformError):
            downloader.probe("https://anything", None)

    def test_it_uses_the_process_wide_registry_by_default(self) -> None:
        assert tuple(PlatformDownloader().platforms.names()) == tuple(registry().names())


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

class _StubDownloader:
    name = "stub"

    def __init__(self, tmp_path) -> None:
        self._tmp_path = tmp_path

    def can_handle(self, url: str) -> bool:
        return True

    def probe(self, url: str, ctx: RunContext):
        raise NotImplementedError

    def fetch(self, url: str, ctx: RunContext):
        from porter.models.materials import RawMaterials, TaskLayout
        from porter.models.metadata import VideoMetadata

        layout = TaskLayout.build(ctx.output_root, "abc", "Stub")
        layout.ensure_dirs()
        (layout.raw_dir / "video.mp4").write_bytes(b"x")
        (layout.raw_dir / "audio.wav").write_bytes(b"x")
        return RawMaterials(
            layout=layout,
            video=layout.raw_dir / "video.mp4",
            audio=layout.raw_dir / "audio.wav",
            info=VideoMetadata(
                id="abc", title="Stub", safe_title="Stub", url=url, width=1920, height=1080
            ),
        )


class _StubTranscriber:
    name = "stub"

    def available(self, ctx: RunContext) -> bool:
        return True

    def transcribe(self, raw, ctx: RunContext):
        from porter.models.subtitle import SubtitleItem, SubtitleSet

        cooked = raw.layout.cooked_dir
        cooked.mkdir(parents=True, exist_ok=True)
        return SubtitleSet(
            subtitle_bilingual_srt=cooked / "b.srt",
            subtitle_bilingual_ass=cooked / "b.ass",
            subtitle_zh_srt=cooked / "z.srt",
            subtitle_zh_ass=cooked / "z.ass",
            transcript_json_path=cooked / "transcript.json",
            transcript_txt_path=cooked / "transcript.txt",
            items=[SubtitleItem(1, 0, 1000, "hello", "你好")],
        )


class _StubTranslator:
    name = "stub"

    def available(self, ctx: RunContext) -> bool:
        return True

    def translate(self, subtitles, target_lang, ctx):
        return subtitles


class TestPhaseSelection:
    """``only_phase`` means "stop after this phase", not "run only this one".

    The distinction is not cosmetic. Each phase consumes the previous one's
    in-memory output, so "run exactly TRANSCRIBE" was impossible: it always failed
    with "requires output from PREPARE". Resuming a single phase from disk is a
    separate unimplemented feature (that is what ``force`` is for), so running the
    prerequisites is the only behaviour that can work today.
    """

    @pytest.fixture
    def pipeline(self, tmp_path):
        return Pipeline.default(_ctx(tmp_path))

    def test_no_only_phase_runs_everything(self, pipeline, tmp_path) -> None:
        assert pipeline.phases_for(_request(tmp_path, burn=BurnMode.DUAL)) == [
            Phase.PREPARE,
            Phase.TRANSCRIBE,
            Phase.TRANSLATE,
            Phase.BURN,
        ]

    def test_burn_skip_drops_the_burn_phase(self, pipeline, tmp_path) -> None:
        assert pipeline.phases_for(_request(tmp_path, burn=BurnMode.SKIP)) == [
            Phase.PREPARE,
            Phase.TRANSCRIBE,
            Phase.TRANSLATE,
        ]

    @pytest.mark.parametrize(
        ("only", "expected"),
        [
            (Phase.PREPARE, [Phase.PREPARE]),
            (Phase.TRANSCRIBE, [Phase.PREPARE, Phase.TRANSCRIBE]),
            (Phase.TRANSLATE, [Phase.PREPARE, Phase.TRANSCRIBE, Phase.TRANSLATE]),
        ],
    )
    def test_prerequisites_are_included(self, pipeline, tmp_path, only, expected) -> None:
        request = _request(tmp_path, burn=BurnMode.SKIP, only_phase=only)
        assert pipeline.phases_for(request) == expected

    def test_a_consuming_phase_never_runs_without_its_producer(
        self, pipeline, tmp_path
    ) -> None:
        """The invariant that makes the original failure impossible.

        Each phase reads the previous phase's in-memory output, so a selection
        containing TRANSCRIBE without PREPARE is guaranteed to fail at runtime --
        which is exactly what "run exactly this phase" produced.
        """
        consumes = {
            Phase.TRANSCRIBE: Phase.PREPARE,
            Phase.TRANSLATE: Phase.TRANSCRIBE,
            Phase.BURN: Phase.TRANSLATE,
        }
        for only in (None, *Phase):
            for burn in (BurnMode.SKIP, BurnMode.DUAL):
                selected = pipeline.phases_for(
                    _request(tmp_path, burn=burn, only_phase=only)
                )
                for phase, producer in consumes.items():
                    if phase in selected:
                        assert producer in selected, (
                            f"{phase.value} selected without {producer.value}"
                        )

    def test_only_burn_with_burn_skip_selects_nothing(self, pipeline, tmp_path) -> None:
        """Contradictory request: run only the phase that was just disabled.

        Doing nothing is honest; silently running the other three would produce
        subtitles the caller did not ask for.
        """
        request = _request(tmp_path, burn=BurnMode.SKIP, only_phase=Phase.BURN)
        assert pipeline.phases_for(request) == []

    def test_only_burn_with_burn_on_runs_the_whole_pipeline(self, pipeline, tmp_path) -> None:
        request = _request(tmp_path, burn=BurnMode.DUAL, only_phase=Phase.BURN)
        assert pipeline.phases_for(request) == [
            Phase.PREPARE,
            Phase.TRANSCRIBE,
            Phase.TRANSLATE,
            Phase.BURN,
        ]


def _request(tmp_path, *, burn: BurnMode, only_phase: Phase | None = None):
    """A request with the phase-selection options set."""
    from porter.models.request import JobRequest

    return JobRequest(
        url="https://www.youtube.com/watch?v=abc",
        options=JobOptions(
            output_dir=tmp_path / "out", burn=burn, only_phase=only_phase
        ),
    )


class TestRequestOptionsWin:
    """The request's options must be the ones the run actually uses.

    ``JobOptions`` lives on both ``JobRequest`` (what was asked for) and
    ``RunContext`` (what is running). The pipeline read them from different
    places -- ``phases_for`` from the request, ``burn()`` and the renderer from
    the context -- so a frontend that set them differently got ``--burn zh_only``
    to *select* the BURN phase and then burn the wrong variants. Silently, because
    both objects were individually valid.

    Found by a real end-to-end run: every unit test passed the same object to
    both, so no unit test could have caught it.
    """

    def test_the_context_options_are_rebound_from_the_request(self, tmp_path) -> None:
        ctx = _ctx(tmp_path)
        assert ctx.options.burn is BurnMode.DUAL, "precondition: the default"

        pipeline = Pipeline.default(ctx)
        pipeline.downloader = _StubDownloader(tmp_path)
        pipeline.transcriber = _StubTranscriber()
        pipeline.translator = _StubTranslator()
        pipeline.run(_request(tmp_path, burn=BurnMode.SKIP), ctx)

        assert ctx.options.burn is BurnMode.SKIP
        assert ctx.options.only_phase is None

    def test_phase_selection_and_the_renderer_agree(self, tmp_path) -> None:
        """The two readers of ``burn`` must see the same value.

        The renderer is a spy here: what matters is which mode it was handed, and
        that it is the mode the phase selection used.
        """
        ctx = _ctx(tmp_path)
        pipeline = Pipeline.default(ctx)
        pipeline.downloader = _StubDownloader(tmp_path)
        pipeline.transcriber = _StubTranscriber()
        pipeline.translator = _StubTranslator()

        seen: list[BurnMode] = []

        class SpyRenderer:
            name = "spy"

            def render(self, raw, subtitles, mode, ctx):
                seen.append(mode)
                return BurnResult()

        pipeline.renderer = SpyRenderer()
        request = _request(tmp_path, burn=BurnMode.ZH_ONLY)

        assert pipeline.phases_for(request) == [
            Phase.PREPARE,
            Phase.TRANSCRIBE,
            Phase.TRANSLATE,
            Phase.BURN,
        ]
        pipeline.run(request, ctx)

        assert seen == [BurnMode.ZH_ONLY], "the renderer must get the requested mode"

    def test_force_is_rebound_too(self, tmp_path) -> None:
        """``force`` had the same defect; it is read from the context by the renderer."""
        ctx = _ctx(tmp_path)
        pipeline = Pipeline.default(ctx)
        pipeline.downloader = _StubDownloader(tmp_path)
        pipeline.transcriber = _StubTranscriber()
        pipeline.translator = _StubTranslator()
        pipeline.renderer = _StubRenderer()

        request = JobRequest(
            url="https://example.com/v",
            options=JobOptions(output_dir=tmp_path / "out", burn=BurnMode.SKIP, force=True),
        )
        pipeline.run(request, ctx)

        assert ctx.options.force is True


class _StubRenderer:
    name = "stub"

    def render(self, raw, subtitles, mode, ctx):
        return BurnResult()
