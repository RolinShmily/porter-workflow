"""Typed view over ffprobe output.

Replaces two ``v0.1`` helpers whose failure mode was worse than their success
mode:

``is_valid_video_file``
    Reasonable — ffprobe the duration to reject half-written downloads. Kept,
    with the silent ``except: pass`` replaced by a debug log.
``get_video_dimensions``
    Returned ``(1920, 1080)`` when probing failed. That is not a default, it is a
    **fabricated measurement**: the pipeline uses it to decide ``is_vertical``,
    which selects the subtitle layout, so a vertical video that failed to probe
    got horizontal styling and its captions were laid out for the wrong frame.

:func:`dimensions` returns ``None`` instead, and the caller falls back to the
platform spec's declared orientation rather than to a guess about pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from porter.logging import get_logger
from porter.media.ffmpeg import FFmpegRunner

__all__ = ["MediaInfo", "dimensions", "is_valid_video", "probe"]

_logger = get_logger(__name__)

#: A file smaller than this cannot be a usable video; used to reject the
#: zero-byte placeholders and truncated fragments that interrupted downloads
#: leave behind.
MIN_VIDEO_BYTES = 1024

#: Codecs that can be stream-copied into the standard MP4 master without a
#: re-encode. Keeps the common path fast and lossless.
COPYABLE_VIDEO_CODECS = frozenset({"h264", "avc1"})
COPYABLE_AUDIO_CODECS = frozenset({"aac"})

_VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".webm", ".ts", ".flv", ".mov"})


@dataclass(frozen=True)
class MediaInfo:
    """What ffprobe can tell us about one media file."""

    path: Path
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    container: str | None = None

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def has_dimensions(self) -> bool:
        return bool(self.width and self.height)

    @property
    def is_vertical(self) -> bool | None:
        """True/False from real pixels, or ``None`` when unknown.

        Deliberately tri-state. Collapsing "unknown" into ``False`` is what made
        v0.1 mis-style vertical videos.
        """
        if not self.has_dimensions:
            return None
        return bool(self.height and self.width and self.height > self.width)

    @property
    def is_stream_copyable(self) -> bool:
        """Whether the master can be produced with ``-c copy``."""
        return (
            self.video_codec in COPYABLE_VIDEO_CODECS
            and self.audio_codec in COPYABLE_AUDIO_CODECS
        )

    @property
    def is_playable(self) -> bool:
        """A real video with a positive duration."""
        return self.has_video and bool(self.duration and self.duration > 0)


def probe(runner: FFmpegRunner, path: Path) -> MediaInfo | None:
    """Probe ``path``.

    Returns ``None`` when the file is absent, empty, or unreadable — callers
    treat that as "not usable" rather than raising, because the pipeline probes
    speculatively (resumption checks, container sniffing).
    """
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_VIDEO_BYTES:
        _logger.debug("not probing %s: missing or too small", path)
        return None

    streams = runner.probe_streams(path)
    fmt = runner.probe_format(path)
    if not streams and not fmt:
        return None

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    return MediaInfo(
        path=path,
        duration=_as_float(fmt.get("duration")),
        width=_as_int(video.get("width")) if video else None,
        height=_as_int(video.get("height")) if video else None,
        video_codec=_as_str(video.get("codec_name")) if video else None,
        audio_codec=_as_str(audio.get("codec_name")) if audio else None,
        container=_as_str(fmt.get("format_name")),
    )


def dimensions(runner: FFmpegRunner, path: Path) -> tuple[int, int] | None:
    """Real pixel dimensions, or ``None`` when they cannot be determined.

    Never invents a resolution; see the module docstring.
    """
    info = probe(runner, path)
    if info is None:
        return None

    width, height = info.width, info.height
    if width is None or height is None:
        # has_dimensions is the same check; written out because an `assert`
        # would disappear under `python -O` and this is a real return path.
        return None
    return (width, height)


def is_valid_video(runner: FFmpegRunner, path: Path) -> bool:
    """Whether ``path`` is a complete, playable video.

    Guards the resumption path: a download killed mid-write leaves a file that
    exists and has a plausible size, so existence and size are not enough.
    """
    info = probe(runner, path)
    if info is None:
        return False
    if info.is_playable:
        return True
    _logger.debug("rejecting %s: video=%s duration=%s", path, info.video_codec, info.duration)
    return False


def find_downloaded_video(directory: Path) -> Path | None:
    """First media file in ``directory``, preferring the largest.

    ``v0.1`` returned whichever ``glob`` yielded first, so a leftover 2 KB
    fragment could shadow a complete download. Size ordering makes the complete
    file win.
    """
    candidates = [
        f
        for f in directory.glob("download.*")
        if f.is_file() and f.suffix.lower() in _VIDEO_EXTENSIONS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.stat().st_size)


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return str(value) if value is not None else None
