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
    tmp_path, config: PorterConfig | None = None, events=None, **options: object
) -> RunContext:
    """A run context. ``**options`` are :class:`JobOptions` overrides."""
    return RunContext(
        job_id="assembly",
        options=JobOptions(output_dir=tmp_path / "out", **options),
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
    """Local Whisper first, then the paid API, then the free endpoints, then the CLI.

    v0.1 ran the VideoCaptioner CLI first only when explicitly asked for: naming
    one of its engines is a request, leaving the field empty makes the same binary
    a fallback of last resort. Both behaviours are user-visible and both are
    pinned here.

    §13.48 moved local Whisper to the head and gave ``asr.engine`` a general
    promote-a-named-backend rule, so the older assertions below changed rather
    than being deleted.
    """

    def test_the_default_order_is_local_then_api_then_free_then_cli(self, tmp_path) -> None:
        pipeline = Pipeline.default(_ctx(tmp_path))
        assert [backend.name for backend in pipeline.transcriber.backends] == [
            "whisper-local",
            "whisper-api",
            "bcut",
            "google-web",
            "videocaptioner",
        ]

    def test_the_only_verified_backend_leads(self, tmp_path) -> None:
        """Local inference is the one engine with no remote protocol to drift.

        The two key-free endpoints were measured returning empty results
        (§13.21), so leading with them would mean the chain tries two engines it
        knows cannot work before reaching one that can.
        """
        backends = Pipeline.default(_ctx(tmp_path)).transcriber.backends
        assert backends[0].name == "whisper-local"
        assert backends[0].endpoint_verified is True

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

    @pytest.mark.parametrize(
        "engine", ["whisper-local", "whisper-api", "bcut", "google-web", "videocaptioner"]
    )
    def test_the_named_backend_flag_is_promoted_to_the_front(
        self, tmp_path, engine: str
    ) -> None:
        """``--asr-engine``, which is what the CLI and both MCP tools actually pass.

        This test used to set ``asr.engine`` in the config while its own docstring
        described the *flag* being ignored -- and the flag really was ignored: all
        three frontends wrote ``JobOptions.asr_engine`` and no code read it, so
        ``porter run --asr-engine whisper-api`` did nothing while the same value in
        the config worked. Testing the config key under a docstring about the flag
        is how that survived.
        """
        ctx = _ctx(tmp_path, asr_engine=engine)
        names = [backend.name for backend in Pipeline.default(ctx).transcriber.backends]

        assert names[0] == engine
        # Promotion is a reorder, not a filter: the rest stay as fallbacks.
        assert names.count(engine) == 1
        assert len(names) == 5

    @pytest.mark.parametrize(
        "engine", ["whisper-local", "whisper-api", "bcut", "google-web", "videocaptioner"]
    )
    def test_a_configured_backend_is_promoted_to_the_front(
        self, tmp_path, engine: str
    ) -> None:
        """The config key is the other way to ask, and it must keep working."""
        ctx = _ctx(tmp_path, PorterConfig(asr={"engine": engine}))
        names = [backend.name for backend in Pipeline.default(ctx).transcriber.backends]

        assert names[0] == engine
        assert len(names) == 5

    def test_the_flag_wins_over_the_config_key(self, tmp_path) -> None:
        """Two sources, one answer, and the more specific one wins.

        Without a stated rule the outcome would depend on which line happened to
        be written first -- the kind of ambiguity that surfaces as a bug report
        years later.
        """
        ctx = _ctx(tmp_path, PorterConfig(asr={"engine": "google-web"}), asr_engine="bcut")

        names = [backend.name for backend in Pipeline.default(ctx).transcriber.backends]

        assert names[0] == "bcut"
        assert names.count("google-web") == 1

    def test_an_unrecognised_engine_keeps_the_default_order(
        self, tmp_path, monkeypatch
    ) -> None:
        """A typo must not change the chain, and must not pass unremarked either.

        Asserts on the module logger directly rather than via ``caplog``: the
        engine root sets ``propagate = False`` (deliberately — see
        ``porter.logging``), so records never reach the root logger a ``caplog``
        handler hangs off.
        """
        import porter.pipeline as pipeline_module

        messages: list[str] = []

        class _Recorder:
            def warning(self, message: str, *args: object) -> None:
                messages.append(message % args if args else message)

            def __getattr__(self, _name: str):
                return lambda *args, **kwargs: None

        monkeypatch.setattr(pipeline_module, "_logger", _Recorder())
        ctx = _ctx(tmp_path, PorterConfig(asr={"engine": "whisperx"}))

        names = [backend.name for backend in Pipeline.default(ctx).transcriber.backends]

        assert names[0] == "whisper-local"
        assert any("whisperx" in message for message in messages)

    def test_the_free_endpoints_sit_between_whisper_and_the_cli(self, tmp_path) -> None:
        names = [b.name for b in Pipeline.default(_ctx(tmp_path)).transcriber.backends]
        assert names.index("whisper-api") < names.index("bcut") < names.index("videocaptioner")

    def test_a_named_backend_that_is_missing_is_reported(
        self, tmp_path, monkeypatch
    ) -> None:
        """Naming a backend keeps the rest as fallbacks, so a missing one is not
        fatal -- which is exactly why silence is wrong: someone who asked for
        `bijian` and got Bcut's output has no way to tell.

        The absence is forced rather than assumed, so the test does not depend on
        whether the machine happens to have the VideoCaptioner CLI installed.
        """
        import porter.pipeline as pipeline_module

        monkeypatch.setattr("porter.asr.videocaptioner._resolve_binary", lambda: None)

        messages: list[str] = []

        class _Recorder:
            def warning(self, message: str, *args: object) -> None:
                messages.append(message % args if args else message)

            def __getattr__(self, _name: str):
                return lambda *args, **kwargs: None

        monkeypatch.setattr(pipeline_module, "_logger", _Recorder())
        # `bijian` is one of VideoCaptioner's engine names.
        ctx = _ctx(tmp_path, PorterConfig(asr={"engine": "bijian"}))

        names = [backend.name for backend in Pipeline.default(ctx).transcriber.backends]

        assert names[0] == "videocaptioner", "naming still promotes"
        assert any("VideoCaptioner" in message for message in messages), messages

    def test_an_available_named_backend_is_not_reported_as_missing(
        self, tmp_path, monkeypatch
    ) -> None:
        """The warning must track reality, not fire on every named backend."""
        import porter.pipeline as pipeline_module

        monkeypatch.setattr(
            "porter.asr.videocaptioner._resolve_binary", lambda: "/usr/bin/videocaptioner"
        )
        messages: list[str] = []

        class _Recorder:
            def warning(self, message: str, *args: object) -> None:
                messages.append(message % args if args else message)

            def __getattr__(self, _name: str):
                return lambda *args, **kwargs: None

        monkeypatch.setattr(pipeline_module, "_logger", _Recorder())
        ctx = _ctx(tmp_path, PorterConfig(asr={"engine": "bijian"}))

        Pipeline.default(ctx)

        assert not any("not on PATH" in message for message in messages), messages


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

    @pytest.mark.parametrize(
        "backend",
        ["llm", "bing", "google", "mymemory", "videocaptioner-llm", "videocaptioner"],
    )
    def test_a_named_backend_is_promoted_to_the_front(self, tmp_path, backend: str) -> None:
        """``--translator`` was the same dead flag ``--asr-engine`` was.

        ``JobOptions.translator`` was accepted by both frontends and read by no
        engine: ``_default_translator`` assembled the whole chain
        unconditionally, so ``--translator bing`` silently got the default order.
        Promotion is a reorder, not a filter -- naming a backend asks for it to
        be *tried first*, not for the job to die when that endpoint is
        rate-limited, which is exactly what happened to bing and google on the
        §13.48 run.
        """
        ctx = _ctx(tmp_path, translator=backend)
        names = [item.name for item in Pipeline.default(ctx).translator.backends]

        assert names[0] == backend
        assert names.count(backend) == 1
        assert len(names) == 6

    def test_promoting_the_cli_backend_does_not_drag_its_llm_sibling(self, tmp_path) -> None:
        """Two distinct backends, two distinct names. No aliasing magic.

        ``videocaptioner`` and ``videocaptioner-llm`` are different engines with
        different requirements (the latter needs an API key), so naming one must
        move only that one. A user who wants the LLM variant can name it.
        """
        names = [
            item.name
            for item in Pipeline.default(_ctx(tmp_path, translator="videocaptioner")).translator.backends
        ]

        assert names[0] == "videocaptioner"
        assert names.index("videocaptioner-llm") > 0

    def test_an_unrecognised_translator_keeps_the_default_order(
        self, tmp_path, monkeypatch
    ) -> None:
        """A typo changes nothing, and is reported rather than swallowed."""
        import porter.pipeline as pipeline_module

        messages: list[str] = []

        class _Recorder:
            def warning(self, message: str, *args: object) -> None:
                messages.append(message % args if args else message)

            def __getattr__(self, _name: str):
                return lambda *args, **kwargs: None

        monkeypatch.setattr(pipeline_module, "_logger", _Recorder())
        ctx = _ctx(tmp_path, translator="deepl")

        names = [item.name for item in Pipeline.default(ctx).translator.backends]

        assert names[0] == "llm"
        assert any("deepl" in message for message in messages)

    def test_the_flag_is_read_from_the_options_not_the_config(self, tmp_path) -> None:
        """Where the value comes from matters: there is no config equivalent.

        ``asr.engine`` is a config field, so it is read from ``ctx.config``.
        Translation has no ``translate`` section at all -- ``PorterConfig`` would
        silently drop one, since it sets ``extra="ignore"`` -- so the per-job
        option is the only source. That is also what makes it work from the MCP
        frontend, whose tool schema is derived from ``JobOptions``.
        """
        ctx = _ctx(tmp_path, translator="google")

        assert not hasattr(ctx.config, "translate")
        assert Pipeline.default(ctx).translator.backends[0].name == "google"


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
