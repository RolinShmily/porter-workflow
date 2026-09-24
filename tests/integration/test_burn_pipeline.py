"""Full pipeline through BURN with real ffmpeg.

The unit tests prove the argv and the control flow; this proves the phase
actually produces two playable release videos when driven through
``Pipeline.run()``, which is the only thing an operator cares about.

The downloader and transcriber are stubs because the real ones need the network
and a working ASR endpoint. Everything from TRANSLATE onward is real: the
translation chain's file writing, the ASS generation, and the ffmpeg burn.

The video is generated with an **apostrophe in its title**, because that is the
case v0.1 could not burn: ``sanitize_filename`` keeps apostrophes, so the task
directory contains one, and v0.1's escaped filtergraph path silently resolved to
a different file. A test with a plain directory name would not have caught it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from porter.config import PorterConfig
from porter.context import RunContext
from porter.events import ArtifactReady, JobState, Phase, collect
from porter.media.burn import BILINGUAL_NAME, ZH_NAME
from porter.media.ffmpeg import FFmpegRunner
from porter.media.probe import probe
from porter.models.materials import RawMaterials, TaskLayout
from porter.models.metadata import VideoMetadata
from porter.models.request import BurnMode, JobOptions, JobRequest
from porter.models.subtitle import SubtitleItem, SubtitleSet
from porter.pipeline import Pipeline

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe not installed",
    ),
]

#: Deliberately contains an apostrophe -- see the module docstring.
TITLE = "It's a Burn Test"

_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 240

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,22,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{text}
"""


def _synthetic_master(dest: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=320x240:d=2:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


class _StubDownloader:
    """Writes a real, playable master into a task directory whose name has an apostrophe."""

    name = "stub"

    def can_handle(self, url: str) -> bool:
        return True

    def probe(self, url: str, ctx: RunContext) -> None:
        raise NotImplementedError

    def fetch(self, url: str, ctx: RunContext) -> RawMaterials:
        layout = TaskLayout.build(ctx.output_root, "burn01", TITLE)
        layout.ensure_dirs()
        assert "'" in layout.task_dir.name, "the apostrophe must survive into the path"

        master = _synthetic_master(layout.raw_dir / "video.mp4")
        audio = layout.raw_dir / "audio.wav"
        audio.write_bytes(b"x")
        return RawMaterials(
            layout=layout,
            video=master,
            audio=audio,
            info=VideoMetadata(
                id="burn01", title=TITLE, safe_title=layout.safe_title, url=url,
                width=320, height=240,
            ),
        )


class _StubTranscriber:
    """Writes the four subtitle files the translate phase expects to find."""

    name = "stub"

    def available(self, ctx: RunContext) -> bool:
        return True

    def transcribe(self, raw: RawMaterials, ctx: RunContext) -> SubtitleSet:
        cooked = raw.layout.cooked_dir
        cooked.mkdir(parents=True, exist_ok=True)
        for name, text in (
            ("subtitle_bilingual.ass", "你好世界 / Hello world"),
            ("subtitle_zh.ass", "你好世界"),
        ):
            (cooked / name).write_text(_ASS.format(text=text), encoding="utf-8")
        return SubtitleSet(
            subtitle_bilingual_srt=cooked / "subtitle_bilingual.srt",
            subtitle_bilingual_ass=cooked / "subtitle_bilingual.ass",
            subtitle_zh_srt=cooked / "subtitle_zh.srt",
            subtitle_zh_ass=cooked / "subtitle_zh.ass",
            transcript_json_path=cooked / "transcript.json",
            transcript_txt_path=cooked / "transcript.txt",
            items=[SubtitleItem(1, 0, 2000, "Hello world", "你好世界")],
            video_width=320,
            video_height=240,
        )


class _PassthroughTranslator:
    """Leaves the stub's subtitle files alone; the renderer is what is under test."""

    name = "stub"

    def available(self, ctx: RunContext) -> bool:
        return True

    def translate(self, subtitles: SubtitleSet, target_lang: str, ctx: RunContext) -> SubtitleSet:
        return subtitles


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        job_id="burn-e2e",
        options=JobOptions(output_dir=tmp_path / "out"),
        config=PorterConfig(),
        events=collect([]),
    )


def _run(ctx: RunContext, tmp_path: Path, mode: BurnMode) -> object:
    pipeline = Pipeline.default(ctx)
    pipeline.downloader = _StubDownloader()
    pipeline.transcriber = _StubTranscriber()
    pipeline.translator = _PassthroughTranslator()
    return pipeline.run(
        JobRequest(url="https://example.com/v", options=JobOptions(
            output_dir=tmp_path / "out", burn=mode
        )),
        ctx,
    )


def test_the_full_pipeline_burns_both_release_videos(ctx: RunContext, tmp_path: Path) -> None:
    """Download (stub) -> transcribe (stub) -> translate (stub) -> burn (real ffmpeg)."""
    result = _run(ctx, tmp_path, BurnMode.DUAL)

    assert result.state is JobState.DONE, result.error
    assert result.burn is not None
    assert result.burn.video_bilingual is not None
    assert result.burn.video_zh is not None

    runner = FFmpegRunner()
    for path in (result.burn.video_bilingual, result.burn.video_zh):
        info = probe(runner, path)
        assert info is not None, f"{path} is not a readable video"
        assert info.has_video and info.has_audio
        assert (info.width, info.height) == (320, 240)
        assert info.duration == pytest.approx(2.0, abs=0.3)


def test_the_release_videos_land_in_cooked_with_the_documented_names(
    ctx: RunContext, tmp_path: Path
) -> None:
    result = _run(ctx, tmp_path, BurnMode.DUAL)

    cooked = result.task_dir / "cooked"
    assert (cooked / BILINGUAL_NAME).is_file()
    assert (cooked / ZH_NAME).is_file()


def test_the_subtitles_are_actually_burned_in(ctx: RunContext, tmp_path: Path) -> None:
    """Not just "the file exists": pixels must differ from the master.

    A burn that silently did nothing would still produce a valid, playable video
    of the right length -- so file existence and ffprobe both pass on a failure.
    Counting non-background pixels is what distinguishes them.
    """
    result = _run(ctx, tmp_path, BurnMode.DUAL)

    def near_white_pixels(video: Path) -> int:
        raw = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-ss", "1", "-i", str(video),
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        return sum(
            1 for i in range(0, len(raw), 3) if raw[i] > 200 and raw[i + 1] > 200 and raw[i + 2] > 200
        )

    master_white = near_white_pixels(result.raw.video)
    burned_white = near_white_pixels(result.burn.video_bilingual)

    assert master_white == 0, "the synthetic master should be a flat colour"
    assert burned_white > 100, "the burned video has no visible subtitle text"


def test_burn_skip_leaves_no_release_videos(ctx: RunContext, tmp_path: Path) -> None:
    """``skip`` removes the phase, so there is no ``BurnResult`` at all.

    Distinct from ``BurnMode`` variants that run and produce nothing: ``skip``
    means the phase is never entered, and reporting an empty ``BurnResult`` would
    imply it ran and found nothing to do.
    """
    result = _run(ctx, tmp_path, BurnMode.SKIP)

    assert result.state is JobState.DONE, result.error
    assert result.burn is None
    assert not list((result.task_dir / "cooked").glob("video_*.mp4"))


def test_zh_only_burns_one_video(ctx: RunContext, tmp_path: Path) -> None:
    result = _run(ctx, tmp_path, BurnMode.ZH_ONLY)

    assert result.state is JobState.DONE, result.error
    assert result.burn.video_zh is not None
    assert result.burn.video_bilingual is None


def test_the_burn_artifacts_are_announced(tmp_path: Path) -> None:
    events: list[object] = []
    ctx = RunContext(
        job_id="burn-e2e",
        options=JobOptions(output_dir=tmp_path / "out"),
        config=PorterConfig(),
        events=collect(events),
    )

    _run(ctx, tmp_path, BurnMode.DUAL)

    videos = [
        e
        for e in events
        if isinstance(e, ArtifactReady) and e.phase is Phase.BURN
    ]
    assert {Path(e.path).name for e in videos} == {BILINGUAL_NAME, ZH_NAME}
    assert all(Path(e.path).is_file() for e in videos)


# ----------------------------------------------------------------------
# The local-file source
# ----------------------------------------------------------------------

LOCAL_TITLE = "It's a Local Test 竖屏"


def _local_source(tmp_path: Path) -> Path:
    """A real video whose directory and filename are full of awkward characters.

    Spaces, an apostrophe and CJK all appear here because each one has broken
    some layer of this pipeline before: the apostrophe defeated v0.1's ffmpeg
    escaping, and the CJK is what makes the task directory need a
    ``sanitize_filename`` pass in the first place.
    """
    source = tmp_path / "我的 视频目录" / f"{LOCAL_TITLE}.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    return _synthetic_master(source)


def _run_local(ctx: RunContext, tmp_path: Path, source: Path, mode: BurnMode) -> object:
    """Real PREPARE (local file) and real BURN; only the network steps are stubs."""
    pipeline = Pipeline.default(ctx)
    # The local preparer is the real one; `Pipeline.default` wires it.
    pipeline.transcriber = _StubTranscriber()
    pipeline.translator = _PassthroughTranslator()
    return pipeline.run(
        JobRequest(
            local_video=source,
            options=JobOptions(output_dir=tmp_path / "out", burn=mode),
        ),
        ctx,
    )


@pytest.fixture
def local_ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        job_id="local-e2e",
        options=JobOptions(output_dir=tmp_path / "out"),
        config=PorterConfig(),
        events=collect([]),
    )


def test_a_local_video_runs_the_whole_pipeline(local_ctx: RunContext, tmp_path: Path) -> None:
    """A file on disk, through PREPARE and BURN, with real ffmpeg at both ends.

    The transcriber and translator are stubs because the network steps need a
    working ASR endpoint and no key-free one exists. Everything that touches the
    file does run for real.
    """
    source = _local_source(tmp_path)

    result = _run_local(local_ctx, tmp_path, source, BurnMode.DUAL)

    assert result.state is JobState.DONE, result.error
    assert result.raw is not None
    assert result.raw.video.name == "video.mp4"
    assert result.raw.audio.name == "audio.wav"
    assert result.raw.cover is not None and result.raw.cover.is_file()
    assert result.burn is not None
    assert result.burn.video_bilingual is not None
    assert result.burn.video_zh is not None


def test_the_local_task_directory_keeps_the_apostrophe(
    local_ctx: RunContext, tmp_path: Path
) -> None:
    """The apostrophe must survive into the path, or this proves nothing.

    v0.1 could not burn a video whose title contained one. If
    ``sanitize_filename`` ever starts stripping them, this test's whole premise
    disappears and it should say so rather than quietly pass.
    """
    source = _local_source(tmp_path)

    result = _run_local(local_ctx, tmp_path, source, BurnMode.ZH_ONLY)

    assert "'" in result.task_dir.name
    assert result.task_dir.name.startswith("local-")


def test_the_local_master_is_standardised_and_probed(local_ctx: RunContext, tmp_path: Path) -> None:
    """The master is measured, not assumed: 320x240 horizontal, 2 seconds."""
    source = _local_source(tmp_path)

    result = _run_local(local_ctx, tmp_path, source, BurnMode.SKIP)

    assert result.raw is not None
    info = probe(FFmpegRunner(), result.raw.video)
    assert info is not None
    assert (info.width, info.height) == (320, 240)
    assert info.duration == pytest.approx(2.0, abs=0.3)
    assert result.raw.info is not None
    assert result.raw.info.is_vertical is False
    assert result.raw.info.platform == "local"


def test_a_vertical_local_video_is_marked_vertical(tmp_path: Path) -> None:
    """Orientation drives the subtitle layout, so it must come from the pixels.

    A vertical video styled with the horizontal layout puts its captions in the
    wrong place, and v0.1's fabricated 1920x1080 default made that
    indistinguishable from a real measurement.
    """
    source = tmp_path / "vertical.mp4"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=navy:s=240x480:d=2:r=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    ctx = RunContext(
        job_id="vertical",
        options=JobOptions(output_dir=tmp_path / "out"),
        config=PorterConfig(),
        events=collect([]),
    )

    result = _run_local(ctx, tmp_path, source, BurnMode.SKIP)

    assert result.raw is not None
    assert (result.raw.info.width, result.raw.info.height) == (240, 480)
    assert result.raw.info.is_vertical is True


def test_the_local_release_videos_have_visible_subtitles(
    local_ctx: RunContext, tmp_path: Path
) -> None:
    """Pixel-level proof for the local source too, not just the URL source."""
    source = _local_source(tmp_path)

    result = _run_local(local_ctx, tmp_path, source, BurnMode.ZH_ONLY)

    def near_white_pixels(video: Path) -> int:
        raw = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-ss", "1", "-i", str(video),
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        return sum(
            1
            for i in range(0, len(raw), 3)
            if raw[i] > 200 and raw[i + 1] > 200 and raw[i + 2] > 200
        )

    assert near_white_pixels(result.raw.video) == 0
    assert near_white_pixels(result.burn.video_zh) > 100


def test_re_running_a_local_video_reuses_the_master(local_ctx: RunContext, tmp_path: Path) -> None:
    """Same file, second run: no re-encode, and no second task directory.

    The identity is derived from the resolved path precisely so this holds; a
    per-run id would re-encode every time and leave a directory behind for each.
    """
    source = _local_source(tmp_path)

    first = _run_local(local_ctx, tmp_path, source, BurnMode.SKIP)
    before = first.raw.video.stat().st_mtime_ns

    second = _run_local(local_ctx, tmp_path, source, BurnMode.SKIP)

    assert second.task_dir == first.task_dir
    assert second.raw.video.stat().st_mtime_ns == before, "the master was re-encoded"
    assert len(list((tmp_path / "out").iterdir())) == 1
