"""Encoder selection — P3.1.

The design being pinned: availability comes from a **trial encode**, not from a
device path or an encoder list. The first class is the regression test for the
bug that motivated the change.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from porter.errors import CapabilityMissingError, MediaError
from porter.media.encode import (
    HARDWARE_PROFILES,
    EncoderProfile,
    EncoderSelector,
    HardwareTier,
    detect_encoder,
    software_profile_for,
)


class FakeRunner:
    """Records the argv it is asked to run and answers from a table."""

    def __init__(self, verdicts: dict[str, tuple[int, str]] | None = None) -> None:
        self.verdicts = verdicts or {}
        self.commands: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.raises: Exception | None = None

    def run(self, args, *, what, check=True, timeout=None):
        self.commands.append(list(args))
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        encoder = args[args.index("-c:v") + 1]
        code, stderr = self.verdicts.get(encoder, (1, "unknown encoder"))
        return _Completed(code, stderr)


def _Completed(code: int, stderr: str):  # noqa: N802
    from types import SimpleNamespace

    return SimpleNamespace(returncode=code, stdout="", stderr=stderr)


def _profile(name: str) -> EncoderProfile:
    for entry in HARDWARE_PROFILES:
        if entry.name == name:
            return entry
    raise AssertionError(f"no such profile: {name}")


class TestWsl2Regression:
    """The bug: a device-path probe misreads WSL2 as having no GPU.

    Measured on the development machine — no ``/dev/dri``, no ``/dev/nvidia*``,
    and NVENC fully working through ``/dev/dxg``. v0.1's NVENC branch did not
    look for a device at all, but its QSV branch did, and the obvious "fix" of
    adding a device check to NVENC would have broken this machine.
    """

    def test_nvenc_works_with_no_nvidia_device_node(self) -> None:
        """No device check for NVENC, because NVENC is not addressed by one."""
        runner = FakeRunner({"h264_nvenc": (0, "")})

        with patch("porter.media.encode.Path") as fake_path:
            # Any path check at all would fail, since WSL2 has neither node.
            fake_path.return_value.exists.return_value = False
            profile = EncoderSelector(runner).select()

        assert profile.name == "h264_nvenc"
        assert profile.tier is HardwareTier.HARDWARE
        assert fake_path.return_value.exists.call_count == 0, (
            "no device path may be consulted when selecting NVENC"
        )

    def test_the_trial_encode_is_what_decides(self) -> None:
        """Present in `-encoders` output is not the question; running is."""
        runner = FakeRunner({"h264_nvenc": (1, "Cannot load libcuda.so.1")})

        profile = EncoderSelector(runner).select()

        assert profile.name != "h264_nvenc"
        # The tier is derived from this machine's core count by
        # ``software_profile_for()``, so pinning a specific tier would bake the
        # development machine's CPU into the test -- it would false-fail on a
        # 4-core CI runner, which falls back to SOFTWARE_SLOW.
        assert profile.tier is software_profile_for().tier
        assert profile.tier in (HardwareTier.SOFTWARE_FAST, HardwareTier.SOFTWARE_SLOW)

    def test_vaapi_does_keep_its_device_check(self) -> None:
        """VAAPI *is* defined in terms of a node, so its absence is conclusive."""
        runner = FakeRunner({"h264_vaapi": (0, "")})

        with patch("porter.media.encode.Path") as fake_path:
            fake_path.return_value.exists.return_value = False
            usable, reason = EncoderSelector(runner).probe(_profile("h264_vaapi"))

        assert usable is False
        assert "/dev/dri/renderD128 is not present" in reason
        assert runner.commands == [], "no point invoking ffmpeg with no device node"


class TestSelectionOrder:
    def test_prefers_hardware_over_software(self) -> None:
        runner = FakeRunner({"h264_nvenc": (0, "")})
        profile = EncoderSelector(runner).select()
        assert profile.name == "h264_nvenc"
        assert profile.tier is HardwareTier.HARDWARE

    def test_falls_through_to_the_next_hardware_encoder(self) -> None:
        runner = FakeRunner({"h264_nvenc": (1, "no capable devices found"), "h264_qsv": (0, "")})
        profile = EncoderSelector(runner).select()
        assert profile.name == "h264_qsv"

    def test_software_is_the_last_resort_and_always_succeeds(self) -> None:
        runner = FakeRunner({})  # everything fails
        profile = EncoderSelector(runner).select()
        assert profile.name == "libx264"
        assert profile.needs_trial is False

    def test_software_is_never_trial_encoded(self) -> None:
        """libx264 cannot fail for want of a device, so probing it is wasted time."""
        runner = FakeRunner({})
        EncoderSelector(runner).select()
        assert all("-c:v" not in cmd or cmd[cmd.index("-c:v") + 1] != "libx264" for cmd in runner.commands)

    def test_an_empty_candidate_list_yields_software(self) -> None:
        profile = EncoderSelector(FakeRunner({}), candidates=()).select()
        assert profile.name == "libx264"


class TestCaching:
    def test_each_encoder_is_probed_once(self) -> None:
        runner = FakeRunner({"h264_nvenc": (1, "nope"), "h264_qsv": (1, "nope")})
        selector = EncoderSelector(runner)

        selector.select()
        first = len(runner.commands)
        selector.select()

        assert len(runner.commands) == first, "the second run must hit the cache"

    def test_two_selectors_do_not_share_verdicts(self) -> None:
        """A driver can be loaded between runs; module-level state would lie."""
        runner = FakeRunner({"h264_nvenc": (1, "Cannot load libcuda.so.1")})
        assert EncoderSelector(runner).select().name != "h264_nvenc"

        runner.verdicts["h264_nvenc"] = (0, "")
        assert EncoderSelector(runner).select().name == "h264_nvenc"

    def test_report_probes_every_candidate(self) -> None:
        """doctor needs the NVENC answer even after NVENC has been ruled out."""
        runner = FakeRunner({"h264_nvenc": (0, "")})
        report = EncoderSelector(runner).report()

        assert len(report) == len(HARDWARE_PROFILES)
        probed = {profile.name for profile, _, _ in report}
        assert "h264_vaapi" in probed, "early exit would never compute this"


class TestTrialCommand:
    def test_probes_one_frame_and_writes_nothing(self) -> None:
        runner = FakeRunner({"h264_nvenc": (0, "")})
        EncoderSelector(runner).probe(_profile("h264_nvenc"))
        cmd = runner.commands[0]

        assert "-frames:v" in cmd
        assert cmd[cmd.index("-frames:v") + 1] == "1"
        assert cmd[-1] == "-"
        assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "lavfi"
        assert "null" in cmd, "the probe must discard its output"
        assert not any(part.endswith(".mp4") for part in cmd), "no file may be written"

    def test_uses_a_small_frame(self) -> None:
        runner = FakeRunner({"h264_nvenc": (0, "")})
        EncoderSelector(runner).probe(_profile("h264_nvenc"))
        cmd = " ".join(runner.commands[0])
        assert "256x144" in cmd

    def test_quality_flags_are_per_encoder(self) -> None:
        """NVENC has no --crf; treating it as universal is a hard failure."""
        nvenc = _profile("h264_nvenc").args()
        qsv = _profile("h264_qsv").args()

        assert "-cq" in nvenc
        assert "-crf" not in nvenc
        assert "-global_quality" in qsv
        assert "-cq" not in qsv

    def test_the_probe_has_a_timeout(self) -> None:
        """A hung encoder must not hang the run before it has started."""
        runner = FakeRunner({"h264_nvenc": (0, "")})
        EncoderSelector(runner).probe(_profile("h264_nvenc"))
        assert runner.timeouts[0] is not None

    def test_vaapi_trial_includes_the_device_and_hwupload(self) -> None:
        cmd = " ".join(_profile("h264_vaapi").trial_command())
        assert "-vaapi_device" in cmd
        assert "hwupload" in cmd

    def test_video_filter_precedes_the_codec(self) -> None:
        """-vf after -c:v is ignored, which would silently disable hwupload."""
        args = _profile("h264_vaapi").args()
        assert args.index("-vf") < args.index("-c:v")


class TestDiagnosis:
    """The failure has to be actionable, and v0.1's messages were not."""

    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            ("[h264_nvenc] Cannot load libcuda.so.1", "CUDA driver"),
            ("[h264_nvenc] No capable devices found", "no compatible GPU"),
            ("Unknown encoder 'h264_nvenc'", "built without"),
            ("[h264_qsv] Error initializing output stream 0:0", "failed to initialise"),
            (
                "[enc:h264_qsv] Could not open encoder before EOF\nConversion failed!",
                "could not be opened",
            ),
        ],
    )
    def test_known_failures_get_a_reason(self, stderr: str, expected: str) -> None:
        runner = FakeRunner({"h264_nvenc": (1, stderr)})
        _, reason = EncoderSelector(runner).probe(_profile("h264_nvenc"))
        assert expected.lower() in reason.lower()

    def test_unknown_failure_reports_the_tail_not_the_banner(self) -> None:
        """v0.1 sliced stderr from the front, so it reported ffmpeg's version."""
        stderr = (
            "ffmpeg version n9.0.1 Copyright (c) 2000-2025 the FFmpeg developers\n"
            "  configuration: --enable-libass --enable-libx264 ...\n"
            "Something specific went wrong at the end\n"
        )
        runner = FakeRunner({"h264_nvenc": (1, stderr)})
        _, reason = EncoderSelector(runner).probe(_profile("h264_nvenc"))

        assert "Something specific went wrong" in reason
        assert "ffmpeg version" not in reason

    def test_an_empty_stderr_still_produces_a_reason(self) -> None:
        runner = FakeRunner({"h264_nvenc": (1, "")})
        usable, reason = EncoderSelector(runner).probe(_profile("h264_nvenc"))
        assert usable is False
        assert reason


class TestErrorHandling:
    def test_a_missing_ffmpeg_propagates(self) -> None:
        """No encoder question can be answered without ffmpeg, so this is fatal."""
        runner = FakeRunner()
        runner.raises = CapabilityMissingError("ffmpeg", "not found")

        with pytest.raises(CapabilityMissingError):
            EncoderSelector(runner).select()

    def test_a_trial_timeout_marks_only_that_encoder_unusable(self) -> None:
        """A slow or hung encoder must not fail the run; another may work."""
        runner = FakeRunner({"h264_qsv": (0, "")})
        runner.raises = MediaError("timed out")

        usable, reason = EncoderSelector(runner).probe(_profile("h264_nvenc"))

        assert usable is False
        assert "did not finish" in reason

    def test_probe_never_raises_for_an_unusable_encoder(self) -> None:
        """Unusable is an ordinary answer here, not an exception."""
        runner = FakeRunner({"h264_nvenc": (127, "boom")})
        assert EncoderSelector(runner).probe(_profile("h264_nvenc"))[0] is False


class TestSoftwareProfile:
    def test_many_cores_gets_veryfast(self) -> None:
        profile = software_profile_for(16)
        assert profile.tier is HardwareTier.SOFTWARE_FAST
        assert "-preset" in profile.quality_args
        assert profile.quality_args[profile.quality_args.index("-preset") + 1] == "veryfast"

    def test_few_cores_gets_ultrafast(self) -> None:
        profile = software_profile_for(4)
        assert profile.tier is HardwareTier.SOFTWARE_SLOW
        assert profile.quality_args[profile.quality_args.index("-preset") + 1] == "ultrafast"

    def test_short_videos_get_software(self) -> None:
        assert software_profile_for(1).needs_trial is False

    def test_the_threshold_is_eight_cores(self) -> None:
        assert software_profile_for(8).tier is HardwareTier.SOFTWARE_FAST
        assert software_profile_for(7).tier is HardwareTier.SOFTWARE_SLOW

    def test_cpu_count_defaults_to_the_machine(self) -> None:
        with patch("porter.media.encode.os.cpu_count", return_value=32):
            assert software_profile_for().tier is HardwareTier.SOFTWARE_FAST


class TestDetectEncoder:
    def test_convenience_wrapper_selects(self) -> None:
        profile = detect_encoder(FakeRunner({"h264_nvenc": (0, "")}))
        assert profile.name == "h264_nvenc"

    def test_profiles_are_hashable_and_comparable_by_name(self) -> None:
        assert _profile("h264_nvenc") == _profile("h264_nvenc")
        assert len({profile.name for profile in HARDWARE_PROFILES}) == len(HARDWARE_PROFILES)


class TestTierIsAString:
    def test_serialises_as_plain_json(self) -> None:
        """The tier ends up in doctor reports and config round-trips."""
        import json

        assert json.dumps({"tier": HardwareTier.HARDWARE}) == '{"tier": "hardware"}'
        assert HardwareTier.HARDWARE == "hardware"
