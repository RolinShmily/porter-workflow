"""Turn a downloaded file into the standard master assets under ``raw/``.

Every platform extractor in ``v0.1`` ended with the same three ffmpeg
invocations, copied five times with small differences. They live here once:

``standardize_video``
    ``download.*`` -> ``raw/video.mp4``, H.264 + AAC + ``faststart``.
``extract_audio``
    ``raw/video.mp4`` -> ``raw/audio.wav``, 16 kHz mono 16-bit PCM.

Two deliberate changes from ``v0.1``
------------------------------------
**Explicit stream mapping.** v0.1 relied on ffmpeg's default ``-map 0``, which
copies *every* stream. A source MKV with several audio tracks or embedded
subtitles then either bloated the master or made ``-c copy`` into MP4 fail
outright (MP4 cannot hold most subtitle codecs). Both paths now take
``-map 0:v:0`` plus an optional ``-map 0:a:0?``, so the master has exactly the
one video and one audio stream everything downstream assumes.

**Real error reporting.** See :mod:`porter.media.ffmpeg`.
"""

from __future__ import annotations

from pathlib import Path

from porter.config import FFmpegConfig
from porter.logging import get_logger
from porter.media.ffmpeg import FFmpegRunner
from porter.media.probe import MediaInfo, probe

__all__ = ["extract_audio", "standardize_video"]

_logger = get_logger(__name__)

#: Stream selection shared by both standardisation paths.
_MAP = ("-map", "0:v:0", "-map", "0:a:0?")


def standardize_video(
    runner: FFmpegRunner,
    source: Path,
    dest: Path,
    *,
    config: FFmpegConfig | None = None,
) -> MediaInfo:
    """Produce the standard master at ``dest`` from ``source``.

    Stream-copies when the source is already H.264/AAC in an MP4 container —
    the common case for yt-dlp's ``merge_output_format=mp4`` — and transcodes
    otherwise. Copying is both faster and lossless, so it is worth the branch.

    Args:
        runner: ffmpeg runner.
        source: The downloaded file.
        dest: Where to write ``video.mp4``.
        config: Encoding parameters. Defaults to :class:`FFmpegConfig`.

    Returns:
        Probing information for the finished master.

    Raises:
        MediaError: The source has no video stream, or ffmpeg failed.
    """
    config = config or FFmpegConfig()
    dest.parent.mkdir(parents=True, exist_ok=True)

    source_info = probe(runner, source)
    if source_info is None:
        from porter.errors import MediaError

        raise MediaError(
            f"cannot standardise an unreadable file: {source}",
            path=str(source),
        )
    if not source_info.has_video:
        from porter.errors import MediaError

        raise MediaError(
            f"this file has no video stream: {source}",
            path=str(source),
            container=source_info.container,
        )

    if source_info.is_stream_copyable and source.suffix.lower() == ".mp4":
        _logger.debug(
            "stream-copying %s (%s/%s) to %s",
            source.name,
            source_info.video_codec,
            source_info.audio_codec,
            dest.name,
        )
        runner.run(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                *_MAP,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(dest),
            ],
            what="standardising the video (stream copy)",
        )
    else:
        _logger.debug(
            "transcoding %s (%s/%s) to %s",
            source.name,
            source_info.video_codec,
            source_info.audio_codec,
            dest.name,
        )
        runner.run(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                *_MAP,
                "-c:v",
                config.video_codec,
                "-preset",
                config.preset,
                "-crf",
                str(config.crf),
                "-pix_fmt",
                config.pixel_format,
                "-c:a",
                config.audio_codec,
                "-b:a",
                config.audio_bitrate,
                "-ar",
                str(config.audio_sample_rate),
                "-movflags",
                "+faststart",
                str(dest),
            ],
            what="standardising the video (transcode)",
        )

    result = probe(runner, dest)
    if result is None:
        from porter.errors import MediaError

        raise MediaError(
            f"standardisation produced an unreadable file: {dest}",
            path=str(dest),
        )

    _logger.info(
        "master ready: %s (%sx%s, %.1fs)",
        dest.name,
        result.width,
        result.height,
        result.duration or 0.0,
    )
    return result


def extract_audio(
    runner: FFmpegRunner,
    source: Path,
    dest: Path,
    *,
    sample_rate: int = 16000,
) -> Path:
    """Extract mono 16-bit PCM at ``sample_rate`` from ``source``.

    16 kHz mono is what every ASR backend in the chain wants, so it is produced
    once here rather than resampled per backend.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    runner.run(
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            str(dest),
        ],
        what="extracting the audio track",
    )
    return dest
