"""PREPARE for a video that is already on disk.

Why this exists
---------------
v0.1 required a URL; every input went through yt-dlp. The MCP scenario needs the
other case: a client has a file on its own machine and wants subtitles for it.
There is nothing to download, but everything else in PREPARE still applies --
standardise the master, extract the audio, enhance it for ASR, take a cover,
write ``metadata.json``.

That work is not duplicated here. It lives in :mod:`porter.media.prepare` and is
shared with :class:`~porter.platforms.base.YtDlpExtractor`; this module is only
the source-specific part: read the file, derive an identity for it, and hand off.

Identity
--------
A URL comes with a video id, which is what makes re-running a job land in the
same task directory and reuse the master. A path has no such thing, so one is
derived: ``local-<8 hex of the resolved path>``. Stable across runs (so resumption
works), unique per file (so two videos with the same name in different directories
do not collide), and visibly not a platform id.

The file's own directory is *not* used as the output root. Writing next to the
user's video would be a surprise, and it would fail outright on a read-only
mount.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from porter.context import RunContext
from porter.errors import ExtractionError
from porter.events import ArtifactKind, ArtifactReady, Phase
from porter.logging import get_logger
from porter.media.ffmpeg import FFmpegRunner
from porter.media.prepare import (
    AUDIO_NAME,
    VIDEO_NAME,
    apply_measured_dimensions,
    enhance_audio,
    extract_cover_frame,
    master_is_complete,
    standardize_master,
)
from porter.media.probe import probe
from porter.models.materials import RawMaterials, TaskLayout
from porter.models.metadata import VideoMetadata

__all__ = ["LocalFileDownloader", "local_video_id"]

_logger = get_logger(__name__)

#: Suffixes treated as video. Deliberately an allowlist: without one, a typo like
#: ``video.txt`` would be handed to ffmpeg and fail with a decoder error that says
#: nothing about the real problem.
VIDEO_SUFFIXES = frozenset(
    {
        ".mp4",
        ".mkv",
        ".mov",
        ".webm",
        ".avi",
        ".m4v",
        ".flv",
        ".wmv",
        ".mpg",
        ".mpeg",
        ".ts",
        ".m2ts",
        ".3gp",
        ".ogv",
    }
)

#: Cap on how much of the path feeds the id hash. Not a security measure -- it
#: just keeps the hash cost constant for pathological paths.
_ID_HASH_BYTES = 8


def local_video_id(path: Path) -> str:
    """A stable, filesystem-safe id for a local video.

    Hashes the **resolved** path, so ``./video.mp4`` and ``/abs/video.mp4`` are
    the same video and re-running from a different working directory resumes the
    same task directory instead of starting a second one.
    """
    resolved = Path(path).expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    return f"local-{digest[:_ID_HASH_BYTES]}"


class LocalFileDownloader:
    """Implements :class:`~porter.ports.LocalPreparer` for a path on disk.

    The name says "downloader" to match the port it stands beside in the
    pipeline; nothing is downloaded. Stateless -- a plain class rather than a
    dataclass, matching :class:`~porter.platforms.downloader.PlatformDownloader`
    -- so the pipeline can build it once and reuse it across jobs.
    """

    name = "local"

    def prepare(
        self,
        path: Path,
        ctx: RunContext,
        *,
        runner: FFmpegRunner | None = None,
    ) -> RawMaterials:
        """Standardise the local file into ``raw/`` and return the raw materials.

        Args:
            path: The video file. Tilde is expanded; the path is resolved.
            ctx: Run context (output root, ``force``, ``audio_denoise``, events).
            runner: ffmpeg runner, built from the config when omitted.

        Returns:
            Paths to the produced assets, plus metadata measured from the file.

        Raises:
            ExtractionError: If the file is missing, unreadable, not a video, or
                cannot be standardised.
            MediaError: If the transcode fails part-way through.
            JobCancelled: If the caller cancelled between steps.
        """
        runner = runner or FFmpegRunner()
        source = self._validate(path)
        layout = self._layout(source, ctx)

        ctx.check_cancelled()
        video_path = layout.raw_dir / VIDEO_NAME
        audio_path = layout.raw_dir / AUDIO_NAME

        # Probe once, before the branch: the reuse check needs it either way, and
        # re-probing inside both arms is how the first draft ended up running
        # ffprobe twice on the common path.
        metadata = self._metadata(source, layout, runner)

        # Resumption, matching the URL path: a complete master from a previous
        # run is reused unless `force` says otherwise. The check is the same
        # function, so a truncated master is rejected either way.
        if not ctx.options.force and master_is_complete(runner, video_path, audio_path):
            _logger.info("reusing the existing master in %s", layout.raw_dir)
            ctx.progress(Phase.PREPARE, 95.0, "reusing existing raw materials")
        else:
            ctx.check_cancelled()
            master = standardize_master(runner, source, layout, ctx)
            # Correct the declared dimensions from the real pixels, exactly as
            # the URL path does. Tri-state: an unmeasurable master keeps what
            # ffprobe reported above rather than a guess.
            metadata = apply_measured_dimensions(metadata, master)

        ctx.check_cancelled()
        enhanced = enhance_audio(runner, audio_path, layout, ctx)

        # A local file has no thumbnail, so the poster frame stands in for one.
        cover = extract_cover_frame(runner, video_path, layout, ctx)

        metadata_path = layout.write_metadata(metadata)
        layout.cleanup_tmp()

        materials = RawMaterials(
            layout=layout,
            video=video_path,
            audio=audio_path,
            audio_enhanced=enhanced,
            cover=cover,
            # No sidecar subtitles are picked up: a `.srt` beside the video could
            # be either the source or the translation, and guessing wrong would
            # silently skip ASR or overwrite the user's file. The only way to
            # opt in is to name one explicitly with --subtitle-file.
            subtitle_src=None,
            subtitle_zh=None,
            metadata_path=metadata_path,
            info=metadata,
        )

        ctx.emit(
            ArtifactReady(
                phase=Phase.PREPARE,
                kind=ArtifactKind.VIDEO,
                path=video_path,
            )
        )
        ctx.progress(Phase.PREPARE, 100.0, "raw materials ready")
        return materials

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _validate(path: Path) -> Path:
        """Resolve ``path`` and refuse anything that is not a readable video file.

        Every failure here is a ``ExtractionError`` naming the path, because the
        caller is a human who mistyped a filename or an MCP client that sent a
        path the server cannot see. Both need to know *which* path was wrong --
        a bare "no such file" is useless when the path came over a network.
        """
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ExtractionError(
                f"local video not found: {candidate}",
                path=str(candidate),
            ) from exc
        except OSError as exc:
            raise ExtractionError(
                f"local video could not be resolved: {candidate} ({exc})",
                path=str(candidate),
            ) from exc

        if resolved.is_dir():
            raise ExtractionError(
                f"local video is a directory, not a file: {resolved}",
                path=str(resolved),
            )
        if not resolved.is_file():
            raise ExtractionError(
                f"local video is not a regular file: {resolved}",
                path=str(resolved),
            )
        if resolved.stat().st_size == 0:
            raise ExtractionError(
                f"local video is empty: {resolved}",
                path=str(resolved),
            )
        if resolved.suffix.lower() not in VIDEO_SUFFIXES:
            raise ExtractionError(
                f"local video has an unrecognised extension {resolved.suffix!r}: {resolved}",
                path=str(resolved),
                supported=sorted(VIDEO_SUFFIXES),
            )
        return resolved

    @staticmethod
    def _layout(source: Path, ctx: RunContext) -> TaskLayout:
        """Derive the task directory from the file's name and identity."""
        layout = TaskLayout.build(
            ctx.output_root,
            local_video_id(source),
            source.stem,
        ).ensure_dirs()
        _logger.info("task directory: %s", layout.task_dir)
        return layout

    @staticmethod
    def _metadata(source: Path, layout: TaskLayout, runner: FFmpegRunner) -> VideoMetadata:
        """Describe the file. Dimensions and duration come from ffprobe.

        Measured here rather than assumed, and left as ``None`` when ffprobe
        cannot read the file: the subtitle layout keys off the orientation, and a
        fabricated landscape default is how a vertical video ends up with
        landscape styling.
        """
        info = probe(runner, source)
        if info is None:
            raise ExtractionError(
                f"local video is not decodable: {source}",
                path=str(source),
            )

        return VideoMetadata(
            id=layout.video_id,
            title=source.stem,
            safe_title=layout.safe_title,
            url=source.as_uri(),
            platform="local",
            duration=info.duration,
            width=info.width,
            height=info.height,
            is_vertical=bool(info.is_vertical),
            has_official_subtitle=False,
            raw_metadata={
                "source_path": str(source),
                "container": info.container,
                "video_codec": info.video_codec,
                "audio_codec": info.audio_codec,
            },
        )
