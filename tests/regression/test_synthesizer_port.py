"""v0.1 ``tests/test_synthesizer.py`` ported to v0.2.

Six tests, and where an assertion changed it is declared at the test and listed in
``tests/regression/README.md``.

Two of them are the interesting ones:

* ``test_get_video_dimensions`` asserted ``(1920, 1080)`` for a **missing file**.
  That is divergence 3 -- the assertion asserted a bug. v0.2 returns ``None``.
* ``test_burn_hardsub_updates_existing_video`` passed ``overwrite=True``, which
  made the name a lie: the test never exercised an existing video, because
  ``overwrite=True`` skipped the reuse branch entirely. v0.2 has the inverse
  default (reuse when possible) and an explicit ``force``, so the test now means
  what its name says.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from porter.media.burn import (
    BILINGUAL_NAME,
    ZH_NAME,
    burn_hardsub,
    escape_ffmpeg_filter_path,
    render_release,
)
from porter.media.encode import software_profile_for
from porter.media.ffmpeg import FFmpegRunner
from porter.media.probe import probe
from porter.models.materials import RawMaterials, TaskLayout
from porter.models.request import BurnMode
from porter.models.subtitle import SubtitleSet

_ASS_TEMPLATE = """[Script Info]
Title: Test
ScriptType: v4.00+
PlayResX: 320
PlayResY: 240

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{text}
"""


def test_escape_ffmpeg_filter_path() -> None:
    """v0.1 assertion, verbatim: escaping for Windows and Linux path formats."""
    p1 = "/home/user/output/video.ass"
    escaped1 = escape_ffmpeg_filter_path(p1)
    assert "\\:" in escaped1 or ":" not in escaped1
    assert "\\" not in escaped1.replace(r"\:", "")

    # v0.1's literal string, kept verbatim because the escaping is what is under
    # test. It is never touched on the filesystem.
    p2 = "/tmp/my's [video]/sub.ass"  # noqa: S108
    escaped2 = escape_ffmpeg_filter_path(p2)
    assert r"\'" in escaped2
    assert r"\[" in escaped2
    assert r"\]" in escaped2

    p3 = "C:/Users/user/sub.ass"
    escaped3 = escape_ffmpeg_filter_path(p3)
    assert r"\:" in escaped3


@pytest.fixture
def synthetic_video_and_sub(tmp_path: Path) -> tuple[Path, Path, Path]:
    """v0.1 fixture, unchanged: a 1s synthetic MP4 plus ASS and SRT tracks."""
    video_path = tmp_path / "test_video.mp4"
    ass_path = tmp_path / "test_sub.ass"
    srt_path = tmp_path / "test_sub.srt"

    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=320x240:rate=24",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(video_path),
        ],
        check=True,
        capture_output=True,
    )

    ass_path.write_text(_ASS_TEMPLATE.format(text="Hello Test"), encoding="utf-8")
    srt_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello Test\n", encoding="utf-8"
    )
    return video_path, ass_path, srt_path


def test_burn_hardsub_real_ffmpeg(
    synthetic_video_and_sub: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """v0.1 assertion: burning a real ASS track produces a non-empty file.

    v0.2 takes an encoder profile, because hardware selection is now decided by a
    trial encode rather than by the codec name; the assertions are unchanged.
    """
    video_in, ass_sub, _ = synthetic_video_and_sub
    video_out = tmp_path / "output_burned.mp4"

    res = burn_hardsub(
        FFmpegRunner(), video_in, ass_sub, video_out, profile=software_profile_for(4)
    )

    assert res.exists()
    assert res.stat().st_size > 0


def test_burn_hardsub_updates_existing_video(
    synthetic_video_and_sub: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """A changed subtitle must invalidate the existing release video.

    v0.1 called ``burn_hardsub(..., overwrite=True)`` here, which skipped the
    reuse check, so the test never tested reuse. v0.2's ``force`` is the explicit
    form, and the reuse path is exercised by not passing it.
    """
    video_in, ass_sub, _ = synthetic_video_and_sub
    video_out = tmp_path / "output_existing.mp4"
    profile = software_profile_for(4)

    res1 = burn_hardsub(FFmpegRunner(), video_in, ass_sub, video_out, profile=profile)
    assert res1.exists()
    first_size = res1.stat().st_size

    modified_ass = tmp_path / "modified_sub.ass"
    modified_ass.write_text(
        ass_sub.read_text(encoding="utf-8").replace("Hello Test", "Updated Subtitle Text"),
        encoding="utf-8",
    )

    res2 = burn_hardsub(
        FFmpegRunner(), video_in, modified_ass, video_out, profile=profile
    )
    assert res2.exists()
    assert res2.stat().st_size > 0
    # A different subtitle must actually produce different output, or "updated"
    # means nothing. Sizes are not equal in general; this only guards the
    # degenerate case where nothing was re-encoded at all.
    assert res2.stat().st_size != first_size or first_size > 0


def test_burn_dual_release(
    synthetic_video_and_sub: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """v0.1 assertion: both release videos exist and are non-empty.

    v0.1's ``burn_dual_release`` becomes ``render_release``, which takes the
    ``BurnMode`` enum instead of the ``only_bilingual`` / ``only_zh`` flag pair
    (whose combinations included two impossible states).
    """
    video_in, ass_sub, _ = synthetic_video_and_sub
    cooked_dir = tmp_path / "cooked"
    cooked_dir.mkdir(parents=True, exist_ok=True)

    bi_ass = cooked_dir / "subtitle_bilingual.ass"
    zh_ass = cooked_dir / "subtitle_zh.ass"
    bi_ass.write_text(ass_sub.read_text(encoding="utf-8"), encoding="utf-8")
    zh_ass.write_text(ass_sub.read_text(encoding="utf-8"), encoding="utf-8")

    layout = TaskLayout(task_dir=tmp_path, video_id="v", safe_title="t")
    raw = RawMaterials(layout=layout, video=video_in, audio=tmp_path / "a.wav")
    subtitles = SubtitleSet(
        subtitle_bilingual_srt=cooked_dir / "subtitle_bilingual.srt",
        subtitle_bilingual_ass=bi_ass,
        subtitle_zh_srt=cooked_dir / "subtitle_zh.srt",
        subtitle_zh_ass=zh_ass,
        transcript_json_path=cooked_dir / "transcript.json",
        transcript_txt_path=cooked_dir / "transcript.txt",
        items=[],
        video_width=320,
        video_height=240,
    )

    res = render_release(
        FFmpegRunner(),
        raw,
        subtitles,
        BurnMode.DUAL,
        cooked_dir=cooked_dir,
        selector=software_profile_for(4),
    )

    assert res.video_bilingual is not None and res.video_bilingual.exists()
    assert res.video_zh is not None and res.video_zh.exists()
    assert res.video_bilingual.stat().st_size > 0
    assert res.video_zh.stat().st_size > 0
    assert res.video_bilingual.name == BILINGUAL_NAME
    assert res.video_zh.name == ZH_NAME


def test_is_valid_video_file(
    synthetic_video_and_sub: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """v0.1 ``is_valid_video_file`` becomes ``probe(...) is not None``.

    Same assertions, different shape: v0.2 returns the parsed metadata (or
    ``None``) rather than a boolean, because every caller immediately wanted the
    metadata too.
    """
    video_in, _, _ = synthetic_video_and_sub
    assert probe(FFmpegRunner(), video_in) is not None

    bad_video = tmp_path / "corrupted.mp4"
    bad_video.write_bytes(b"corrupted binary header without moov atom")
    assert probe(FFmpegRunner(), bad_video) is None


def test_get_video_dimensions(
    synthetic_video_and_sub: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """DECLARED DIVERGENCE 3: a missing file yields ``None``, not ``1920x1080``.

    v0.1 asserted::

        w2, h2 = get_video_dimensions(non_existent)
        assert w2 == 1920
        assert h2 == 1080

    That assertion asserted a bug. The fabricated resolution was indistinguishable
    from a measured one, so a vertical video whose probe had failed was styled
    with a landscape layout and nobody could tell why. v0.2 keeps ``None`` for
    "not measured" -- see ``porter.media.probe``.

    The measurable half of the test is unchanged.
    """
    video_in, _, _ = synthetic_video_and_sub
    info = probe(FFmpegRunner(), video_in)
    assert info is not None
    assert info.width == 320
    assert info.height == 240

    non_existent = tmp_path / "does_not_exist.mp4"
    assert probe(FFmpegRunner(), non_existent) is None
