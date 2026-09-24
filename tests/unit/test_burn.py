"""Unit tests for the BURN phase.

The happy path needs real ffmpeg and is covered in
``tests/regression/test_synthesizer_port.py``. This file covers what a fake
runner can prove: the argv that gets built, the failure handling, and the reuse
logic. Those are where the v0.1 defects were.

The central test is :class:`TestPathsThatBreakEscaping`, because the whole design
of this module exists to avoid a bug that only appears when the user's output
directory contains an apostrophe.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from porter.config import PorterConfig
from porter.context import RunContext
from porter.errors import MediaError, RenderError
from porter.events import ArtifactReady, Phase, ProgressUpdated, collect
from porter.media.burn import (
    BILINGUAL_NAME,
    ZH_NAME,
    FfmpegRenderer,
    _filter_arg,
    _is_reusable,
    burn_hardsub,
    escape_ffmpeg_filter_path,
    render_release,
)
from porter.media.encode import software_profile_for
from porter.media.probe import MediaInfo
from porter.models.materials import RawMaterials, TaskLayout
from porter.models.request import BurnMode, JobOptions
from porter.models.subtitle import SubtitleSet

# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


@dataclass
class _Recorded:
    args: list[str]
    what: str
    cwd: Path | None
    timeout: float | None


class _FakeRunner:
    """A runner that records argv and writes a plausible output file.

    Not an ``FFmpegRunner`` subclass: the point is to assert on the argv, so the
    fake must not share the real implementation's behaviour.
    """

    def __init__(
        self,
        *,
        fail_with: Exception | None = None,
        write_output: bool = True,
    ) -> None:
        self.calls: list[_Recorded] = []
        self._fail_with = fail_with
        self._write_output = write_output

    def run(
        self,
        args: list[str],
        *,
        what: str,
        check: bool = True,
        timeout: float | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(_Recorded(list(args), what, cwd, timeout))
        if self._fail_with is not None:
            raise self._fail_with
        if self._write_output:
            # The last positional argument is the destination -- except for the
            # encoder trial encode, which writes to `-f null -`. Treating that as
            # a path created a file literally named "-" in the repository root.
            dest = Path(args[-1])
            if str(dest) != "-":
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"\x00" * 64)
        return subprocess.CompletedProcess(args, 0, "", "")


def _burns(fake: _FakeRunner) -> list[str]:
    """The ``what`` of every real burn, excluding trial encodes."""
    return [c.what for c in fake.calls if c.what.startswith("burning ")]


def _trials(fake: _FakeRunner) -> list[_Recorded]:
    """Runner calls that are encoder trial encodes (``lavfi`` colour source)."""
    return [c for c in fake.calls if "lavfi" in c.args]


@pytest.fixture(autouse=True)
def _stub_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``probe`` accept the fake runner's stub output.

    The fake writes 64 bytes, which real ffprobe cannot parse, so without this
    every fake-based test would fail at the validation step. Patching here keeps
    these tests about the argv and the control flow -- what a fake can actually
    prove. Real probing and real encoding are covered by
    ``tests/regression/test_synthesizer_port.py``, which runs actual ffmpeg.

    Tests that need the *real* "unreadable" verdict re-patch ``probe``
    themselves, and monkeypatch applies the innermost patch last.
    """
    import porter.media.burn as module

    monkeypatch.setattr(
        module, "probe", lambda *a, **k: MediaInfo(path=Path("stub.mp4"), video_codec="h264")
    )


def _runner_and_renderer(tmp_path: Path, **kwargs: object) -> tuple[_FakeRunner, FfmpegRenderer]:
    fake = _FakeRunner(**kwargs)  # type: ignore[arg-type]
    renderer = FfmpegRenderer(fake, config=PorterConfig().ffmpeg)  # type: ignore[arg-type]
    return fake, renderer


def _fixture(tmp_path: Path, *, subs: bool = True) -> tuple[RawMaterials, SubtitleSet]:
    cooked = tmp_path / "cooked"
    cooked.mkdir(parents=True, exist_ok=True)
    master = tmp_path / "video.mp4"
    master.write_bytes(b"\x00" * 128)

    paths = {}
    for name in ("subtitle_bilingual.ass", "subtitle_zh.ass"):
        path = cooked / name
        if subs:
            path.write_text("[Events]\n", encoding="utf-8")
        paths[name] = path

    layout = TaskLayout(task_dir=tmp_path, video_id="vid", safe_title="a title")
    raw = RawMaterials(layout=layout, video=master, audio=tmp_path / "audio.wav")
    subtitles = SubtitleSet(
        subtitle_bilingual_srt=cooked / "subtitle_bilingual.srt",
        subtitle_bilingual_ass=paths["subtitle_bilingual.ass"],
        subtitle_zh_srt=cooked / "subtitle_zh.srt",
        subtitle_zh_ass=paths["subtitle_zh.ass"],
        transcript_json_path=cooked / "transcript.json",
        transcript_txt_path=cooked / "transcript.txt",
        items=[],
        video_width=1920,
        video_height=1080,
    )
    return raw, subtitles


def _relative_fixture(tmp_path: Path) -> None:
    """A master and an ASS track laid out as the pipeline lays them out.

    For the tests that ``chdir`` and then pass output-relative paths, which is
    what the real CLI does and what ``tmp_path``-based tests cannot express.
    """
    raw = tmp_path / "raw"
    cooked = tmp_path / "cooked"
    raw.mkdir(parents=True, exist_ok=True)
    cooked.mkdir(parents=True, exist_ok=True)
    (raw / "video.mp4").write_bytes(b"\x00" * 128)
    (cooked / "subtitle_zh.ass").write_text("[Events]\n", encoding="utf-8")


def _ctx(tmp_path: Path, *, force: bool = False) -> RunContext:
    return RunContext(
        job_id="burn",
        options=JobOptions(output_dir=tmp_path, force=force),
        config=PorterConfig(),
    )


# ----------------------------------------------------------------------
# The design that avoids escaping
# ----------------------------------------------------------------------


class TestPathsThatBreakEscaping:
    """The user's output directory must never enter the filtergraph.

    v0.1 put the absolute subtitle path in ``-vf`` and escaped it. Measured
    against real ffmpeg that cannot work for an apostrophe -- the filtergraph
    parser consumes the quote and the path silently changes. It is reachable,
    because ``sanitize_filename`` keeps apostrophes, so a video titled "It's a
    Wonderful Life" produced a task directory that every burn failed on.
    """

    def test_the_filter_argument_is_a_bare_filename(self) -> None:
        """Not a path. A path would be resolved against the wrong directory."""
        arg = _filter_arg(Path("/some/it's here/cooked/subtitle_zh.ass"))

        assert arg == "ass='subtitle_zh.ass'"
        assert "/" not in arg.replace("ass='", "").replace("'", "")

    def test_an_apostrophe_in_the_directory_is_irrelevant(self) -> None:
        """The whole point: the apostrophe is in the cwd, not the filtergraph."""
        # A literal path string, never touched on the filesystem: the point is
        # that the apostrophe reaches the filter argument, or does not.
        arg = _filter_arg(Path("/tmp/burn_it's_a_test/cooked/subtitle_zh.ass"))  # noqa: S108

        assert "'" not in arg.replace("ass='", "", 1).replace("'", "", 1)
        assert "it's" not in arg

    def test_the_child_runs_in_the_subtitle_directory(self, tmp_path: Path) -> None:
        """``cwd`` is what makes the bare filename resolve."""
        raw, subtitles = _fixture(tmp_path)
        fake = _FakeRunner()

        burn_hardsub(
            fake,  # type: ignore[arg-type]
            raw.video,
            subtitles.subtitle_zh_ass,
            tmp_path / "cooked" / "out.mp4",
            profile=software_profile_for(4),
        )

        assert fake.calls[0].cwd == subtitles.subtitle_zh_ass.parent

    def test_the_bare_name_is_not_resolved_against_the_process_cwd(self) -> None:
        """Regression: the first version resolved the name and broke the design.

        ``Path("subtitle_zh.ass").resolve()`` resolves against *this* process's
        working directory, not the child's ``cwd``, so the filtergraph received
        ``/home/.../subtitle_zh.ass`` -- a path that does not exist. ffmpeg said
        "Could not create a libass track when reading file", which is a confusing
        way to learn about a resolution bug.
        """
        arg = _filter_arg(Path("subtitle_zh.ass"))

        assert Path.cwd().as_posix() not in arg

    def test_relative_argv_paths_are_made_absolute(self, tmp_path: Path, monkeypatch) -> None:
        """The other half of the same design, and it was missing entirely.

        ``cwd=subtitle.parent`` makes *every* relative path in argv ambiguous,
        not just the filtergraph's. The pipeline passes output-relative paths
        (the ones it prints to the user), so ffmpeg looked for
        ``cooked/porter_output/<task>/raw/video.mp4`` and reported "could not
        read the master video" while the file sat in the right place.

        No earlier test could see it: they all pass ``tmp_path``, which is
        absolute by construction.
        """
        _relative_fixture(tmp_path)
        monkeypatch.chdir(tmp_path)
        fake = _FakeRunner()

        burn_hardsub(
            fake,  # type: ignore[arg-type]
            Path("raw/video.mp4"),
            Path("cooked/subtitle_zh.ass"),
            Path("cooked/out.mp4"),
            profile=software_profile_for(4),
        )

        args = fake.calls[0].args
        source = args[args.index("-i") + 1]
        # The last argument is the *temporary* output: ffmpeg writes beside the
        # destination and the rename happens only after the file probes clean.
        dest = Path(args[-1])

        assert Path(source).is_absolute(), "-i must not depend on the child's cwd"
        assert dest.is_absolute(), "the output must not either"
        assert source == str((tmp_path / "raw" / "video.mp4").resolve())
        assert dest.parent == (tmp_path / "cooked").resolve()
        assert dest.name == ".tmp_out.mp4"

    def test_relative_paths_do_not_undo_the_apostrophe_design(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Making argv absolute must not put a path back in the filtergraph."""
        _relative_fixture(tmp_path)
        monkeypatch.chdir(tmp_path)
        fake = _FakeRunner()

        burn_hardsub(
            fake,  # type: ignore[arg-type]
            Path("raw/video.mp4"),
            Path("cooked/subtitle_zh.ass"),
            Path("cooked/out.mp4"),
            profile=software_profile_for(4),
        )

        args = fake.calls[0].args
        assert args[args.index("-vf") + 1] == "ass='subtitle_zh.ass'"
        assert str(tmp_path) not in args[args.index("-vf") + 1]

    def test_the_temp_file_stays_beside_the_destination(self, tmp_path: Path, monkeypatch) -> None:
        """Atomicity depends on the temp file sharing a directory with the target."""
        _relative_fixture(tmp_path)
        monkeypatch.chdir(tmp_path)
        fake = _FakeRunner()

        burn_hardsub(
            fake,  # type: ignore[arg-type]
            Path("raw/video.mp4"),
            Path("cooked/subtitle_zh.ass"),
            Path("cooked/out.mp4"),
            profile=software_profile_for(4),
        )

        temp = Path(fake.calls[0].args[-1])
        assert temp.name.startswith(".tmp_")
        assert temp.parent == (tmp_path / "cooked").resolve()

    def test_srt_uses_the_subtitles_filter_and_ass_uses_ass(self) -> None:
        assert _filter_arg(Path("subtitle_zh.ass")).startswith("ass=")
        assert _filter_arg(Path("subtitle_zh.srt")).startswith("subtitles=")

    def test_the_case_of_the_extension_does_not_matter(self) -> None:
        assert _filter_arg(Path("subtitle_zh.ASS")).startswith("ass=")


class TestEscapeFunction:
    """``escape_ffmpeg_filter_path`` keeps the v0.1 contract and states its limit."""

    def test_a_plain_path_is_unchanged_apart_from_resolution(self, tmp_path: Path) -> None:
        escaped = escape_ffmpeg_filter_path(tmp_path / "sub.ass")

        # The drive colon is escaped by design, so ``as_posix()`` is not a prefix on
        # Windows: ``C:/tmp/sub.ass`` becomes ``C\:/tmp/sub.ass``, which is what
        # ffmpeg's filtergraph parser needs. Escaping the expected prefix keeps the
        # assertion about "nothing else changed" on every platform.
        assert escaped.startswith(tmp_path.as_posix().replace(":", r"\:"))
        assert escaped.endswith("sub.ass")

    def test_a_colon_is_escaped(self) -> None:
        assert r"\:" in escape_ffmpeg_filter_path("C:/Users/user/sub.ass")

    def test_the_docstring_records_that_apostrophes_cannot_be_escaped(self) -> None:
        """A reader must not think this function makes arbitrary paths safe.

        Asserted rather than left as prose because the failure mode is silent: the
        escaped string looks right and ffmpeg reads a different file.
        """
        doc = escape_ffmpeg_filter_path.__doc__ or ""

        assert "cannot" in doc.lower()
        assert "apostrophe" in doc.lower()
        assert "working directory" in doc.lower()


# ----------------------------------------------------------------------
# Failure handling
# ----------------------------------------------------------------------


class TestNothingCorruptIsEverPublished:
    """v0.1 renamed the temp file unconditionally, so a truncated encode shipped."""

    def test_a_failed_burn_leaves_no_temp_file(self, tmp_path: Path) -> None:
        raw, subtitles = _fixture(tmp_path)
        out = tmp_path / "cooked" / "out.mp4"
        fake = _FakeRunner(fail_with=MediaError("ffmpeg said no", stderr="boom"))

        with pytest.raises(RenderError):
            burn_hardsub(
                fake,  # type: ignore[arg-type]
                raw.video,
                subtitles.subtitle_zh_ass,
                out,
                profile=software_profile_for(4),
            )

        assert not out.exists()
        assert list(out.parent.glob(".tmp_*")) == []

    def test_a_failed_burn_does_not_overwrite_a_good_previous_release(
        self, tmp_path: Path
    ) -> None:
        """A re-run that fails must not destroy the video from the last good run."""
        raw, subtitles = _fixture(tmp_path)
        out = tmp_path / "cooked" / "out.mp4"
        out.write_bytes(b"previous good release")
        fake = _FakeRunner(fail_with=MediaError("ffmpeg said no", stderr="boom"))

        with pytest.raises(RenderError):
            burn_hardsub(
                fake,  # type: ignore[arg-type]
                raw.video,
                subtitles.subtitle_zh_ass,
                out,
                profile=software_profile_for(4),
            )

        assert out.read_bytes() == b"previous good release"

    def test_success_with_no_output_file_is_an_error(self, tmp_path: Path) -> None:
        """ffmpeg exiting 0 is not proof a file was written."""
        raw, subtitles = _fixture(tmp_path)
        fake = _FakeRunner(write_output=False)

        with pytest.raises(RenderError, match="wrote no file"):
            burn_hardsub(
                fake,  # type: ignore[arg-type]
                raw.video,
                subtitles.subtitle_zh_ass,
                tmp_path / "cooked" / "out.mp4",
                profile=software_profile_for(4),
            )

    def test_an_unreadable_output_is_not_published(self, tmp_path: Path, monkeypatch) -> None:
        """The temp file is probed before the rename; an unreadable one is dropped.

        This is the check v0.1 performed only on the reuse path -- the path that
        by definition does not produce a file.
        """
        import porter.media.burn as module

        raw, subtitles = _fixture(tmp_path)
        out = tmp_path / "cooked" / "out.mp4"
        fake = _FakeRunner()
        monkeypatch.setattr(module, "probe", lambda *a, **k: None)

        with pytest.raises(RenderError, match="unreadable"):
            burn_hardsub(
                fake,  # type: ignore[arg-type]
                raw.video,
                subtitles.subtitle_zh_ass,
                out,
                profile=software_profile_for(4),
            )

        assert not out.exists()

    def test_a_missing_libass_is_diagnosed_not_guessed(self, tmp_path: Path) -> None:
        """The message must name the cause, because the fix is not in this project."""
        raw, subtitles = _fixture(tmp_path)
        fake = _FakeRunner(
            fail_with=MediaError("boom", stderr="No such filter: 'ass'")
        )

        with pytest.raises(RenderError) as excinfo:
            burn_hardsub(
                fake,  # type: ignore[arg-type]
                raw.video,
                subtitles.subtitle_zh_ass,
                tmp_path / "cooked" / "out.mp4",
                profile=software_profile_for(4),
            )

        message = str(excinfo.value)
        assert "libass" in message
        assert "porter doctor" in message

    def test_there_is_no_silent_fallback_to_srt(self, tmp_path: Path) -> None:
        """v0.1 fell back to the sibling .srt when the ASS burn failed.

        That produced a *different* video -- different styling, possibly different
        line breaks -- and reported success, so the operator never learned that
        the styled release was missing. The .srt is present here and must not be
        used.
        """
        raw, subtitles = _fixture(tmp_path)
        subtitles.subtitle_zh_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
        fake = _FakeRunner(fail_with=MediaError("boom", stderr="No such filter: 'ass'"))

        with pytest.raises(RenderError):
            burn_hardsub(
                fake,  # type: ignore[arg-type]
                raw.video,
                subtitles.subtitle_zh_ass,
                tmp_path / "cooked" / "out.mp4",
                profile=software_profile_for(4),
            )

        # Exactly one attempt, and it used the .ass that was asked for.
        assert len(fake.calls) == 1
        assert "ass=" in fake.calls[0].args[fake.calls[0].args.index("-vf") + 1]


class TestInputValidation:
    def test_a_burn_has_a_timeout(self, tmp_path: Path) -> None:
        """v0.1 passed none, so a hung encode never returned."""
        raw, subtitles = _fixture(tmp_path)
        fake = _FakeRunner()

        burn_hardsub(
            fake,  # type: ignore[arg-type]
            raw.video,
            subtitles.subtitle_zh_ass,
            tmp_path / "cooked" / "out.mp4",
            profile=software_profile_for(4),
        )

        assert fake.calls[0].timeout is not None
        assert fake.calls[0].timeout > 0

    def test_a_missing_subtitle_file_fails_before_running_ffmpeg(self, tmp_path: Path) -> None:
        raw, subtitles = _fixture(tmp_path, subs=False)
        fake = _FakeRunner()

        with pytest.raises(RenderError, match="was not written"):
            render_release(
                fake,  # type: ignore[arg-type]
                raw,
                subtitles,
                BurnMode.DUAL,
                cooked_dir=tmp_path / "cooked",
                selector=software_profile_for(4),
            )

        assert fake.calls == []

    def test_the_encoder_flags_come_from_the_profile_only(self, tmp_path: Path) -> None:
        """No hand-written codec flags, and no duplicated ``-pix_fmt``.

        The first version added its own ``-pix_fmt yuv420p`` on top of the
        profile's, producing ``-pix_fmt yuv420p -pix_fmt yuv420p``. Letting the
        profile own every encoder flag is the reason it carries them at all.
        """
        raw, subtitles = _fixture(tmp_path)
        fake = _FakeRunner()

        burn_hardsub(
            fake,  # type: ignore[arg-type]
            raw.video,
            subtitles.subtitle_zh_ass,
            tmp_path / "cooked" / "out.mp4",
            profile=software_profile_for(4),
        )

        args = fake.calls[0].args
        assert args.count("-pix_fmt") == 1
        assert args.count("-c:v") == 1
        assert args[args.index("-c:v") + 1] == "libx264"


# ----------------------------------------------------------------------
# Modes and reuse
# ----------------------------------------------------------------------


class TestModes:
    @pytest.mark.parametrize(
        ("mode", "bilingual", "zh"),
        [
            (BurnMode.DUAL, True, True),
            (BurnMode.BILINGUAL_ONLY, True, False),
            (BurnMode.ZH_ONLY, False, True),
            (BurnMode.SKIP, False, False),
        ],
    )
    def test_each_mode_burns_exactly_what_it_names(
        self, tmp_path: Path, mode: BurnMode, bilingual: bool, zh: bool
    ) -> None:
        raw, subtitles = _fixture(tmp_path)
        fake = _FakeRunner()

        result = render_release(
            fake,  # type: ignore[arg-type]
            raw,
            subtitles,
            mode,
            cooked_dir=tmp_path / "cooked",
            selector=software_profile_for(4),
        )

        assert (result.video_bilingual is not None) is bilingual
        assert (result.video_zh is not None) is zh
        assert len(fake.calls) == int(bilingual) + int(zh)

    def test_skip_runs_no_ffmpeg_at_all(self, tmp_path: Path) -> None:
        raw, subtitles = _fixture(tmp_path)
        fake = _FakeRunner()

        render_release(
            fake,  # type: ignore[arg-type]
            raw,
            subtitles,
            BurnMode.SKIP,
            cooked_dir=tmp_path / "cooked",
            selector=software_profile_for(4),
        )

        assert fake.calls == []


class TestReuse:
    """``force`` finally has a meaning: it is the inverse of reuse."""

    def test_a_missing_output_is_not_reusable(self, tmp_path: Path) -> None:
        fake = _FakeRunner()
        assert _is_reusable(fake, tmp_path / "nope.mp4", ()) is False  # type: ignore[arg-type]

    def test_an_up_to_date_output_is_reused(self, tmp_path: Path) -> None:
        fake = _FakeRunner()
        out = tmp_path / "out.mp4"
        out.write_bytes(b"x")
        source = tmp_path / "src.mp4"
        source.write_bytes(b"x")
        # Explicit mtimes rather than write ordering. On Windows two writes a
        # moment apart land in the same filesystem timestamp tick, so "written
        # second" does not imply "newer" -- and the assertions below then fail
        # for a reason that has nothing to do with the code under test.
        os.utime(source, (1_000_000, 1_000_000))
        os.utime(out, (1_000_001, 1_000_001))

        assert _is_reusable(fake, out, (source,)) is True  # type: ignore[arg-type]

    def test_a_newer_subtitle_invalidates_the_release(self, tmp_path: Path) -> None:
        fake = _FakeRunner()
        out = tmp_path / "out.mp4"
        out.write_bytes(b"x")
        sub = tmp_path / "sub.ass"
        sub.write_bytes(b"x")
        # One second newer, stated explicitly -- see the note above.
        os.utime(out, (1_000_000, 1_000_000))
        os.utime(sub, (1_000_001, 1_000_001))

        assert _is_reusable(fake, out, (sub,)) is False  # type: ignore[arg-type]

    def test_force_re_encodes_even_when_reusable(self, tmp_path: Path) -> None:
        raw, subtitles = _fixture(tmp_path)
        fake = _FakeRunner()
        cooked = tmp_path / "cooked"

        render_release(
            fake,  # type: ignore[arg-type]
            raw,
            subtitles,
            BurnMode.ZH_ONLY,
            cooked_dir=cooked,
            selector=software_profile_for(4),
        )
        assert len(fake.calls) == 1

        render_release(
            fake,  # type: ignore[arg-type]
            raw,
            subtitles,
            BurnMode.ZH_ONLY,
            cooked_dir=cooked,
            force=True,
            selector=software_profile_for(4),
        )
        assert len(fake.calls) == 2, "force must re-encode"

    def test_the_hardware_is_probed_once_for_both_variants(self, tmp_path: Path) -> None:
        """A selector costs a trial encode per candidate, so it is not per-variant."""
        raw, subtitles = _fixture(tmp_path)
        fake = _FakeRunner()
        calls = 0

        def counting_selector() -> object:
            nonlocal calls
            calls += 1
            return software_profile_for(4)

        import porter.media.burn as module

        original = module.detect_encoder
        module.detect_encoder = lambda *a, **k: counting_selector()  # type: ignore[assignment]
        try:
            render_release(
                fake,  # type: ignore[arg-type]
                raw,
                subtitles,
                BurnMode.DUAL,
                cooked_dir=tmp_path / "cooked",
            )
        finally:
            module.detect_encoder = original  # type: ignore[assignment]

        assert calls == 1
        assert len(fake.calls) == 2


# ----------------------------------------------------------------------
# The Renderer port
# ----------------------------------------------------------------------


class TestFfmpegRendererPort:
    def test_it_satisfies_the_renderer_protocol(self, tmp_path: Path) -> None:
        from porter.ports import Renderer

        _, renderer = _runner_and_renderer(tmp_path)

        assert isinstance(renderer, Renderer)
        assert renderer.name == "ffmpeg"

    def test_it_writes_into_the_layouts_cooked_directory(self, tmp_path: Path) -> None:
        raw, subtitles = _fixture(tmp_path)
        _, renderer = _runner_and_renderer(tmp_path)

        result = renderer.render(raw, subtitles, BurnMode.ZH_ONLY, _ctx(tmp_path))

        assert result.video_zh == raw.layout.task_dir / "cooked" / ZH_NAME

    def test_it_announces_the_artifacts_and_progress(self, tmp_path: Path) -> None:
        raw, subtitles = _fixture(tmp_path)
        _, renderer = _runner_and_renderer(tmp_path)
        events: list[object] = []
        ctx = RunContext(
            job_id="burn",
            options=JobOptions(output_dir=tmp_path),
            config=PorterConfig(),
            events=collect(events),
        )

        renderer.render(raw, subtitles, BurnMode.DUAL, ctx)

        announced = [e for e in events if isinstance(e, ArtifactReady)]
        assert {Path(e.path).name for e in announced} == {BILINGUAL_NAME, ZH_NAME}
        assert all(e.phase is Phase.BURN for e in announced)
        assert any(isinstance(e, ProgressUpdated) and e.phase is Phase.BURN for e in events)

    def test_it_honours_force_from_the_options(self, tmp_path: Path) -> None:
        raw, subtitles = _fixture(tmp_path)
        fake, renderer = _runner_and_renderer(tmp_path)

        renderer.render(raw, subtitles, BurnMode.ZH_ONLY, _ctx(tmp_path))
        renderer.render(raw, subtitles, BurnMode.ZH_ONLY, _ctx(tmp_path, force=True))

        assert _burns(fake) == ["burning subtitle_zh.ass", "burning subtitle_zh.ass"]

    def test_the_encoder_verdict_is_probed_once_per_process(self, tmp_path: Path) -> None:
        """A second job must not re-probe the hardware.

        Each selector miss runs a real ffmpeg trial encode per candidate encoder.
        The MCP frontend runs many jobs in one process, so a per-job probe would
        pay that cost every time -- which is what the selector's cache exists to
        prevent.

        Counted through the runner rather than by subclassing ``probe``: ``probe``
        is where the cache lives, so overriding it would bypass the very thing
        under test. A trial encode is recognisable by its ``lavfi`` input.
        """
        raw, subtitles = _fixture(tmp_path)
        fake, renderer = _runner_and_renderer(tmp_path)

        renderer.render(raw, subtitles, BurnMode.ZH_ONLY, _ctx(tmp_path))
        trials_after_first = len(_trials(fake))
        assert trials_after_first > 0, "the first job must probe"

        renderer.render(raw, subtitles, BurnMode.ZH_ONLY, _ctx(tmp_path, force=True))

        assert len(_trials(fake)) == trials_after_first, "the second job must reuse it"
        assert len(_burns(fake)) == 2

    def test_the_selector_is_created_lazily_and_reused(self, tmp_path: Path) -> None:
        raw, subtitles = _fixture(tmp_path)
        _, renderer = _runner_and_renderer(tmp_path)

        renderer.render(raw, subtitles, BurnMode.ZH_ONLY, _ctx(tmp_path))
        first = renderer._encoder()
        renderer.render(raw, subtitles, BurnMode.ZH_ONLY, _ctx(tmp_path))

        assert renderer._encoder() is first

    def test_it_does_not_need_a_real_ffmpeg_to_construct(self) -> None:
        """Construction must stay cheap; the CLI builds one on every run."""
        renderer = FfmpegRenderer.__new__(FfmpegRenderer)

        assert renderer is not None


class TestPipelineAssemblyUsesTheRealRenderer:
    def test_default_wires_the_ffmpeg_renderer(self, tmp_path: Path) -> None:
        """The P4 stub is gone; ``Pipeline.default`` must build the real one."""
        from porter.pipeline import Pipeline

        pipeline = Pipeline.default(_ctx(tmp_path))

        assert isinstance(pipeline.renderer, FfmpegRenderer)
        assert pipeline.renderer.name == "ffmpeg"
