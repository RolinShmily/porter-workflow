"""Gather the facts a workflow decision depends on.

The resolver's whole job is to choose *without guessing*, so everything it needs
to know is collected here, before a plan exists: what kind of input this is,
whether it has video and audio, which subtitle tracks the platform advertises,
and what is wrong with it (if anything). No media is downloaded, and a subtitle
track is not claimed to be usable until it has actually been fetched and parsed.

This module never raises for a bad source. A missing file or dead link is a
:class:`~porter.core.workflow.model.SourceProbe` with a blocker warning, because
"this source is unusable" is a plan outcome, not an exception. Genuine faults --
a cancelled job, a broken install -- still propagate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from porter.core.asr.platform_subs import (
    SUPPORTED_SUBTITLE_SUFFIXES,
    load_supplied_subtitles,
)
from porter.core.context import RunContext
from porter.core.errors import PorterError
from porter.core.logging import get_logger
from porter.core.media.ffmpeg import FFmpegRunner, FFmpegTools
from porter.core.media.probe import MediaInfo, probe
from porter.core.models.inspection import InspectionResult
from porter.core.platforms.base import SUBTITLE_NAME, SUBTITLE_ZH_NAME, YtDlpExtractor
from porter.core.platforms.inspector import inspect_url
from porter.core.platforms.registry import registry
from porter.core.platforms.ydl import has_audio_stream
from porter.core.utils.source import normalize_source_input
from porter.core.workflow.model import (
    PlanIssue,
    SourceKind,
    SourceProbe,
    TrackInfo,
)

__all__ = ["audio_suffixes", "probe_source", "subtitle_suffixes"]

_logger = get_logger(__name__)

#: Suffixes treated as audio-only input. Kept as data: classification is one
#: table lookup, not a chain of guesses about the filename.
_AUDIO_SUFFIXES = frozenset(
    {
        ".aac",
        ".aif",
        ".aiff",
        ".alac",
        ".flac",
        ".m4a",
        ".mp3",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
    }
)

#: Video suffixes, copied from the local preparer's allowlist. Duplicated here
#: deliberately: this module classifies input, and importing the preparer would
#: drag ffmpeg preparation code into `porter probe` for a suffix set.
_VIDEO_SUFFIXES = frozenset(
    {
        ".3gp",
        ".avi",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogv",
        ".ts",
        ".webm",
        ".wmv",
    }
)


def subtitle_suffixes() -> frozenset[str]:
    """Formats accepted as a subtitle input."""
    return frozenset(SUPPORTED_SUBTITLE_SUFFIXES)


def audio_suffixes() -> frozenset[str]:
    """Formats recognised as audio-only input."""
    return _AUDIO_SUFFIXES


def probe_source(source: str | Path, ctx: RunContext) -> SourceProbe:
    """Describe ``source`` without downloading it or running a single stage."""
    text, is_url = normalize_source_input(source)

    if is_url:
        return _probe_url(text, ctx)

    path = _local_path(text)
    suffix = path.suffix.lower()

    if suffix in SUPPORTED_SUBTITLE_SUFFIXES:
        return _probe_subtitle(path)
    if suffix in _AUDIO_SUFFIXES:
        return _probe_media(path, ctx, kind=SourceKind.AUDIO)
    return _probe_media(path, ctx, kind=SourceKind.LOCAL_VIDEO)


# ----------------------------------------------------------------------
# URL
# ----------------------------------------------------------------------


def _probe_url(url: str, ctx: RunContext) -> SourceProbe:
    result = inspect_url(url, ctx)
    if not result.is_valid:
        return _from_inspection(
            result,
            warnings=(
                PlanIssue(
                    code="SOURCE_UNUSABLE",
                    message=result.error_message or "the link could not be inspected",
                    fix="check the link, or pass cookies when the platform requires login",
                    stage=None,
                ),
            ),
        )

    tracks, source_lang = _tracks_from_inspection(result)
    warnings: list[PlanIssue] = []
    if tracks:
        warnings.append(
            PlanIssue(
                code="TRACKS_UNVERIFIED",
                severity="warning",
                message=(
                    "the platform lists subtitle tracks, but they have not been "
                    "fetched; the plan treats that route as requested, not guaranteed"
                ),
                fix=(
                    "pass --transcript whisper-local to plan ASR despite the listed "
                    "track, or accept the track as a request rather than a guarantee"
                ),
            )
        )

    return _from_inspection(
        result,
        tracks=tracks,
        source_lang=source_lang,
        warnings=tuple(warnings),
    )


def _from_inspection(
    result: InspectionResult,
    *,
    tracks: tuple[TrackInfo, ...] = (),
    source_lang: str | None = None,
    warnings: tuple[PlanIssue, ...] = (),
) -> SourceProbe:
    return SourceProbe(
        kind=SourceKind.URL,
        source=result.input_url,
        platform=result.platform,
        title=result.title,
        duration_seconds=result.duration_seconds,
        width=result.width,
        height=result.height,
        is_vertical=result.is_vertical,
        has_video=result.has_video,
        has_audio=has_audio_stream(result.raw_info) if result.raw_info else False,
        source_lang=source_lang,
        tracks=tracks,
        warnings=warnings,
    )


def _tracks_from_inspection(
    result: InspectionResult,
) -> tuple[tuple[TrackInfo, ...], str | None]:
    """Ask the platform spec which tracks it would request.

    ``has_subtitles`` is a *listing*, not a promise: Instagram advertises nothing
    and fetches nothing, and x's spec deliberately returns an empty plan. Reading
    the same ``plan_subtitles`` the downloader uses is the only way the probe can
    agree with PREPARE about what "has subtitles" means.
    """
    handler = registry().find_or_none(result.canonical_url)
    if not isinstance(handler, YtDlpExtractor):
        return (), None

    plan = handler.plan_subtitles(result.raw_info)
    if not plan:
        return (), None

    source_lang = None
    tracks: list[TrackInfo] = []
    for destination, (lang, is_auto) in plan.items():
        role: Literal["source", "target", "other"] = "other"
        if destination == SUBTITLE_NAME:
            role = "source"
            source_lang = lang
        elif destination == SUBTITLE_ZH_NAME:
            role = "target"
        tracks.append(
            TrackInfo(
                lang=lang,
                role=role,
                auto_generated=is_auto,
                verified=False,
            )
        )
    return tuple(tracks), source_lang


# ----------------------------------------------------------------------
# Local
# ----------------------------------------------------------------------


def _local_path(text: str) -> Path:
    if text.startswith("file://"):
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        return Path(url2pathname(urlparse(text).path))
    return Path(text).expanduser()


def _probe_subtitle(path: Path) -> SourceProbe:
    checks: list[PlanIssue] = []
    if not path.is_file():
        return SourceProbe(
            kind=SourceKind.SUBTITLE,
            source=str(path),
            has_video=False,
            has_audio=False,
            warnings=(
                PlanIssue(
                    code="SOURCE_NOT_FOUND",
                    message=f"subtitle file not found: {path}",
                    fix="check the path",
                ),
            ),
        )

    try:
        # Parsed to prove it is usable, not to count cues; the count is not part
        # of the probe contract and SourceProbe has no field for it.
        load_supplied_subtitles(path)
    except (PorterError, OSError) as exc:
        checks.append(
            PlanIssue(
                code="SOURCE_UNUSABLE",
                message=str(exc),
                fix="pass a non-empty .srt or .vtt file",
            )
        )

    return SourceProbe(
        kind=SourceKind.SUBTITLE,
        source=str(path),
        title=path.stem,
        has_video=False,
        has_audio=False,
        tracks=(
            TrackInfo(
                lang="und",
                role="source",
                verified=True,
                path=str(path),
            ),
        ),
        warnings=tuple(checks),
    )


def _probe_media(path: Path, ctx: RunContext, *, kind: SourceKind) -> SourceProbe:
    if kind is SourceKind.LOCAL_VIDEO and path.suffix.lower() not in _VIDEO_SUFFIXES:
        _logger.info(
            "local input %s has an unrecognised extension %r; probing it anyway",
            path,
            path.suffix,
        )
    if not path.is_file():
        return SourceProbe(
            kind=kind,
            source=str(path),
            has_video=False,
            has_audio=False,
            warnings=(
                PlanIssue(
                    code="SOURCE_NOT_FOUND",
                    message=f"local file not found: {path}",
                    fix="check the path",
                ),
            ),
        )

    info: MediaInfo | None = None
    problem: PlanIssue | None = None
    try:
        tools = FFmpegTools.resolve(
            ctx.config.ffmpeg.ffmpeg_path,
            ctx.config.ffmpeg.ffprobe_path,
        )
        info = probe(FFmpegRunner(tools), path)
    except (PorterError, OSError) as exc:
        problem = PlanIssue(
            code="PROBE_FAILED",
            message=f"could not probe {path}: {exc}",
            fix="install ffmpeg/ffprobe or check that the file is readable",
        )

    if info is None and problem is None:
        problem = PlanIssue(
            code="SOURCE_UNUSABLE",
            message=f"local file is empty or not decodable: {path}",
            fix="check the file; a truncated download cannot be processed",
        )

    if info is None:
        return SourceProbe(
            kind=kind,
            source=str(path),
            title=path.stem,
            has_video=False,
            has_audio=False,
            warnings=(problem,) if problem is not None else (),
        )

    has_video = info.has_video
    # A media file classified as video by suffix but carrying no video stream is
    # an audio file that was named badly. Report the measured truth; the resolver
    # decides whether that is usable for the requested task.
    measured_kind = kind
    if kind is SourceKind.LOCAL_VIDEO and not has_video and info.has_audio:
        measured_kind = SourceKind.AUDIO

    return SourceProbe(
        kind=measured_kind,
        source=str(path),
        platform="local",
        title=path.stem,
        duration_seconds=info.duration,
        width=info.width,
        height=info.height,
        is_vertical=info.is_vertical,
        has_video=has_video,
        has_audio=info.has_audio,
        warnings=(),
    )
