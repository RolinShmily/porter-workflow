"""End-to-end media tests against a real ffmpeg.

Slow, and skipped when ffmpeg is absent. These exist because the media layer's
failure modes are not visible to mocks: a wrong pixel format, a lost audio
stream, or a mis-set sample rate all "succeed" with a non-zero exit code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from porter.config import FFmpegConfig
from porter.media.enhance import enhance_for_asr
from porter.media.ffmpeg import FFmpegRunner, FFmpegTools
from porter.media.probe import dimensions, is_valid_video, probe
from porter.media.standardize import extract_audio, standardize_video

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe not installed",
    ),
]


@pytest.fixture(scope="module")
def runner() -> FFmpegRunner:
    return FFmpegRunner(FFmpegTools.resolve())


def _make_video(
    dest: Path,
    *,
    size: str = "320x240",
    codec: str = "libx264",
    seconds: float = 1.0,
    with_audio: bool = True,
) -> Path:
    """Synthesise a tiny video. No network, no fixtures on disk."""
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate=10:duration={seconds}",
    ]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += [
        "-c:v",
        codec,
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
    ]
    if with_audio:
        args += ["-c:a", "aac", "-shortest"]
    args += [str(dest)]

    subprocess.run(args, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)
    return dest


class TestProbe:
    def test_reads_dimensions_and_codecs(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        video = _make_video(tmp_path / "src.mp4")
        info = probe(runner, video)

        assert info is not None
        assert (info.width, info.height) == (320, 240)
        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"
        assert info.duration and 0.5 < info.duration < 2.0
        assert info.is_playable
        assert info.is_stream_copyable

    def test_vertical_video_reports_vertical(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        video = _make_video(tmp_path / "portrait.mp4", size="240x426")
        info = probe(runner, video)
        assert info is not None
        assert info.is_vertical is True

    def test_missing_file_is_none_not_an_exception(
        self, runner: FFmpegRunner, tmp_path: Path
    ) -> None:
        assert probe(runner, tmp_path / "nope.mp4") is None

    def test_truncated_file_is_not_valid(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        """v0.1's resumption check trusted size alone."""
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"\x00" * 4096)
        assert probe(runner, broken) is None
        assert is_valid_video(runner, broken) is False

    def test_dimensions_returns_none_rather_than_1920x1080(
        self, runner: FFmpegRunner, tmp_path: Path
    ) -> None:
        """v0.1 fabricated a resolution, which then selected the wrong styling."""
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"\x00" * 4096)
        assert dimensions(runner, broken) is None


class TestStandardize:
    def test_stream_copy_path_keeps_the_codecs(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        source = _make_video(tmp_path / "src.mp4")
        dest = tmp_path / "video.mp4"

        info = standardize_video(runner, source, dest)

        assert dest.is_file()
        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"
        assert info.has_audio

    def test_transcode_path_produces_h264_aac(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        """A non-H.264 source must be re-encoded into the standard master."""
        source = _make_video(tmp_path / "src.avi", codec="mpeg4")
        dest = tmp_path / "video.mp4"

        info = standardize_video(runner, source, dest, config=FFmpegConfig(preset="ultrafast"))

        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"

    def test_master_has_exactly_one_video_and_one_audio_stream(
        self, runner: FFmpegRunner, tmp_path: Path
    ) -> None:
        """Explicit -map: v0.1's default -map 0 copied every stream."""
        # A source with two audio tracks.
        source = tmp_path / "multi.mkv"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=0.5",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                "-f", "lavfi", "-i", "sine=frequency=880:duration=0.5",
                "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(source),
            ],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        )

        before = runner.probe_streams(source)
        assert len([s for s in before if s.get("codec_type") == "audio"]) == 2

        dest = tmp_path / "video.mp4"
        standardize_video(runner, source, dest, config=FFmpegConfig(preset="ultrafast"))

        after = runner.probe_streams(dest)
        assert len([s for s in after if s.get("codec_type") == "video"]) == 1
        assert len([s for s in after if s.get("codec_type") == "audio"]) == 1

    def test_master_is_faststart(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        """The moov atom must be at the front, so playback can start immediately."""
        source = _make_video(tmp_path / "src.mp4")
        dest = tmp_path / "video.mp4"
        standardize_video(runner, source, dest)

        head = dest.read_bytes()[:4096]
        assert b"moov" in head

    def test_unreadable_source_raises(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        from porter.errors import MediaError

        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"\x00" * 4096)
        with pytest.raises(MediaError):
            standardize_video(runner, broken, tmp_path / "out.mp4")

    def test_video_without_audio_still_standardises(
        self, runner: FFmpegRunner, tmp_path: Path
    ) -> None:
        """`-map 0:a:0?` is optional, so a silent source must not fail."""
        source = _make_video(tmp_path / "silent.mp4", with_audio=False)
        info = standardize_video(runner, source, tmp_path / "video.mp4")
        assert info.has_video
        assert not info.has_audio


class TestAudioExtraction:
    def test_is_16k_mono_s16le(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        video = _make_video(tmp_path / "src.mp4")
        wav = extract_audio(runner, video, tmp_path / "audio.wav")

        data = runner.probe_json(
            wav,
            ["-show_entries", "stream=sample_rate,channels,codec_name"],
        )
        stream = data["streams"][0]
        assert stream["sample_rate"] == "16000"
        assert stream["channels"] == 1
        assert stream["codec_name"] == "pcm_s16le"

    def test_respects_a_custom_sample_rate(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        video = _make_video(tmp_path / "src.mp4")
        wav = extract_audio(runner, video, tmp_path / "audio.wav", sample_rate=22050)
        data = runner.probe_json(wav, ["-show_entries", "stream=sample_rate"])
        assert data["streams"][0]["sample_rate"] == "22050"


class TestEnhancement:
    def test_produces_a_usable_wav(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        video = _make_video(tmp_path / "src.mp4")
        audio = extract_audio(runner, video, tmp_path / "audio.wav")

        enhanced = enhance_for_asr(runner, audio, tmp_path / "audio_enhanced.wav")

        assert enhanced is not None
        assert enhanced.stat().st_size > 0
        data = runner.probe_json(enhanced, ["-show_entries", "stream=sample_rate,channels"])
        assert data["streams"][0]["sample_rate"] == "16000"
        assert data["streams"][0]["channels"] == 1

    def test_missing_input_returns_none(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        assert enhance_for_asr(runner, tmp_path / "nope.wav", tmp_path / "out.wav") is None

    def test_empty_input_returns_none(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        empty = tmp_path / "empty.wav"
        empty.write_bytes(b"")
        assert enhance_for_asr(runner, empty, tmp_path / "out.wav") is None

    def test_failure_is_not_fatal(self, runner: FFmpegRunner, tmp_path: Path) -> None:
        """Enhancement is an optimisation; losing it must not lose the transcript."""
        not_audio = tmp_path / "garbage.wav"
        not_audio.write_bytes(b"not audio at all" * 100)
        assert enhance_for_asr(runner, not_audio, tmp_path / "out.wav") is None


class TestStdinIsolation:
    """The MCP stdio transport owns stdin; prove ffmpeg leaves it alone.

    Measured reality: ffmpeg gates interactive stdin on ``tcgetattr(0)``, which
    fails for a pipe, so on Linux it does not read fd 0 even without
    ``-nostdin``. ``-nostdin`` plus ``DEVNULL`` is therefore defence in depth
    (Windows has no ``tcgetattr``, and a client may supply a pty) rather than a
    fix for a live corruption. This test pins the observed behaviour either way:
    if a future ffmpeg starts consuming piped stdin, it fails here.
    """

    _CHILD = (
        "import subprocess, sys\n"
        "subprocess.run(sys.argv[1:], capture_output=True, text=True, check=False)\n"
        "sys.stdout.write(sys.stdin.read())\n"
    )

    def test_ffmpeg_does_not_consume_the_parent_stdin(self, tmp_path: Path) -> None:
        sentinel = "SENTINEL-0123456789"
        read_fd, write_fd = os.pipe()
        os.write(write_fd, sentinel.encode())
        os.close(write_fd)

        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    self._CHILD,
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:d=0.05",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                stdin=read_fd,
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            os.close(read_fd)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == sentinel, (
            "ffmpeg consumed bytes from stdin; in the MCP frontend those bytes "
            "are JSON-RPC frames"
        )
