"""Encoder selection by **trial encode**, not by device paths or encoder lists.

## Why the v0.1 approach was wrong

``detect_hardware_profile()`` walked a tier table:

* Tier A if a hardware encoder appeared in ``ffmpeg -encoders`` output
* Tier B if the CPU had >= 8 cores
* Tier C otherwise

Two failures, and the second is the interesting one.

**Compiled in is not runnable.** ``-encoders`` lists what the build supports, not
what the machine can do. A CI container, a laptop with the driver unloaded, and a
WSL2 guest all report ``h264_nvenc`` while being unable to load ``libcuda``. The
QSV branch at least checked ``/dev/dri``; the NVENC branch checked nothing at all.

**And a device path is not the right question either.** This was measured on the
development machine: it has *neither* ``/dev/dri`` *nor* ``/dev/nvidia*``, yet
NVENC works fully, because WSL2 reaches the GPU through ``/dev/dxg`` plus
``/usr/lib/wsl/lib/libcuda.so``. Any path-based probe misreads that machine as
Tier B and silently forfeits a measured **1.46x** speedup on a 1080p30 encode.

The failure mode is what makes this worth fixing rather than tolerating: the
misdetection is not reported when it happens. ffmpeg is invoked with
``-c:v h264_nvenc``, and the error surfaces at the *end* of the job — after
download, transcription and translation — as ``Cannot load libcuda.so.1``.

## What this module does instead

Ask ffmpeg. Encoding one 256x144 black frame costs a few hundred milliseconds and
answers the real question — *can this machine produce a video with this encoder,
right now, under this invocation?* — rather than inferring it from a device node
that may or may not be how the platform reaches the GPU.

## Two deliberate exceptions

A trial encode is used for everything except:

1. **Software fallback.** ``libx264`` cannot fail for want of a device, so no
   probe is needed; its preset is picked from the CPU count, which is what the
   old Tier B/C split was actually getting right.
2. **VAAPI's device path.** VAAPI genuinely is addressed by a device node, and
   ``-vaapi_device`` must name one that exists or ffmpeg errors out before
   encoding anything. So VAAPI keeps a path check — as a *fast, honest refusal*
   when the node is absent, not as evidence that the GPU works.

The distinction is the point: check a device path only when the encoder's
interface is defined in terms of that path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from porter.errors import CapabilityMissingError, MediaError
from porter.logging import get_logger
from porter.media.ffmpeg import FFmpegRunner

if TYPE_CHECKING:  # pragma: no cover - annotation only, keeps media import-light
    from porter.config import FFmpegConfig

__all__ = [
    "HARDWARE_PROFILES",
    "SOFTWARE_PROFILE",
    "TRIAL_FRAME_SIZE",
    "EncoderProfile",
    "EncoderSelector",
    "HardwareTier",
    "detect_encoder",
    "profile_from_config",
    "software_profile_for",
]

_logger = get_logger(__name__)

#: Small enough to encode in a fraction of a second, large enough that a real
#: encoder will object if its pipeline cannot be set up at all. Odd dimensions
#: would add a yuv420p alignment warning that has nothing to do with the probe.
TRIAL_FRAME_SIZE = "256x144"

#: Duration of the synthetic trial input. One frame is requested, so this only
#: has to be long enough that the filter graph produces anything at all.
_TRIAL_DURATION = 0.1


class HardwareTier(str, Enum):
    """How the encode will be performed. Kept from v0.1 because the names are
    already in configuration files and operator vocabulary.

    ``(str, Enum)`` rather than :class:`enum.StrEnum`. The floor is 3.11, so
    ``StrEnum`` is available, but the two differ in what ``str()`` and f-strings
    produce, and changing that is a separate user-visible decision rather than part
    of a support-floor bump. For what matters here they agree —
    ``HardwareTier.HARDWARE == "hardware"``, JSON serialisation as a plain string.

    ``HARDWARE`` ⇄ old Tier A, ``SOFTWARE_FAST`` ⇄ Tier B, ``SOFTWARE_SLOW`` ⇄
    Tier C. The names now describe what was *measured* rather than assumed from
    the core count.
    """

    HARDWARE = "hardware"
    SOFTWARE_FAST = "software_fast"
    SOFTWARE_SLOW = "software_slow"


#: Cores at or above which libx264 ``veryfast`` keeps up with a decode. v0.1 used
#: the same threshold; it was the part of the tier table that was sound, because
#: a core count really is the relevant input for a software encoder.
_FAST_CPU_CORES = 8


@dataclass(frozen=True, eq=False)
class EncoderProfile:
    """One complete answer to "how do we encode", including the flags.

    Carrying the flags here rather than mapping them from the encoder name at the
    call site is what removes the ``if tier == A: preset = p4`` branching: the
    pipeline asks for a profile and passes ``profile.args()`` to ffmpeg.
    """

    name: str
    label: str
    tier: HardwareTier
    #: A filter that must precede the encoder. VAAPI needs ``hwupload``.
    video_filter: str | None = None
    #: Quality flags, e.g. ``("-preset", "p4", "-cq", "19")``. The NVENC
    #: equivalent of CRF is CQ; VAAPI uses ``-global_quality``. Treating "crf" as
    #: a universal option is how a hardware encode ends up with the wrong
    #: quality target (or a hard failure on an unrecognised option).
    quality_args: tuple[str, ...] = ()
    #: Required device node, if the encoder is *defined* in terms of one.
    device: str | None = None
    #: The flag that hands :attr:`device` to ffmpeg, e.g. ``-vaapi_device``.
    #:
    #: Paired with ``device`` rather than folded into an argv tuple so the
    #: existence check and the flag cannot drift apart. The first draft set
    #: ``device`` without the flag, which made VAAPI *look* selectable while
    #: every trial encode failed — a profile that could never be chosen.
    device_flag: str | None = None
    #: Whether the trial encode should be attempted at all.
    needs_trial: bool = True
    #: Why this profile is unusable, filled in by the selector.
    unavailable_reason: str | None = field(default=None, compare=False)

    @property
    def input_args(self) -> tuple[str, ...]:
        """Flags placed *before* ``-i`` that the device requires."""
        if self.device and self.device_flag:
            return (self.device_flag, self.device)
        return ()

    def args(self) -> list[str]:
        """Every flag this profile contributes, in the right order.

        ``-vf`` has to come after the inputs and before the codec, which is why
        it is returned here rather than being left to the caller.
        """
        args: list[str] = []
        if self.video_filter:
            args.extend(["-vf", self.video_filter])
        args.extend(["-c:v", self.name])
        args.extend(self.quality_args)
        return args

    def trial_command(self) -> list[str]:
        """argv for the availability probe, excluding the leading ``-nostdin``."""
        cmd: list[str] = list(self.input_args)
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={TRIAL_FRAME_SIZE}:d={_TRIAL_DURATION}:r=1",
                "-frames:v",
                "1",
            ]
        )
        cmd.extend(self.args())
        # `-f null -` encodes and discards, so the probe measures the encoder
        # without writing anything to disk.
        cmd.extend(["-f", "null", "-"])
        return cmd


#: Preference order. Hardware first because it is measurably faster, then
#: software. Order within the hardware group is by how likely the platform is to
#: be the one actually present, so the common case costs one trial encode rather
#: than four.
HARDWARE_PROFILES: tuple[EncoderProfile, ...] = (
    EncoderProfile(
        name="h264_nvenc",
        label="NVIDIA NVENC",
        tier=HardwareTier.HARDWARE,
        quality_args=("-preset", "p4", "-cq", "19", "-pix_fmt", "yuv420p"),
    ),
    EncoderProfile(
        name="h264_qsv",
        label="Intel Quick Sync",
        tier=HardwareTier.HARDWARE,
        # QSV also works without a device node (it uses the default adapter), but
        # it does need the hardware to actually be there, so it gets a trial.
        quality_args=("-preset", "medium", "-global_quality", "22", "-pix_fmt", "nv12"),
    ),
    EncoderProfile(
        name="h264_videotoolbox",
        label="Apple VideoToolbox",
        tier=HardwareTier.HARDWARE,
        quality_args=("-q:v", "55", "-pix_fmt", "yuv420p"),
    ),
    EncoderProfile(
        name="h264_vaapi",
        label="VAAPI",
        tier=HardwareTier.HARDWARE,
        # `55` on the 0-51 ICQ scale, which VAAPI's global_quality expects.
        quality_args=("-global_quality", "22", "-pix_fmt", "nv12"),
        video_filter="format=nv12,hwupload",
        device="/dev/dri/renderD128",
        device_flag="-vaapi_device",
    ),
)


def software_profile_for(cpu_count: int | None = None) -> EncoderProfile:
    """The libx264 profile appropriate to this machine's core count.

    No trial encode: software encoding cannot fail for want of a device, and
    ``libx264`` is compiled into every ffmpeg build porter will realistically
    meet. If it somehow is absent, the encode raises a clear ``MediaError``
    immediately rather than hours later.
    """
    cores = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    if cores >= _FAST_CPU_CORES:
        return EncoderProfile(
            name="libx264",
            label=f"libx264 (veryfast, {cores} cores)",
            tier=HardwareTier.SOFTWARE_FAST,
            quality_args=("-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"),
            needs_trial=False,
        )
    return EncoderProfile(
        name="libx264",
        label=f"libx264 (ultrafast, {cores} cores)",
        tier=HardwareTier.SOFTWARE_SLOW,
        quality_args=("-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p"),
        needs_trial=False,
    )


# Backwards-compatible alias for the v0.1 name.
SOFTWARE_PROFILE = software_profile_for()


def profile_from_config(config: FFmpegConfig) -> EncoderProfile:
    """The software profile a user gets when ``ffmpeg.auto_tune`` is off.

    Auto-tune exists because the right preset depends on the machine, and the
    trial encode is how that is discovered rather than guessed. Turning it off
    means "I know my settings": the probe is skipped entirely and
    ``ffmpeg.preset`` / ``ffmpeg.crf`` are used verbatim.

    No trial encode, for the same reason :func:`software_profile_for` has none:
    a user who named a preset wants that preset, not a verdict about it.
    """
    return EncoderProfile(
        name="configured",
        label=f"libx264 ({config.preset}, crf {config.crf}) -- ffmpeg.auto_tune is off",
        tier=HardwareTier.SOFTWARE_FAST,
        quality_args=(
            "-preset",
            config.preset,
            "-crf",
            str(config.crf),
            "-pix_fmt",
            config.pixel_format,
        ),
        needs_trial=False,
    )


class EncoderSelector:
    """Probes encoders once per run and remembers the answers.

    The cache matters: selection is consulted by both ``doctor`` and the burn
    phase, and each miss costs a real ffmpeg process. It is an instance
    attribute rather than module state so a test — or a second run in the same
    process — cannot inherit a stale verdict from a machine state that has since
    changed (a driver loaded, a GPU passed through).
    """

    def __init__(
        self,
        runner: FFmpegRunner,
        *,
        cpu_count: int | None = None,
        candidates: tuple[EncoderProfile, ...] = HARDWARE_PROFILES,
        cache: dict[str, tuple[bool, str]] | None = None,
    ) -> None:
        self.runner = runner
        self.cpu_count = cpu_count
        self.candidates = candidates
        self._cache: dict[str, tuple[bool, str]] = cache if cache is not None else {}

    @classmethod
    def for_config(
        cls,
        runner: FFmpegRunner,
        config: FFmpegConfig | None = None,
        *,
        cpu_count: int | None = None,
    ) -> EncoderSelector:
        """A selector that honours ``ffmpeg.auto_tune``.

        The default (auto-tune on, or no config at all) probes the hardware tier
        by trial encode. With auto-tune off there is nothing to discover, so the
        candidate list is the one profile built from the configured preset and
        CRF -- which also means ``doctor`` reports the encoder the job will
        actually use instead of one it will not.
        """
        if config is None or config.auto_tune:
            return cls(runner, cpu_count=cpu_count)
        return cls(runner, cpu_count=cpu_count, candidates=(profile_from_config(config),))

    # ------------------------------------------------------------------
    # Probing
    # ------------------------------------------------------------------

    def probe(self, profile: EncoderProfile) -> tuple[bool, str]:
        """Trial-encode one frame. Returns ``(usable, reason_if_not)``.

        Never raises: an unusable encoder is an ordinary answer here, and the
        whole point of the method is to convert "this will fail in three hours"
        into a boolean available now.
        """
        cached = self._cache.get(profile.name)
        if cached is not None:
            return cached

        result = self._probe_uncached(profile)
        self._cache[profile.name] = result
        return result

    def _probe_uncached(self, profile: EncoderProfile) -> tuple[bool, str]:
        if not profile.needs_trial:
            return True, ""

        if profile.device and not Path(profile.device).exists():
            # A fast, honest refusal: VAAPI is addressed through this node, so
            # its absence is conclusive and there is no point invoking ffmpeg.
            reason = f"{profile.device} is not present"
            _logger.debug("encoder %s unavailable: %s", profile.name, reason)
            return False, reason

        try:
            result = self.runner.run(
                profile.trial_command(),
                what=f"{profile.name} trial encode",
                check=False,
                timeout=60.0,
            )
        except CapabilityMissingError:
            # ffmpeg itself is absent. Propagating is right: no encoder question
            # can be answered without it, and the caller needs the install hint.
            raise
        except MediaError as exc:
            # A timeout. Report it as this encoder being unusable rather than
            # failing the run, since another encoder may well work.
            return False, f"trial encode did not finish: {exc.message}"

        if result.returncode == 0:
            _logger.debug("encoder %s available", profile.name)
            return True, ""

        reason = _diagnose(profile, result.stderr or "")
        _logger.debug("encoder %s unavailable: %s", profile.name, reason)
        return False, reason

    # ------------------------------------------------------------------
    # Selecting
    # ------------------------------------------------------------------

    def available(self, profile: EncoderProfile) -> bool:
        return self.probe(profile)[0]

    def select(self) -> EncoderProfile:
        """The first hardware encoder that works, else the software profile.

        Always returns something, because the software profile cannot fail for
        want of a device. Callers therefore never need a "no encoder" branch —
        and there is no path where the job runs for an hour and then discovers
        it cannot encode.
        """
        for profile in self.candidates:
            usable, reason = self.probe(profile)
            if usable:
                _logger.info("using %s for video encoding", profile.label)
                return profile
            _logger.debug("skipping %s: %s", profile.label, reason)

        profile = software_profile_for(self.cpu_count)
        _logger.info(
            "no hardware encoder available; using %s",
            profile.label,
        )
        return profile

    def report(self) -> list[tuple[EncoderProfile, bool, str]]:
        """Every candidate with its verdict, for ``doctor``.

        Probing all of them rather than stopping at the first success is
        deliberate: an operator asking "why is NVENC not being used" needs the
        NVENC answer, which early-exit would never compute.
        """
        return [(profile, *self.probe(profile)) for profile in self.candidates]


def _diagnose(profile: EncoderProfile, stderr: str) -> str:
    """Turn ffmpeg's stderr into a short reason an operator can act on.

    Best-effort by design: the raw stderr is logged at debug level, and this
    only has to be good enough to distinguish "driver not loaded" from "this
    ffmpeg was built without the encoder" — the two failures that look identical
    if you only know that the exit code was non-zero.
    """
    lowered = stderr.lower()
    patterns = (
        ("cannot load libcuda", "the CUDA driver could not be loaded"),
        ("libcuda.so", "the CUDA driver could not be loaded"),
        ("no capable devices found", "no compatible GPU was found"),
        ("cannot open device", "the encoder's device could not be opened"),
        # What QSV emits with no Intel GPU: ffmpeg reports the generic
        # "Conversion failed!" at the end, so the useful line is earlier.
        ("could not open encoder", "the encoder could not be opened (GPU absent or in use)"),
        ("unknown encoder", f"this ffmpeg was built without {profile.name}"),
        ("encoder not found", f"this ffmpeg was built without {profile.name}"),
        ("no such filter", "a required filter is missing from this ffmpeg"),
        ("error initializing output stream", "the encoder failed to initialise"),
        ("device creation failed", "the encoder's device could not be created"),
    )
    for needle, message in patterns:
        if needle in lowered:
            return message

    tail = _last_meaningful_line(stderr)
    return f"trial encode failed: {tail}" if tail else "trial encode failed"


def _last_meaningful_line(stderr: str) -> str:
    """The last non-empty line of ffmpeg's report, trimmed.

    The *last* line, not the first: ffmpeg prints its banner first, so taking the
    head yields the version string and the build configuration — which is
    precisely the mistake that made v0.1's error messages useless.
    """
    for line in reversed(stderr.strip().splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def detect_encoder(
    runner: FFmpegRunner | None = None,
    *,
    cpu_count: int | None = None,
) -> EncoderProfile:
    """Convenience wrapper for callers that do not need to reuse the cache."""
    return EncoderSelector(runner or FFmpegRunner(), cpu_count=cpu_count).select()
