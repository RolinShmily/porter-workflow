"""Tools: the three phase-level stages.

The stage tools work on **artifacts**, not on pipeline runs: an SRT in and an
SRT out, a video plus an ASS in and a video out. That is what makes them worth
having next to ``porter_job_start`` -- an agent can translate a subtitle file it
already has, or burn a track it just corrected, without re-running acquisition.

They are blocking, and the job API remains the reliable path because an MCP
tool call has a timeout of a minute or two, and a 1080p burn takes tens of
minutes. So the rule applied here is that **every accepted input is one whose work
finishes quickly**:

``porter_translate``
    A local SRT. No download, no recognition, just network round-trips.
``porter_burn``
    A local video and a local ASS. ffmpeg only.
``porter_transcribe``
    A **local** media file. Accepted because the alternative -- fetching a URL
    first -- is a download, and a download is exactly the long task the job API
    exists for. A URL is refused with a pointer to
    ``porter_job_start(only_phase="transcribe")`` rather than accepted and then
    timing out. The input was first specified as ``audio|url``; the ``url`` half moved to
    the job API, and this is the honest place to record that.

None of these re-derive anything. The chain comes from ``Pipeline.default(ctx)``
and the encoder verdict from the same ``EncoderSelector`` the burn phase uses, so
a stage tool and a full run cannot disagree about which backend or encoder they
picked.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from porter.config import resolve
from porter.context import RunContext
from porter.errors import PorterError
from porter.events import Phase, null_sink
from porter.logging import get_logger
from porter.models.request import BurnMode, JobOptions, JobRequest
from porter.models.subtitle import SubtitleItem, SubtitleSet
from porter_mcp.limits import HEAVY, LIGHT
from porter_mcp.stdout_guard import protect

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]

_logger = get_logger(__name__)

#: Written next to a standalone SRT when no output directory is given.
DEFAULT_TRANSLATED_SUBDIR = "translated"


def _cues(items: list[SubtitleItem], limit: int | None = None) -> list[dict[str, Any]]:
    """Render cues as JSON, optionally truncated.

    A truncated list is labelled by the caller rather than silently shortened:
    an agent that reads forty cues and reports "the transcript is forty lines" is
    worse off than one told the list was cut.
    """
    chosen = items if limit is None else items[:limit]
    return [
        {
            "index": item.index,
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
            "source": item.source_text,
            "target": item.target_text,
        }
        for item in chosen
    ]


def _read_srt(path: Path) -> list[SubtitleItem]:
    """Parse an SRT file, raising ``PorterError`` for anything unusable."""
    from porter.subtitles.srt import parse_srt

    if not path.exists():
        raise PorterError(f"no such file: {path}")
    if not path.is_file():
        raise PorterError(f"not a file: {path}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PorterError(f"could not read {path}: {exc}") from exc
    items = parse_srt(text)
    if not items:
        raise PorterError(
            f"{path} contains no subtitle cues",
            hint="An empty file usually means the download failed, not that the "
            "video has no speech.",
        )
    return items


def register(server: FastMCP) -> None:
    """Attach the stage tools to ``server``."""

    @server.tool(
        name="porter_translate",
        description=(
            "Translate a local .srt subtitle file into target_lang and write the "
            "bilingual and target-only subtitle files. Use this to translate an "
            "existing transcript without running the whole pipeline. Returns the "
            "cues plus the paths written."
        ),
    )
    @protect
    def porter_translate(
        srt_path: str,
        target_lang: str = "zh-Hans",
        backend: str | None = None,
        output_dir: str | None = None,
        max_cues: int = 0,
    ) -> dict[str, Any]:
        try:
            config = resolve(None)
        except PorterError as exc:
            return {"ok": False, "error": f"configuration could not be resolved: {exc}"}

        source = Path(srt_path).expanduser()
        try:
            items = _read_srt(source)
        except PorterError as exc:
            return {"ok": False, "error": exc.message}

        out_dir = (
            Path(output_dir).expanduser()
            if output_dir
            else source.parent / DEFAULT_TRANSLATED_SUBDIR
        )
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"could not create {out_dir}: {exc}"}

        options = JobOptions(
            output_dir=out_dir,
            target_lang=target_lang,
            translator=backend,
            # No recognition happens here, so the denoise pass would be wasted work
            # on a source that has already been through it.
            audio_denoise=False,
        )
        ctx = RunContext(job_id="stage-translate", options=options, config=config, events=null_sink)
        subtitles = SubtitleSet(
            subtitle_bilingual_srt=out_dir / "subtitle.srt",
            subtitle_bilingual_ass=out_dir / "subtitle.ass",
            subtitle_zh_srt=out_dir / "subtitle_zh.srt",
            subtitle_zh_ass=out_dir / "subtitle_zh.ass",
            items=items,
            transcript_json_path=out_dir / "transcript.json",
            transcript_txt_path=out_dir / "transcript.txt",
        )

        with LIGHT:
            try:
                from porter.pipeline import Pipeline

                # The same chain a full run would build, from the same constructor.
                translated = Pipeline.default(ctx).translator.translate(
                    subtitles, target_lang, ctx
                )
            except PorterError as exc:
                return {"ok": False, "error": exc.message, "details": getattr(exc, "context", None)}

        limit = max_cues if max_cues > 0 else None
        merged = len(translated.items) != len(items)
        return {
            "ok": True,
            "target_lang": target_lang,
            "input_cue_count": len(items),
            "cue_count": len(translated.items),
            # Reported rather than quietly reconciled: the chain merges short
            # adjacent cues to rebuild sentences, which is what makes ASR shards
            # translatable but also joins two unrelated short lines in an SRT that
            # was already segmented. A caller editing subtitles needs to know its
            # cue boundaries may have moved.
            "cues_merged": merged,
            "cues": _cues(translated.items, limit),
            "cues_truncated": limit is not None and len(translated.items) > limit,
            "note": (
                f"{len(items)} input cues became {len(translated.items)}: adjacent "
                "short cues were merged to translate whole sentences, and each "
                "merged cue spans its originals' full time range. Pass a source "
                "whose cues are already sentence-length to avoid this."
                if merged
                else None
            ),
            "subtitle_bilingual_srt": str(translated.subtitle_bilingual_srt),
            "subtitle_zh_srt": str(translated.subtitle_zh_srt),
            "subtitle_zh_ass": str(translated.subtitle_zh_ass),
            "transcript_txt": str(translated.transcript_txt_path),
        }

    @server.tool(
        name="porter_burn",
        description=(
            "Burn a local .ass (or .srt) subtitle file into a local video and "
            "return the output path. Use this to re-render a release video after "
            "editing a subtitle track, without re-running recognition or "
            "translation. Hardware encoding is used when available."
        ),
    )
    @protect
    def porter_burn(
        video: str,
        ass: str,
        output: str | None = None,
    ) -> dict[str, Any]:
        # No config resolution here: a burn needs ffmpeg and an encoder verdict,
        # neither of which comes from porter's config file. Resolving one anyway
        # would make this tool fail on a broken config it does not read.
        video_path = Path(video).expanduser()
        subtitle_path = Path(ass).expanduser()
        for label, path in (("video", video_path), ("subtitle", subtitle_path)):
            if not path.exists():
                return {"ok": False, "error": f"no such {label} file: {path}"}
            if not path.is_file():
                return {"ok": False, "error": f"{label} is not a file: {path}"}

        out_path = (
            Path(output).expanduser()
            if output
            else video_path.with_name(f"{video_path.stem}_hardsub{video_path.suffix}")
        )
        # ffmpeg writes a temporary file next to the destination and renames it, so
        # the parent must exist before the burn starts. The pipeline does this in
        # render_release; a standalone burn has to do it too, or an explicit
        # output directory that does not exist yet fails with a bare OSError.
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"could not create {out_path.parent}: {exc}"}

        from porter.media.burn import burn_hardsub
        from porter.media.encode import EncoderSelector
        from porter.media.ffmpeg import FFmpegRunner

        runner = FFmpegRunner()
        # The same selector the burn phase uses, so a stage burn and a full run
        # cannot disagree about the hardware verdict -- including `auto_tune`.
        try:
            ffmpeg_config = resolve(None).ffmpeg
        except PorterError:
            ffmpeg_config = None
        profile = EncoderSelector.for_config(runner, ffmpeg_config).select()

        with HEAVY:
            try:
                burn_hardsub(
                    runner,
                    video_path,
                    subtitle_path,
                    out_path,
                    profile=profile,
                )
            except PorterError as exc:
                # RenderError is a PorterError, so one handler covers a failed
                # encode and a missing ffmpeg alike.
                return {"ok": False, "error": exc.message}

        return {
            "ok": True,
            "video": str(out_path),
            "encoder": profile.name,
            "subtitle": str(subtitle_path),
            "note": (
                "The .ass carries its own styling, so this burns it as authored. "
                "To change the look, regenerate the subtitle file."
            ),
        }

    @server.tool(
        name="porter_transcribe",
        description=(
            "Produce a transcript for a LOCAL media file (audio or video) and "
            "return the cues. Uses the platform's own subtitle track when the "
            "source is a URL, so for a URL call porter_job_start with "
            "only_phase='transcribe' instead: fetching a URL is a download, and a "
            "download is a long task."
        ),
    )
    @protect
    def porter_transcribe(
        source: str,
        engine: str | None = None,
        max_cues: int = 0,
    ) -> dict[str, Any]:
        try:
            config = resolve(None)
        except PorterError as exc:
            return {"ok": False, "error": f"configuration could not be resolved: {exc}"}

        # Refuse a URL here rather than accepting it and blocking for minutes on a
        # download the caller cannot cancel. The input is documented as
        # ``audio|url``; the URL half belongs to the job API.
        looks_like_url = "://" in source and not source.startswith("file://")
        if looks_like_url:
            return {
                "ok": False,
                "error": "porter_transcribe takes a local file, not a URL",
                "hint": (
                    "Use porter_job_start with only_phase='transcribe' for a URL: "
                    "it returns a job id immediately and you poll "
                    "porter_job_status, which does not hit the tool-call timeout."
                ),
            }

        path = Path(source.removeprefix("file://")).expanduser()
        if not path.exists():
            return {"ok": False, "error": f"no such file: {path}"}
        if not path.is_file():
            return {"ok": False, "error": f"not a file: {path}"}

        try:
            options = JobOptions(
                output_dir=config.output_dir,
                asr_engine=engine,
                # Stop after recognition: this tool exists to produce the
                # transcript, and running on would translate and encode work the
                # caller did not ask for.
                only_phase=Phase.TRANSCRIBE,
                burn=BurnMode.SKIP,
            )
            request = JobRequest.from_source(str(path), options)
        except (PorterError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

        ctx = RunContext(job_id="stage-transcribe", options=options, config=config, events=null_sink)

        with HEAVY:
            from porter.pipeline import Pipeline

            result = Pipeline.default(ctx).run(request, ctx)

        if result.subtitles is None:
            return {
                "ok": False,
                "error": result.error.message if result.error else "recognition produced nothing",
                "state": result.state.value,
            }

        limit = max_cues if max_cues > 0 else None
        items = result.subtitles.items
        return {
            "ok": True,
            "cue_count": len(items),
            "cues": _cues(items, limit),
            "cues_truncated": limit is not None and len(items) > limit,
            "subtitle_srt": str(result.subtitles.subtitle_bilingual_srt),
            "transcript_txt": str(result.subtitles.transcript_txt_path),
            "task_dir": str(result.task_dir) if result.task_dir else None,
        }
