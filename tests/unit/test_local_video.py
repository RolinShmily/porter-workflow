"""PREPARE for a local video file.

The real-ffmpeg path is covered by ``tests/integration/test_burn_pipeline.py``.
This file covers the decisions a fake can prove: which file is accepted, what the
task directory is called, what metadata is written, and where the pipeline sends a
request.

The identity tests are the important ones. A URL arrives with a video id, so
re-running a job finds the same task directory for free. A path does not, and
getting that wrong means a re-run silently starts a second task directory and
re-downloads nothing while re-encoding everything.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from porter.config import PorterConfig
from porter.context import RunContext
from porter.errors import ExtractionError
from porter.events import ArtifactReady, Phase, collect
from porter.media.prepare import (
    AUDIO_NAME,
    COVER_NAME,
    METADATA_NAME,
    VIDEO_NAME,
)
from porter.models.request import JobOptions, JobRequest
from porter.platforms.local import VIDEO_SUFFIXES, LocalFileDownloader, local_video_id

SRC = Path("/tmp/somewhere/My Video.mp4")  # noqa: S108 - a string under test, never touched

#: Paths passed to ``JobRequest.from_source``. Pure strings: classification must
#: not touch the filesystem, so these are deliberately not real files.
_LOCAL_PATH_SOURCES = [
    "/tmp/v.mp4",  # noqa: S108 - a string under test, never touched
    "./v.mp4",
    "v.mp4",
    "/tmp/My Video.mp4",  # noqa: S108 - ditto
    "C:/v.mp4",
]


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def video(tmp_path: Path) -> Path:
    """A real file with a video suffix, in its own directory.

    The subdirectory matters: ``ctx`` writes to ``tmp_path/out``, so a video
    directly in ``tmp_path`` would share its parent with the output and the
    "does not write next to the source" test could not tell them apart.
    """
    path = tmp_path / "src" / "My Video.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 4096)
    return path


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        job_id="local",
        options=JobOptions(output_dir=tmp_path / "out"),
        config=PorterConfig(),
    )


class FakeRunner:
    """An ffmpeg runner that writes plausible files instead of encoding.

    This is the *real* boundary, so everything above it runs for real: the
    standardise branch, the resumption check, the cover-frame logic and the
    events it emits, the ``audio_denoise`` flag. Stubbing
    ``standardize_master`` instead would have been less code but would have
    replaced the logic under test -- and the first draft did exactly that, which
    is why the cover event went missing from an assertion that was checking the
    producer, not the stub.

    ``probe_streams`` / ``probe_format`` answer ``ffprobe``, so
    :func:`~porter.media.probe.probe` and therefore ``is_valid_video`` and the
    metadata step are the real implementations too.
    """

    #: Above ``probe.MIN_VIDEO_BYTES``, or every file is treated as unreadable.
    FILE_SIZE = 4096

    def __init__(
        self,
        *,
        width: int = 1080,
        height: int = 1920,
        duration: float = 12.5,
        decodable: bool = True,
    ) -> None:
        self.width = width
        self.height = height
        self.duration = duration
        #: When False, ffprobe reports nothing, so ``probe`` returns ``None``.
        self.decodable = decodable
        self.encoded: list[str] = []
        self.cover_requests = 0

    def run(
        self,
        args: list[str],
        *,
        what: str,
        check: bool = True,
        timeout: float | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.encoded.append(what)
        if "cover frame" in what:
            self.cover_requests += 1
        # `-` means stdout (the trial encode uses `-f null -`); writing to it
        # would create a file named "-" in the repository root.
        dest = Path(args[-1])
        if str(dest) != "-":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\x00" * self.FILE_SIZE)
        return subprocess.CompletedProcess(args, 0, "", "")

    def probe_streams(self, path: Path) -> list[dict[str, object]]:
        if not self.decodable:
            return []
        return [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": self.width,
                "height": self.height,
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ]

    def probe_format(self, path: Path) -> dict[str, object]:
        if not self.decodable:
            return {}
        return {"duration": str(self.duration), "format_name": "mov,mp4,m4a"}

    def encode_count(self) -> int:
        """How many times the master was actually produced."""
        return sum(1 for what in self.encoded if "standardis" in what)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------


class TestLocalVideoIdentity:
    """A path has no id, so one is derived. It has to be stable and unique."""

    def test_the_id_is_stable_for_the_same_path(self, tmp_path: Path) -> None:
        path = tmp_path / "v.mp4"
        path.write_bytes(b"x")

        assert local_video_id(path) == local_video_id(path)

    def test_the_id_does_not_depend_on_how_the_path_was_spelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``./v.mp4``, ``v.mp4`` and the absolute path are the same video.

        This is what makes a re-run from a different working directory resume the
        same task directory instead of starting a second one.
        """
        path = tmp_path / "v.mp4"
        path.write_bytes(b"x")
        absolute = local_video_id(path)

        monkeypatch.chdir(tmp_path)
        assert local_video_id(Path("./v.mp4")) == absolute
        assert local_video_id(Path("v.mp4")) == absolute

    def test_two_files_with_the_same_name_do_not_collide(self, tmp_path: Path) -> None:
        """Same filename, different directories: different tasks.

        Using the stem as the id would merge them and the second video would
        silently reuse the first one's master.
        """
        a = tmp_path / "a" / "video.mp4"
        b = tmp_path / "b" / "video.mp4"
        for path in (a, b):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

        assert local_video_id(a) != local_video_id(b)

    def test_the_id_is_visibly_not_a_platform_id(self) -> None:
        assert local_video_id(SRC).startswith("local-")

    def test_the_id_is_filesystem_safe(self) -> None:
        """It goes straight into a directory name."""
        ident = local_video_id(Path("/tmp/我的 视频/it's [odd].mp4"))  # noqa: S108

        assert ident.isascii()
        assert not set(ident) & set("/\\:*?\"<>|'[] ")


# ----------------------------------------------------------------------
# Which source is this?
# ----------------------------------------------------------------------


class TestJobRequestFromSource:
    """One positional argument, classified by rule rather than by the user."""

    @pytest.mark.parametrize(
        "source",
        [
            "https://www.youtube.com/watch?v=abc",
            "https://x.com/user/status/1",
            "http://example.com/v.mp4",
            "ftp://example.com/v.mp4",
        ],
    )
    def test_anything_with_a_scheme_is_a_url(self, source: str) -> None:
        """Including an unsupported scheme.

        Treating ``ftp://...`` as a path would produce "file not found:
        ftp://...", which hides the real answer (no extractor claims it).
        """
        request = JobRequest.from_source(source, JobOptions())

        assert request.url == source
        assert request.local_video is None

    @pytest.mark.parametrize("source", _LOCAL_PATH_SOURCES)
    def test_anything_without_a_scheme_is_a_local_path(self, source: str) -> None:
        request = JobRequest.from_source(source, JobOptions())

        assert request.local_video == Path(source)
        assert request.url is None

    def test_a_file_url_becomes_a_path(self) -> None:
        """``file://`` is an explicit local file, not a URL to dispatch."""
        request = JobRequest.from_source("file:///tmp/My%20Video.mp4", JobOptions())

        assert request.local_video == Path("/tmp/My Video.mp4")  # noqa: S108
        assert request.url is None

    def test_a_file_url_with_escaped_characters_is_decoded(self) -> None:
        request = JobRequest.from_source(
            "file:///tmp/%E6%88%91%E7%9A%84/it%27s.mp4", JobOptions()
        )

        assert request.local_video == Path("/tmp/我的/it's.mp4")  # noqa: S108

    def test_existence_is_not_checked(self) -> None:
        """Classification must not touch the filesystem.

        A path that does not exist is still a path; the error belongs to the
        producer, which can name the resolved path.
        """
        request = JobRequest.from_source("/nope/missing.mp4", JobOptions())

        assert request.local_video == Path("/nope/missing.mp4")


# ----------------------------------------------------------------------
# Input validation
# ----------------------------------------------------------------------


class TestInputValidation:
    """Every refusal names the path, because the caller may be a remote client."""

    @pytest.mark.parametrize("suffix", sorted(VIDEO_SUFFIXES))
    def test_the_known_video_suffixes_are_accepted(self, tmp_path: Path, suffix: str) -> None:
        path = tmp_path / f"v{suffix}"
        path.write_bytes(b"x")

        assert LocalFileDownloader._validate(path) == path.resolve()

    @pytest.mark.parametrize("suffix", [".Mp4", ".MP4", ".MkV"])
    def test_the_extension_check_ignores_case(self, tmp_path: Path, suffix: str) -> None:
        path = tmp_path / f"v{suffix}"
        path.write_bytes(b"x")

        assert LocalFileDownloader._validate(path) == path.resolve()

    def test_a_missing_file_is_refused_with_the_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.mp4"

        with pytest.raises(ExtractionError) as excinfo:
            LocalFileDownloader._validate(missing)

        assert str(missing) in str(excinfo.value)
        assert excinfo.value.details["path"] == str(missing)

    def test_a_directory_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ExtractionError, match="is a directory"):
            LocalFileDownloader._validate(tmp_path)

    def test_an_empty_file_is_refused(self, tmp_path: Path) -> None:
        """An empty file would reach ffmpeg and fail with a decoder message."""
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")

        with pytest.raises(ExtractionError, match="is empty"):
            LocalFileDownloader._validate(empty)

    def test_a_wrong_extension_is_refused_before_ffmpeg(self, tmp_path: Path) -> None:
        """The message must say what *is* accepted.

        Without the allowlist a typo like ``video.txt`` reaches ffmpeg and the
        error is about a missing decoder, which says nothing about the real
        problem.
        """
        notes = tmp_path / "notes.txt"
        notes.write_text("not a video", encoding="utf-8")

        with pytest.raises(ExtractionError) as excinfo:
            LocalFileDownloader._validate(notes)

        assert ".txt" in str(excinfo.value)
        assert ".mp4" in excinfo.value.details["supported"]

    def test_a_tilde_path_is_expanded(self, tmp_path: Path, monkeypatch) -> None:
        # Both variables, because the platform consults only one of them:
        # ``expanduser`` reads USERPROFILE on Windows and HOME elsewhere. The test
        # is about the product expanding ``~``, not about which variable it asks.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")

        assert LocalFileDownloader._validate(Path("~/v.mp4")) == video.resolve()

    def test_the_validation_does_not_decode(self, tmp_path: Path) -> None:
        """Garbage with a video suffix passes validation and fails later.

        Validation is about *which file*, not *whether it decodes*: the decoder
        check costs an ffprobe and belongs to the metadata step, where the
        message can talk about the file's contents.
        """
        junk = tmp_path / "junk.mp4"
        junk.write_bytes(b"definitely not an mp4")

        assert LocalFileDownloader._validate(junk) == junk.resolve()


# ----------------------------------------------------------------------
# prepare()
# ----------------------------------------------------------------------


class TestPrepare:
    @pytest.mark.usefixtures("runner")
    def test_it_produces_the_documented_raw_layout(
        self, video: Path, ctx: RunContext, runner: FakeRunner
    ) -> None:
        materials = LocalFileDownloader().prepare(video, ctx, runner=runner)

        assert materials.video.name == VIDEO_NAME
        assert materials.audio.name == AUDIO_NAME
        assert materials.cover is not None and materials.cover.name == COVER_NAME
        assert materials.metadata_path is not None
        assert materials.metadata_path.name == METADATA_NAME
        assert materials.raw_dir == materials.layout.task_dir / "raw"
        for path in (materials.video, materials.audio, materials.cover):
            assert path.is_file()

    @pytest.mark.usefixtures("runner")
    def test_the_task_directory_uses_the_stem_and_the_derived_id(
        self, video: Path, ctx: RunContext, runner: FakeRunner
    ) -> None:
        materials = LocalFileDownloader().prepare(video, ctx, runner=runner)

        assert materials.layout.video_id == local_video_id(video)
        assert materials.layout.task_dir.name.startswith(local_video_id(video))
        assert "My_Video" in materials.layout.task_dir.name

    @pytest.mark.usefixtures("runner")
    def test_it_does_not_write_next_to_the_source(
        self, video: Path, ctx: RunContext, runner: FakeRunner
    ) -> None:
        """The user's own directory must stay untouched.

        Writing the task folder beside the video would surprise the user and fail
        outright on a read-only mount.
        """
        before = sorted(p.name for p in video.parent.iterdir())

        LocalFileDownloader().prepare(video, ctx, runner=runner)

        assert sorted(p.name for p in video.parent.iterdir()) == before

    @pytest.mark.usefixtures("runner")
    def test_metadata_records_the_measured_geometry(
        self, video: Path, ctx: RunContext, runner: FakeRunner
    ) -> None:
        """A vertical video must be marked vertical, or the ASS layout is wrong."""
        materials = LocalFileDownloader().prepare(video, ctx, runner=runner)
        metadata = json.loads(materials.metadata_path.read_text(encoding="utf-8"))

        assert metadata["platform"] == "local"
        assert metadata["width"] == 1080
        assert metadata["height"] == 1920
        assert metadata["is_vertical"] is True
        assert metadata["duration"] == 12.5
        assert metadata["title"] == "My Video"
        assert metadata["url"].startswith("file://")

    @pytest.mark.usefixtures("runner")
    def test_metadata_keeps_the_readable_source_path(
        self, video: Path, ctx: RunContext, runner: FakeRunner
    ) -> None:
        """``url`` is percent-encoded; the human-readable path is kept too.

        Debugging a job means correlating it with a file the user named, and
        ``%E6%88%91`` is not that.
        """
        materials = LocalFileDownloader().prepare(video, ctx, runner=runner)
        metadata = json.loads(materials.metadata_path.read_text(encoding="utf-8"))

        assert metadata["raw_metadata"]["source_path"] == str(video.resolve())

    @pytest.mark.usefixtures("runner")
    def test_no_sidecar_subtitles_are_picked_up(
        self, video: Path, ctx: RunContext, runner: FakeRunner
    ) -> None:
        """A sibling .srt could be the source or the translation.

        Guessing wrong would either skip ASR or overwrite the user's file, so
        neither is claimed.
        """
        video.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

        materials = LocalFileDownloader().prepare(video, ctx, runner=runner)

        assert materials.subtitle_src is None
        assert materials.subtitle_zh is None

    @pytest.mark.usefixtures("runner")
    def test_the_artifacts_are_announced(
        self, video: Path, tmp_path: Path, runner: FakeRunner
    ) -> None:
        events: list[object] = []
        ctx = RunContext(
            job_id="local",
            options=JobOptions(output_dir=tmp_path / "out"),
            config=PorterConfig(),
            events=collect(events),
        )

        LocalFileDownloader().prepare(video, ctx, runner=runner)

        announced = [e for e in events if isinstance(e, ArtifactReady)]
        assert all(e.phase is Phase.PREPARE for e in announced)
        assert any(e.path.name == VIDEO_NAME for e in announced)
        assert any(e.path.name == COVER_NAME for e in announced)

    @pytest.mark.usefixtures("runner")
    def test_tmp_is_cleaned_up(
        self, video: Path, ctx: RunContext, runner: FakeRunner
    ) -> None:
        materials = LocalFileDownloader().prepare(video, ctx, runner=runner)

        assert not materials.layout.tmp_dir.exists()

    @pytest.mark.usefixtures("runner")
    def test_no_denoise_skips_the_enhanced_track(
        self, video: Path, tmp_path: Path, runner: FakeRunner
    ) -> None:
        ctx = RunContext(
            job_id="local",
            options=JobOptions(output_dir=tmp_path / "out", audio_denoise=False),
            config=PorterConfig(),
        )

        materials = LocalFileDownloader().prepare(video, ctx, runner=runner)

        assert materials.audio_enhanced is None

    @pytest.mark.usefixtures("runner")
    def test_an_undecodable_file_fails_before_standardising(
        self, video: Path, ctx: RunContext
    ) -> None:
        """Not decodable is a different error from not found."""
        runner = FakeRunner(decodable=False)

        with pytest.raises(ExtractionError, match="not decodable"):
            LocalFileDownloader().prepare(video, ctx, runner=runner)

        assert runner.encoded == [], "ffmpeg must not run on an unreadable file"

    @pytest.mark.usefixtures("runner")
    def test_it_satisfies_the_local_preparer_protocol(self) -> None:
        from porter.ports import LocalPreparer

        assert isinstance(LocalFileDownloader(), LocalPreparer)
        assert LocalFileDownloader.name == "local"


class TestResumption:
    """A second run must reuse the master rather than re-encode a long video."""

    @pytest.mark.usefixtures("runner")
    def test_a_complete_master_is_reused(
        self, video: Path, ctx: RunContext, runner: FakeRunner
    ) -> None:
        downloader = LocalFileDownloader()
        downloader.prepare(video, ctx, runner=runner)
        assert runner.encode_count() == 1

        downloader.prepare(video, ctx, runner=runner)

        assert runner.encode_count() == 1, "the master should be reused"

    def test_force_re_encodes(
        self, video: Path, tmp_path: Path, runner: FakeRunner
    ) -> None:
        out = tmp_path / "out"
        options = JobOptions(output_dir=out)
        LocalFileDownloader().prepare(
            video,
            RunContext(job_id="a", options=options, config=PorterConfig()),
            runner=runner,
        )

        LocalFileDownloader().prepare(
            video,
            RunContext(
                job_id="b",
                options=JobOptions(output_dir=out, force=True),
                config=PorterConfig(),
            ),
            runner=runner,
        )

        assert runner.encode_count() == 2, "force must re-encode"

    @pytest.mark.usefixtures("runner")
    def test_a_zero_byte_audio_file_is_not_reused(
        self, video: Path, ctx: RunContext, runner: FakeRunner
    ) -> None:
        """The v0.1 defect: size alone accepted a truncated master."""
        downloader = LocalFileDownloader()
        materials = downloader.prepare(video, ctx, runner=runner)
        materials.audio.write_bytes(b"")

        downloader.prepare(video, ctx, runner=runner)

        assert runner.encode_count() == 2, "an empty audio track must not be reused"


# ----------------------------------------------------------------------
# Pipeline dispatch
# ----------------------------------------------------------------------


class TestPipelineDispatch:
    """PREPARE must route by source kind, and the request is what says which."""

    def test_a_local_request_goes_to_the_local_preparer(self, tmp_path: Path) -> None:
        from porter.pipeline import Pipeline

        seen: list[Path] = []

        class SpyLocal:
            name = "spy-local"

            def prepare(self, path, ctx):
                seen.append(path)
                raise _RouteReachedError

        class ExplodingDownloader:
            name = "exploding"

            def can_handle(self, url):
                raise AssertionError("the downloader must not be consulted")

            probe = fetch = can_handle

        ctx = RunContext(
            job_id="d", options=JobOptions(output_dir=tmp_path), config=PorterConfig()
        )
        pipeline = Pipeline.default(ctx, local=SpyLocal())
        pipeline.downloader = ExplodingDownloader()  # type: ignore[assignment]

        with pytest.raises(_RouteReachedError):
            pipeline.prepare(JobRequest(local_video=Path("/tmp/v.mp4"), options=ctx.options), ctx)  # noqa: S108

        assert seen == [Path("/tmp/v.mp4")]  # noqa: S108

    def test_a_url_request_goes_to_the_downloader(self, tmp_path: Path) -> None:
        from porter.pipeline import Pipeline

        seen: list[str] = []

        class SpyDownloader:
            name = "spy-url"

            def can_handle(self, url):
                return True

            def probe(self, url, ctx):
                raise AssertionError("prepare must not probe")

            def fetch(self, url, ctx):
                seen.append(url)
                raise _RouteReachedError

        class ExplodingLocal:
            name = "exploding-local"

            def prepare(self, path, ctx):
                raise AssertionError("the local preparer must not be consulted")

        ctx = RunContext(
            job_id="d", options=JobOptions(output_dir=tmp_path), config=PorterConfig()
        )
        pipeline = Pipeline.default(ctx, downloader=SpyDownloader())  # type: ignore[arg-type]
        pipeline.local = ExplodingLocal()  # type: ignore[assignment]

        with pytest.raises(_RouteReachedError):
            pipeline.prepare(JobRequest(url="https://example.com/v", options=ctx.options), ctx)

        assert seen == ["https://example.com/v"]

    def test_default_wires_the_local_preparer(self, tmp_path: Path) -> None:
        from porter.pipeline import Pipeline
        from porter.platforms.local import LocalFileDownloader
        from porter.ports import LocalPreparer

        ctx = RunContext(
            job_id="d", options=JobOptions(output_dir=tmp_path), config=PorterConfig()
        )

        pipeline = Pipeline.default(ctx)

        assert isinstance(pipeline.local, LocalFileDownloader)
        assert isinstance(pipeline.local, LocalPreparer)


class _RouteReachedError(Exception):
    """Stops a spy mid-call so the assertion is about the routing, not the result."""
