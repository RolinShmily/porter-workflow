"""The single implementation of "extract raw materials from a URL".

Replaces five pasted copies
---------------------------
``v0.1`` declared :class:`BasePlatformExtractor` with two abstract methods
(``can_handle``, ``extract_raw_materials``) and no shared implementation. Each
subclass therefore pasted the entire ~400-line pipeline into
``extract_raw_materials``. Diffing the five copies showed the *sequence* was
identical and only the *data* differed — that data now lives in
:class:`~porter.platforms.spec.PlatformSpec`.

``YtDlpExtractor`` is the template: it reads a spec and runs the one pipeline.

What ``fetch`` does, in order
-----------------------------
1. resolve metadata (one yt-dlp info call),
2. pick source and Chinese subtitle languages from the available tracks,
3. short-circuit if a previous run already produced a complete master,
4. download each wanted subtitle track into its own directory,
5. download the media streams,
6. standardise to ``raw/video.mp4`` and extract ``raw/audio.wav``,
7. enhance the audio for ASR,
8. correct the metadata dimensions from the *actual* master,
9. fetch the cover,
10. write ``raw/metadata.json``,
11. delete ``.tmp``.

Deliberate differences from v0.1
--------------------------------
**Subtitle files are addressed, not globbed.** v0.1 downloaded all languages
into one directory and then searched with ``glob("download*.<lang>.*")``, plus a
"rescue" pass that grabbed any leftover subtitle file whose name did not look
Chinese. That heuristics-heavy approach mis-assigned tracks when two language
tags shared a prefix. Each track is now fetched into ``.tmp/subs/<lang>/`` with
a fixed output name, so the file is either there or it is not.

**Covers go through ffmpeg.** v0.1 required Pillow to convert the thumbnail to
JPEG, which silently dropped the cover whenever the ``[images]`` extra was not
installed. ffmpeg is already a hard dependency and converts any thumbnail
format, so the extra is no longer needed for this path.

**Resumption validates the master.** v0.1 checked file size alone, so a
download killed mid-write was accepted as complete and every later phase worked
from a truncated file. The check now probes the container.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from porter.context import RunContext
from porter.errors import CapabilityMissingError, ExtractionError, JobCancelled
from porter.events import ArtifactKind, ArtifactReady, Phase
from porter.logging import get_logger
from porter.media.ffmpeg import FFmpegRunner
from porter.media.prepare import (
    AUDIO_NAME,
    COVER_NAME,
    VIDEO_NAME,
    apply_measured_dimensions,
    enhance_audio,
    existing_file,
    master_is_complete,
    standardize_master,
)
from porter.media.probe import find_downloaded_video
from porter.models.materials import RawMaterials, TaskLayout
from porter.models.metadata import VideoMetadata
from porter.models.request import JobOptions
from porter.platforms.spec import PlatformSpec
from porter.platforms.ydl import (
    YdlPolicy,
    build_ydl,
    download_progress_hook,
    has_video_stream,
)
from porter.subtitles.srt import bilibili_json_to_srt, vtt_to_srt
from porter.utils.text import sanitize_filename

__all__ = ["YtDlpExtractor"]

_logger = get_logger(__name__)

#: Metadata keys copied verbatim into ``VideoMetadata.raw_metadata`` for
#: debugging. Deliberately a small allowlist: yt-dlp's info dict can be
#: megabytes, and dumping it all makes ``metadata.json`` unreadable.
_RAW_METADATA_KEYS = (
    "id",
    "extractor",
    "extractor_key",
    "webpage_url",
    "upload_date",
    "timestamp",
    "view_count",
    "like_count",
    "comment_count",
    "tags",
    "categories",
    "availability",
    "live_status",
    "language",
    "age_limit",
)

#: Filenames inside ``raw/`` that are specific to URL sources. The rest -- the
#: master, the audio, the cover and ``metadata.json`` -- are shared with the local
#: file path and live in :mod:`porter.media.prepare`.
SUBTITLE_NAME = "subtitle.srt"
SUBTITLE_ZH_NAME = "subtitle_zh.srt"

#: Maps a raw/ subtitle filename to the event kind describing it.
_SUBTITLE_KINDS = {
    SUBTITLE_NAME: ArtifactKind.SUBTITLE_SRC,
    SUBTITLE_ZH_NAME: ArtifactKind.SUBTITLE_ZH_SRT,
}


@dataclass(frozen=True, eq=False)
class YtDlpExtractor:
    """A platform extractor driven entirely by its :class:`PlatformSpec`."""

    spec: PlatformSpec

    @property
    def name(self) -> str:
        """Stable platform identifier, also the registry key."""
        return self.spec.name

    @property
    def display_name(self) -> str:
        """Human-facing platform name."""
        return self.spec.display_name

    def can_handle(self, url: str) -> bool:
        """Return True when this extractor claims ``url``."""
        return self.spec.can_handle(url)

    def video_id(self, url: str) -> str | None:
        """Best-effort video id from the URL alone."""
        return self.spec.video_id(url)

    # ------------------------------------------------------------------
    # Phase 0: probe
    # ------------------------------------------------------------------

    def probe(
        self,
        url: str,
        ctx: RunContext | None = None,
        *,
        policy: YdlPolicy | None = None,
    ) -> VideoMetadata:
        """Resolve metadata for ``url`` without downloading anything.

        Backs ``porter inspect`` and step 1 of :meth:`fetch`.

        Args:
            url: The video URL.
            ctx: Run context, used for cookie settings and progress events.
            policy: Override the yt-dlp policy outright (tests, debugging).

        Returns:
            Standardised metadata.

        Raises:
            ExtractionError: If yt-dlp returns no usable information.
        """
        resolved = policy or self.policy_for(ctx)
        info = self.extract_info(url, policy=resolved, extract_flat=False)
        return self._build_metadata(url, info)

    # ------------------------------------------------------------------
    # Phase PREPARE: fetch
    # ------------------------------------------------------------------

    def fetch(
        self,
        url: str,
        ctx: RunContext,
        *,
        runner: FFmpegRunner | None = None,
        policy: YdlPolicy | None = None,
    ) -> RawMaterials:
        """Download and standardise everything under ``raw/``.

        Args:
            url: The video URL.
            ctx: Run context (output root, cookies, cancellation, events).
            runner: ffmpeg runner. Built from ``ctx.config.ffmpeg`` when omitted.
            policy: Override the yt-dlp policy outright (tests, debugging).

        Returns:
            Paths to the produced assets, plus the corrected metadata.

        Raises:
            JobCancelled: If the caller cancelled between steps.
            ExtractionError: If no usable media could be downloaded.
            CapabilityMissingError: If ffmpeg is absent.
        """
        runner = runner or FFmpegRunner(self._tools(ctx))
        base_policy = policy or self.policy_for(ctx)

        # --- 1. metadata -------------------------------------------------
        ctx.check_cancelled()
        ctx.progress(Phase.PREPARE, 0.0, "resolving video metadata")
        info = self.extract_info(url, policy=base_policy, extract_flat=False)
        metadata = self._build_metadata(url, info)

        layout = TaskLayout.build(
            ctx.output_root,
            metadata.id,
            metadata.title,
        ).ensure_dirs()
        _logger.info("task directory: %s", layout.task_dir)

        # --- 2. subtitle plan -------------------------------------------
        plan = self.plan_subtitles(info)

        # --- 3. resumption ----------------------------------------------
        video_path = layout.raw_dir / VIDEO_NAME
        audio_path = layout.raw_dir / AUDIO_NAME
        if not ctx.options.force and master_is_complete(runner, video_path, audio_path):
            _logger.info("reusing the existing master in %s", layout.raw_dir)
            ctx.progress(Phase.PREPARE, 95.0, "reusing existing raw materials")
        else:
            # --- 4. subtitles (best effort) -----------------------------
            self._download_subtitles(url, base_policy, layout, plan, ctx)

            # --- 5. media -----------------------------------------------
            ctx.check_cancelled()
            source = self._download_media(url, base_policy, layout, ctx)

            # --- 6. standardise + audio ---------------------------------
            ctx.check_cancelled()
            master = standardize_master(runner, source, layout, ctx)

            # Correct the metadata from the real pixels. The info dict's
            # dimensions are frequently absent (TikTok) or wrong (rotated
            # uploads), and the subtitle layout keys off the orientation.
            metadata = apply_measured_dimensions(metadata, master)

        # --- 7. ASR enhancement ------------------------------------------
        enhanced = enhance_audio(runner, audio_path, layout, ctx)

        # --- 8. cover ----------------------------------------------------
        cover = self._fetch_cover(runner, metadata, layout, ctx)

        # --- 9. metadata -------------------------------------------------
        metadata_path = layout.write_metadata(metadata)

        # --- 10. cleanup -------------------------------------------------
        layout.cleanup_tmp()

        materials = RawMaterials(
            layout=layout,
            video=video_path,
            audio=audio_path,
            audio_enhanced=enhanced,
            cover=cover,
            subtitle_src=existing_file(layout.raw_dir / SUBTITLE_NAME),
            subtitle_zh=existing_file(layout.raw_dir / SUBTITLE_ZH_NAME),
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

    # ------------------------------------------------------------------
    # Policy plumbing
    # ------------------------------------------------------------------

    def policy_for(self, ctx: RunContext | None) -> YdlPolicy:
        """Build the yt-dlp policy from a run context.

        Cookies come from the job options because they are per-job; the format
        selector and player clients come from the spec because they are
        per-platform.
        """
        options: JobOptions | None = ctx.options if ctx is not None else None
        return YdlPolicy(
            cookies_file=str(options.cookies_file) if options and options.cookies_file else None,
            cookies_browser=options.cookies_browser if options else None,
            player_clients=self.spec.player_clients,
            extractor_args=self.spec.extractor_args,
        )

    @staticmethod
    def _tools(ctx: RunContext) -> Any:
        """Resolve ffmpeg/ffprobe from config."""
        from porter.media.ffmpeg import FFmpegTools

        return FFmpegTools.resolve(
            ctx.config.ffmpeg.ffmpeg_path,
            ctx.config.ffmpeg.ffprobe_path,
        ).require()

    # ------------------------------------------------------------------
    # Step 1: metadata
    # ------------------------------------------------------------------

    def fetch_info(
        self,
        url: str,
        ctx: RunContext | None = None,
        *,
        policy: YdlPolicy | None = None,
    ) -> dict[str, Any]:
        """Return yt-dlp's raw info dict for ``url``, downloading nothing.

        Public because callers other than :meth:`probe` need the *unprocessed*
        dict: ``porter inspect`` reads the format table to decide whether the
        post actually contains a video, which :meth:`probe` discards.

        Raises:
            ExtractionError: If yt-dlp returns no usable information.
        """
        return self.extract_info(url, policy=policy or self.policy_for(ctx))

    def extract_info(
        self,
        url: str,
        *,
        policy: YdlPolicy,
        extract_flat: bool = False,
    ) -> dict[str, Any]:
        """Run yt-dlp's metadata extraction and return its info dict."""
        merged = replace(policy, extract_flat=extract_flat)

        try:
            with build_ydl(merged) as ydl:
                info = ydl.extract_info(url, download=False)
        except ImportError as exc:  # pragma: no cover - yt-dlp is a core dep
            raise CapabilityMissingError(
                "yt-dlp",
                "yt-dlp is required to resolve video metadata",
            ) from exc

        if info is None:
            raise ExtractionError(
                f"yt-dlp returned no metadata for this URL: {url}",
                url=url,
                platform=self.spec.name,
            )

        # A carousel or playlist resolves to a list of entries; take the first
        # that actually carries a video stream (Instagram carousels, TikTok
        # photo posts).
        entries = info.get("entries")
        if entries and not has_video_stream(info):
            info = self._first_video_entry(url, list(entries))

        return dict(info)

    def _first_video_entry(self, url: str, entries: list[Any]) -> dict[str, Any]:
        """Pick the first entry that carries a video stream.

        Raises when none does, rather than returning an entry anyway. A TikTok
        photo post or an all-image Instagram carousel really has no video, and
        handing a video-less entry downstream only moves the failure to the
        standardisation step — where the message no longer mentions that the
        post was a slideshow.
        """
        candidates = [e for e in entries if isinstance(e, dict)]

        for entry in candidates:
            if has_video_stream(entry):
                return entry

        raise ExtractionError(
            f"this {self.spec.display_name} URL has no downloadable video "
            f"({len(candidates)} entries, none with a media stream)",
            url=url,
            platform=self.spec.name,
        )

    def _build_metadata(self, url: str, info: dict[str, Any]) -> VideoMetadata:
        """Map yt-dlp's info dict onto :class:`VideoMetadata`."""
        video_id = str(info.get("id") or self.spec.video_id(url) or "unknown_id")
        raw_title = info.get("title") or info.get("description") or ""
        title = self.spec.clean_title(raw_title, video_id)

        width = _as_int(info.get("width"))
        height = _as_int(info.get("height"))
        # Platforms that omit dimensions fall back to the spec's declared default
        # rather than guessing, because the ASS style depends on the orientation.
        is_vertical = height > width if width and height else self.spec.default_vertical

        subtitles = info.get("subtitles") or {}
        auto_captions = info.get("automatic_captions") or {}
        official_lang = self.spec.select_source_lang(subtitles, is_auto=False)
        if official_lang is None:
            official_lang = self.spec.select_source_lang(auto_captions, is_auto=True)

        return VideoMetadata(
            id=video_id,
            title=title,
            safe_title=sanitize_filename(title),
            url=url,
            platform=self.spec.name,
            uploader=info.get("uploader"),
            channel=info.get("channel"),
            duration=_as_float(info.get("duration")),
            width=width,
            height=height,
            is_vertical=is_vertical,
            description=info.get("description"),
            thumbnail_url=info.get("thumbnail"),
            has_official_subtitle=official_lang is not None,
            official_subtitle_lang=official_lang,
            raw_metadata={k: info[k] for k in _RAW_METADATA_KEYS if k in info},
        )

    # ------------------------------------------------------------------
    # Step 2: subtitle plan
    # ------------------------------------------------------------------

    def plan_subtitles(self, info: dict[str, Any]) -> dict[str, tuple[str, bool]]:
        """Decide which subtitle tracks to request.

        Returns:
            ``{destination filename: (language tag, is_auto)}``. Empty when the
            platform has no subtitle tracks, in which case ASR runs.
        """
        if not self.spec.subtitles.remote:
            return {}

        human = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}

        source_lang = self.spec.select_source_lang(human, is_auto=False)
        source_is_auto = False
        if source_lang is None:
            source_lang = self.spec.select_source_lang(auto, is_auto=True)
            source_is_auto = source_lang is not None

        plan: dict[str, tuple[str, bool]] = {}
        if source_lang:
            plan[SUBTITLE_NAME] = (source_lang, source_is_auto)

        if self.spec.subtitles.prefer_existing_chinese:
            zh_lang = self.spec.select_chinese_lang(human) or self.spec.select_chinese_lang(auto)
            if zh_lang and zh_lang != source_lang:
                plan[SUBTITLE_ZH_NAME] = (zh_lang, zh_lang not in human)

        if plan:
            _logger.info("subtitle plan: %s", ", ".join(f"{k} <- {v[0]}" for k, v in plan.items()))
        return plan

    def _download_subtitles(
        self,
        url: str,
        policy: YdlPolicy,
        layout: TaskLayout,
        plan: dict[str, tuple[str, bool]],
        ctx: RunContext,
    ) -> dict[str, Path]:
        """Fetch each planned subtitle track into its own directory.

        Best effort throughout: a missing caption track means ASR runs instead,
        which is a slower job, not a failed one.
        """
        if not plan:
            return {}

        downloaded: dict[str, Path] = {}
        for dest_name, (lang, is_auto) in plan.items():
            ctx.check_cancelled()
            scratch = layout.tmp_dir / "subs" / sanitize_filename(lang)
            scratch.mkdir(parents=True, exist_ok=True)

            sub_policy = replace(
                policy,
                skip_download=True,
                write_subtitles=not is_auto,
                write_auto_subs=is_auto,
                subtitle_langs=(lang,),
                outtmpl=str(scratch / "sub.%(ext)s"),
                ignoreerrors=True,
            )

            try:
                with build_ydl(sub_policy) as ydl:
                    ydl.download([url])
            except JobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - best effort by design
                _logger.warning("could not download the %s subtitle track: %s", lang, exc)
                continue

            found = _find_subtitle_file(scratch)
            if found is None:
                _logger.warning("no subtitle file was produced for language %s", lang)
                continue

            dest = layout.raw_dir / dest_name
            suffix = found.suffix.lower()
            if suffix == ".json":
                # Bilibili hands its CC track back as JSON with cue times in
                # seconds, and yt-dlp writes it through verbatim. Copying it would
                # produce a file that parses as zero cues -- an empty transcript
                # that looks like a successful fetch, so ASR would be skipped and
                # the job would produce subtitles with no text in them.
                converted = bilibili_json_to_srt(
                    found.read_text(encoding="utf-8", errors="replace")
                )
                if not converted:
                    _logger.warning("the %s track was JSON but held no cues", lang)
                    continue
                dest.write_text(converted)
            elif suffix == ".vtt":
                dest.write_text(vtt_to_srt(found.read_text(encoding="utf-8", errors="replace")))
            else:
                shutil.copyfile(found, dest)
            downloaded[dest_name] = dest
            ctx.emit(
                ArtifactReady(
                    phase=Phase.PREPARE,
                    kind=_SUBTITLE_KINDS[dest_name],
                    path=dest,
                )
            )
            _logger.info("subtitle %s ready (%s)", dest_name, lang)

        return downloaded

    # ------------------------------------------------------------------
    # Step 3: resumption
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Step 5: media
    # ------------------------------------------------------------------

    def _download_media(
        self,
        url: str,
        policy: YdlPolicy,
        layout: TaskLayout,
        ctx: RunContext,
    ) -> Path:
        """Download the media streams into ``.tmp`` and return the file.

        Retries once with a narrower client set when the platform rate-limits,
        but only for specs that declare it — an unconditional retry doubles the
        wait on a platform whose failure is not transient.
        """
        attempts: list[tuple[str, YdlPolicy]] = [
            (
                "primary",
                replace(
                    policy,
                    format=self.spec.format_selector,
                    outtmpl=str(layout.tmp_dir / "download.%(ext)s"),
                    merge_output_format="mp4",
                    concurrent_fragments=5,
                ),
            )
        ]
        if self.spec.retries_on_rate_limit:
            attempts.append(
                (
                    "rate-limit fallback",
                    replace(
                        attempts[0][1],
                        extractor_args={**self.spec.extractor_args, "youtube": {"player_client": ["android"]}},
                        concurrent_fragments=1,
                    ),
                )
            )

        last_error: Exception | None = None
        for label, attempt_policy in attempts:
            ctx.check_cancelled()
            hook = download_progress_hook(ctx.emit, phase=Phase.PREPARE)
            try:
                with build_ydl(attempt_policy, progress_hook=hook) as ydl:
                    ydl.download([url])
            except JobCancelled:
                # Cancellation is a control signal, not a download failure.
                # Catching it here would report "could not download" for a job
                # the caller deliberately stopped, and the retry would start a
                # second download the user just asked to abort.
                raise
            except Exception as exc:  # noqa: BLE001 - retried or reported below
                last_error = exc
                _logger.warning("%s download failed: %s", label, exc)

            found = find_downloaded_video(layout.tmp_dir)
            if found is not None:
                _logger.info("downloaded %s (%s attempt)", found.name, label)
                return found

        raise ExtractionError(
            f"could not download any media for this {self.spec.display_name} URL"
            + (f": {last_error}" if last_error else ""),
            url=url,
            platform=self.spec.name,
        )

    # ------------------------------------------------------------------
    # Steps 7-9: enhancement, cover, metadata
    # ------------------------------------------------------------------

    def _fetch_cover(
        self,
        runner: FFmpegRunner,
        metadata: VideoMetadata,
        layout: TaskLayout,
        ctx: RunContext,
    ) -> Path | None:
        """Download the thumbnail and convert it to JPEG.

        Conversion goes through ffmpeg rather than Pillow: ffmpeg is already a
        hard dependency and handles every format a CDN might serve, so the
        optional ``[images]`` extra is not needed here.
        """
        if not metadata.thumbnail_url:
            return None

        import requests

        raw = layout.tmp_dir / "thumb.raw"
        try:
            response = requests.get(metadata.thumbnail_url, timeout=15)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - a cover is optional
            _logger.warning("could not download the thumbnail: %s", exc)
            return None

        raw.write_bytes(response.content)
        dest = layout.raw_dir / COVER_NAME
        proc = runner.run(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(raw),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(dest),
            ],
            what="converting the cover image",
            check=False,
        )
        if proc.returncode != 0 or not dest.is_file():
            _logger.warning("could not convert the thumbnail to JPEG; continuing without a cover")
            return None

        ctx.emit(
            ArtifactReady(
                phase=Phase.PREPARE,
                kind=ArtifactKind.COVER,
                path=dest,
            )
        )
        return dest

def _find_subtitle_file(directory: Path) -> Path | None:
    """Find the subtitle track yt-dlp actually wrote.

    **yt-dlp inserts the language tag.** With ``outtmpl="sub.%(ext)s"`` and
    ``subtitleslangs=("en",)`` it writes ``sub.en.srt`` -- not ``sub.srt``. This
    code searched for the literal name, so it found nothing, every time, on every
    platform that has a subtitle track at all.

    Nothing surfaced it: a missing caption track is best-effort by design (ASR is
    supposed to take over), so the failure was one warning line in a log. The unit
    tests could not see it either, because their fake yt-dlp wrote ``sub.srt`` --
    a double more forgiving than the thing it stood in for. v0.1 globbed
    ``download*.{lang}.*`` and worked; this is that, with the format preference
    kept explicit.

    Preference is SRT, then VTT, then the JSON body bilibili returns.
    """
    candidates = [
        path for path in directory.glob("sub.*") if path.is_file() and path.stat().st_size > 0
    ]
    for wanted in (".srt", ".vtt", ".json"):
        for path in sorted(candidates):
            if path.suffix.lower() == wanted:
                return path
    return None


def _first_existing(directory: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


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
