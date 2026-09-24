"""The PREPARE steps that every source shares.

Two things can produce ``RawMaterials``: a URL (via
:class:`~porter.platforms.base.YtDlpExtractor`) and a file already on disk (via
:class:`~porter.platforms.local.LocalFileDownloader`). The *source* differs; the
work after it does not -- standardise the master, extract the audio, enhance it
for ASR, write ``metadata.json``, clean up scratch.

That work lived as private methods on ``YtDlpExtractor``, which was fine while it
had one caller. Adding the second caller is exactly the situation that produced
five pasted copies of the extraction pipeline in v0.1, so it is hoisted here
instead: one implementation, two sources.

The filenames are here rather than in ``platforms/base.py`` because they are part
of the on-disk ``raw/`` contract, not of any one platform. ``platforms`` sits
above ``media`` in the layer contract, so it may import these; the reverse would
be a cycle.
"""

from __future__ import annotations

from pathlib import Path

from porter.context import RunContext
from porter.events import ArtifactKind, ArtifactReady, Phase
from porter.logging import get_logger
from porter.media.enhance import enhance_for_asr
from porter.media.ffmpeg import FFmpegRunner
from porter.media.probe import MediaInfo, is_valid_video, probe
from porter.media.standardize import extract_audio, standardize_video
from porter.models.materials import TaskLayout
from porter.models.metadata import VideoMetadata

__all__ = [
    "AUDIO_NAME",
    "COVER_NAME",
    "ENHANCED_AUDIO_NAME",
    "METADATA_NAME",
    "VIDEO_NAME",
    "apply_measured_dimensions",
    "enhance_audio",
    "existing_file",
    "extract_cover_frame",
    "master_is_complete",
    "standardize_master",
]

_logger = get_logger(__name__)

#: Filenames inside ``raw/``. Part of the on-disk contract. They are the same
#: for every source: a local file and
#: a downloaded video produce indistinguishable task directories on purpose, so
#: nothing downstream needs to know where the master came from.
VIDEO_NAME = "video.mp4"
AUDIO_NAME = "audio.wav"
ENHANCED_AUDIO_NAME = "audio_enhanced.wav"
COVER_NAME = "cover.jpg"
METADATA_NAME = "metadata.json"


def master_is_complete(runner: FFmpegRunner, video: Path, audio: Path) -> bool:
    """Whether a previous run left a usable master.

    Probes the container rather than trusting the file size, because an
    interrupted download leaves a plausible-looking truncated file.
    """
    if not video.is_file() or not audio.is_file():
        return False
    if audio.stat().st_size == 0:
        return False
    return is_valid_video(runner, video)


def standardize_master(
    runner: FFmpegRunner,
    source: Path,
    layout: TaskLayout,
    ctx: RunContext,
) -> MediaInfo:
    """Produce ``raw/video.mp4`` and ``raw/audio.wav`` from ``source``.

    Returns the probe of the produced master, so the caller can correct the
    declared dimensions from the real pixels.

    Raises:
        MediaError: If the source cannot be read or the transcode fails.
    """
    video_path = layout.raw_dir / VIDEO_NAME
    audio_path = layout.raw_dir / AUDIO_NAME

    ctx.progress(Phase.PREPARE, 60.0, "standardising the video")
    master = standardize_video(runner, source, video_path, config=ctx.config.ffmpeg)

    ctx.progress(Phase.PREPARE, 75.0, "extracting the audio track")
    extract_audio(
        runner,
        video_path,
        audio_path,
        sample_rate=ctx.config.ffmpeg.wav_sample_rate,
    )
    return master


def apply_measured_dimensions(
    metadata: VideoMetadata,
    master: MediaInfo,
) -> VideoMetadata:
    """Overwrite metadata dimensions with the master's real pixels.

    Tri-state on purpose: when ffprobe cannot measure the file, the
    spec-derived orientation is kept rather than being replaced by a guess.
    """
    if not master.has_dimensions:
        _logger.debug("master dimensions unavailable; keeping the declared orientation")
        return metadata
    return metadata.model_copy(
        update={
            "width": master.width,
            "height": master.height,
            "is_vertical": bool(master.is_vertical),
        }
    )


def enhance_audio(
    runner: FFmpegRunner,
    audio: Path,
    layout: TaskLayout,
    ctx: RunContext,
) -> Path | None:
    """Produce the ASR-optimised WAV, honouring ``--no-denoise``."""
    if not ctx.options.audio_denoise:
        _logger.info("audio enhancement disabled by request")
        return None

    ctx.progress(Phase.PREPARE, 85.0, "enhancing the audio for ASR")
    return enhance_for_asr(runner, audio, layout.raw_dir / ENHANCED_AUDIO_NAME)


def extract_cover_frame(
    runner: FFmpegRunner,
    video: Path,
    layout: TaskLayout,
    ctx: RunContext,
) -> Path | None:
    """Take a poster frame from the master as the cover.

    A local file has no thumbnail to download, so the analogue is a frame from
    the video itself. The frame is taken a little way in rather than at zero,
    because the first frame of a real video is very often a black fade-in -- a
    cover that is a black rectangle is worse than none.

    Never fatal: a missing cover only costs the ``cover.jpg`` artifact.
    """
    position = _cover_position(runner, video)
    dest = layout.raw_dir / COVER_NAME

    proc = runner.run(
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{position:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(dest),
        ],
        what="extracting the cover frame",
        check=False,
    )
    if proc.returncode != 0 or not dest.is_file():
        _logger.warning("could not extract a cover frame; continuing without a cover")
        return None

    ctx.emit(
        ArtifactReady(
            phase=Phase.PREPARE,
            kind=ArtifactKind.COVER,
            path=dest,
        )
    )
    return dest


def _cover_position(runner: FFmpegRunner, video: Path) -> float:
    """Where to grab the poster frame: a tenth in, capped at three seconds.

    Capped because a tenth of a two-hour video is twelve minutes, which would
    mean seeking through a file for a thumbnail. Floored at zero so an unknown
    or zero duration still yields the first frame rather than a negative seek.
    """
    info = probe(runner, video)
    if info is None or not info.duration or info.duration <= 0:
        return 0.0
    return max(0.0, min(3.0, info.duration * 0.1))


def existing_file(path: Path) -> Path | None:
    """``path`` if it is a file, else ``None``.

    Optional artifacts are reported as ``None`` rather than as a path that does
    not exist, so downstream code never has to re-check.
    """
    return path if path.is_file() else None
