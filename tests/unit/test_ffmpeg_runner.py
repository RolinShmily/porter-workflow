"""Process-safety and error-reporting contract for the ffmpeg boundary.

``ffmpeg`` is the engine's highest-risk subprocess: it runs for hours, it can
consume stdin, and its most useful diagnostic is on the *last* line of stderr.
Each of those is asserted here rather than trusted to reviewer memory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from porter.errors import CapabilityMissingError, MediaError
from porter.media.ffmpeg import FFmpegRunner, FFmpegTools


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every subprocess.run call instead of executing it."""
    calls: list[dict[str, Any]] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


@pytest.fixture
def runner() -> FFmpegRunner:
    return FFmpegRunner(FFmpegTools(ffmpeg="ffmpeg", ffprobe="ffprobe"))


class TestStdinIsolation:
    """In the MCP frontend stdin carries JSON-RPC; ffmpeg must not read it."""

    def test_nostdin_is_in_every_ffmpeg_argv(self, runner: FFmpegRunner, recorded) -> None:
        runner.run(["-i", "in.mp4", "out.mp4"], what="a test")
        assert "-nostdin" in recorded[0]["cmd"]

    def test_nostdin_comes_before_the_caller_arguments(
        self, runner: FFmpegRunner, recorded
    ) -> None:
        """Global options must precede inputs, or ffmpeg may ignore them."""
        runner.run(["-i", "in.mp4"], what="a test")
        assert recorded[0]["cmd"].index("-nostdin") < recorded[0]["cmd"].index("-i")

    def test_stdin_is_devnull(self, runner: FFmpegRunner, recorded) -> None:
        """The belt to -nostdin's braces: the child never inherits fd 0."""
        runner.run(["-i", "in.mp4"], what="a test")
        assert recorded[0]["stdin"] is subprocess.DEVNULL

    def test_probe_also_is_isolated(self, runner: FFmpegRunner, recorded) -> None:
        runner.probe_streams(Path("movie.mp4"))
        assert recorded[0]["stdin"] is subprocess.DEVNULL


class TestErrorReporting:
    """v0.1 sliced stderr from the front, so it reported the version banner."""

    def test_error_uses_the_tail_of_stderr(
        self, runner: FFmpegRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        banner = "ffmpeg version 9.0.1 Copyright (c) 2000-2025 the FFmpeg developers\n"
        cause = "Unknown encoder 'h264_nvenc'"

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=banner * 20 + cause)

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(MediaError) as excinfo:
            runner.run(["-i", "x.mp4"], what="burning subtitles")

        assert cause in str(excinfo.value)
        assert excinfo.value.details["what"] == "burning subtitles"
        assert excinfo.value.details["exit_code"] == 1

    def test_long_stderr_is_truncated_from_the_front(self, runner: FFmpegRunner) -> None:
        from porter.media.ffmpeg import _tail

        tail = _tail("A" * 5000 + "THE_CAUSE")
        assert tail.endswith("THE_CAUSE")
        assert len(tail) < 5000
        assert tail.startswith("...")

    def test_short_stderr_is_kept_whole(self) -> None:
        from porter.media.ffmpeg import _tail

        assert _tail("short") == "short"

    def test_none_stderr_is_empty(self) -> None:
        from porter.media.ffmpeg import _tail

        assert _tail(None) == ""

    def test_check_false_returns_the_failure_instead_of_raising(
        self, runner: FFmpegRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        monkeypatch.setattr(subprocess, "run", fake_run)
        proc = runner.run(["-i", "x"], what="optional step", check=False)
        assert proc.returncode == 1

    def test_missing_binary_becomes_a_capability_error(
        self, runner: FFmpegRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(CapabilityMissingError) as excinfo:
            runner.run(["-i", "x"], what="a test")

        assert excinfo.value.capability == "ffmpeg"
        assert "not installed" in str(excinfo.value)

    def test_timeout_becomes_a_media_error(
        self, runner: FFmpegRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(MediaError, match="timed out"):
            runner.run(["-i", "x"], what="probing", timeout=5)

    def test_no_output_stream_is_ever_touched(
        self, runner: FFmpegRunner, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="some error")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(MediaError):
            runner.run(["-i", "x"], what="a test")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestProbeJson:
    def test_returns_empty_dict_on_failure(self, runner: FFmpegRunner, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="nope")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert runner.probe_json(Path("x.mp4"), []) == {}

    def test_returns_empty_dict_on_malformed_json(
        self, runner: FFmpegRunner, monkeypatch
    ) -> None:
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="{not json", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert runner.probe_json(Path("x.mp4"), []) == {}

    def test_parses_a_real_payload(self, runner: FFmpegRunner, monkeypatch) -> None:
        payload = '{"streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}'

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert runner.probe_streams(Path("x.mp4"))[0]["height"] == 1080

    def test_uses_a_timeout_because_probing_a_hung_mount_must_not_hang(
        self, runner: FFmpegRunner, recorded
    ) -> None:
        runner.probe_format(Path("x.mp4"))
        assert recorded[0]["timeout"] is not None


class TestTools:
    def test_resolve_returns_the_configured_names(self) -> None:
        tools = FFmpegTools.resolve("ffmpeg-custom", "ffprobe-custom")
        assert tools.ffmpeg.startswith("ffmpeg-custom")

    def test_missing_reports_absent_tools(self) -> None:
        tools = FFmpegTools(ffmpeg="/nonexistent/ffmpeg", ffprobe="/nonexistent/ffprobe")
        assert set(tools.missing()) == {"ffmpeg", "ffprobe"}

    def test_require_raises_naming_what_is_missing(self) -> None:
        tools = FFmpegTools(ffmpeg="/nonexistent/ffmpeg", ffprobe="/nonexistent/ffprobe")
        with pytest.raises(CapabilityMissingError, match="ffmpeg"):
            tools.require()

    def test_require_passes_when_present(self) -> None:
        import shutil

        if shutil.which("ffmpeg") is None:  # pragma: no cover
            pytest.skip("ffmpeg not installed")
        FFmpegTools.resolve().require()


class TestEncoderDetection:
    """`has_encoder` says compiled-in; `try_encoder` says actually works."""

    def test_has_encoder_parses_the_encoder_table(
        self, runner: FFmpegRunner, monkeypatch
    ) -> None:
        listing = " V....D libx264  H.264 / AVC\n V....D h264_nvenc  NVIDIA NVENC\n"

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert runner.has_encoder("libx264") is True
        assert runner.has_encoder("libx264rgb") is False

    def test_try_encoder_actually_encodes(
        self, runner: FFmpegRunner, monkeypatch
    ) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert runner.try_encoder("h264_nvenc", []) is True
        # A synthetic frame source, not a real file: no I/O, no network.
        assert "lavfi" in seen[0]
        assert "-frames:v" in seen[0]

    def test_try_encoder_reports_failure(self, runner: FFmpegRunner, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Cannot load libcuda")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert runner.try_encoder("h264_nvenc", []) is False

    def test_has_filter_parses_the_filter_table(
        self, runner: FFmpegRunner, monkeypatch
    ) -> None:
        listing = " ... subtitles  V->V  Render text subtitles\n ... ass  V->V  Render ASS\n"

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert runner.has_filter("subtitles") is True
        assert runner.has_filter("drawtext") is False
