"""Tools: operations on artifacts, outside the workflow plan.

The stage tools work on **files, not sources**: an SRT in and an SRT out, a
video plus an ASS in and a video out. That is what makes them worth having next
to ``porter_job_start`` -- an agent can translate a subtitle file it already has,
transcribe one local clip, or burn a track it just corrected, without re-running
acquisition.

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
    ``porter_job_start`` rather than accepted and then timing out.

**No fallback chains.** ``porter_translate`` resolves a plan for the subtitle
and builds a translation chain containing exactly the capability that plan
selected. ``porter_transcribe`` resolves a transcribe plan and runs it through
the same executor as a job, a single ASR capability. The encoder verdict comes
from the same ``EncoderSelector`` the burn phase uses, so a stage tool and a full
run cannot disagree about which capability or encoder they picked.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from porter.core.config import resolve
from porter.core.context import RunContext
from porter.core.errors import PorterError
from porter.core.events import JobState, null_sink
from porter.core.logging import get_logger
from porter.core.models.request import JobOptions
from porter.core.models.subtitle import SubtitleItem, SubtitleSet
from porter.mcp.limits import HEAVY, LIGHT
from porter.mcp.stdout_guard import protect

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
    from porter.core.subtitles.srt import parse_srt

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

        # Resolve a plan so the choice between translation peers goes through the
        # same availability and paid-consent rules as a job. The plan is not
        # executed here: this tool has no media and wants the cue objects back,
        # so it uses the plan's single capability directly.
        from porter.core.workflow.executor import build_translation_chain
        from porter.core.workflow.model import Stage
        from porter.mcp.tools.planning import plan_for

        try:
            plan = plan_for(
                str(source),
                task="subtitle",
                output_dir=str(out_dir),
                target_lang=target_lang,
                translate=backend,
                force_translate=True,
            )
        except (PorterError, ValueError) as exc:
            message = exc.message if isinstance(exc, PorterError) else str(exc)
            return {"ok": False, "error": message}

        if not plan.feasible:
            issue = plan.blockers[0] if plan.blockers else None
            return {
                "ok": False,
                "error": issue.message if issue is not None else "the plan is not feasible",
                "fix": issue.fix if issue is not None else None,
                "alternatives": list(issue.alternatives) if issue is not None else [],
                "issues": [item.model_dump(mode="json") for item in plan.issues],
            }

        translate_stage = plan.stage(Stage.TRANSLATE)
        if translate_stage is None:
            return {"ok": False, "error": "the plan chose not to translate this subtitle"}

        options = JobOptions(
            output_dir=out_dir,
            target_lang=target_lang,
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
                translated = build_translation_chain(translate_stage.capability).translate(
                    subtitles, target_lang, ctx
                )
            except PorterError as exc:
                return {"ok": False, "error": exc.message, "details": dict(exc.details)}

        limit = max_cues if max_cues > 0 else None
        merged = len(translated.items) != len(items)
        return {
            "ok": True,
            "target_lang": target_lang,
            "capability": translate_stage.capability,
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

        from porter.core.media.burn import burn_hardsub
        from porter.core.media.encode import EncoderSelector
        from porter.core.media.ffmpeg import FFmpegRunner

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
            "return the cues. Resolves a transcribe plan and runs exactly one ASR "
            "capability, with no fallback. For a URL use porter_plan with "
            "task='transcribe' and porter_job_start: fetching a URL is a download, "
            "and a download is a long task."
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
                    "Use porter_plan with task='transcribe' for a URL, then pass "
                    "that plan to porter_job_start: it returns a job id immediately "
                    "and you poll porter_job_status, which does not hit the "
                    "tool-call timeout."
                ),
            }

        path = Path(source.removeprefix("file://")).expanduser()
        if not path.exists():
            return {"ok": False, "error": f"no such file: {path}"}
        if not path.is_file():
            return {"ok": False, "error": f"not a file: {path}"}

        # A plan fixes the single ASR capability and records the paid-consent
        # decision. Running the executor means this tool cannot fall back to a
        # different engine than the one reported.
        from porter.core.workflow.executor import execute_plan
        from porter.core.workflow.model import Stage
        from porter.mcp.tools.planning import plan_for

        try:
            plan = plan_for(str(path), task="transcribe", transcript=engine)
        except (PorterError, ValueError) as exc:
            message = exc.message if isinstance(exc, PorterError) else str(exc)
            return {"ok": False, "error": message}

        if not plan.feasible:
            issue = plan.blockers[0] if plan.blockers else None
            return {
                "ok": False,
                "error": issue.message if issue is not None else "the plan is not feasible",
                "fix": issue.fix if issue is not None else None,
                "alternatives": list(issue.alternatives) if issue is not None else [],
                "issues": [item.model_dump(mode="json") for item in plan.issues],
            }

        ctx = RunContext(
            job_id="stage-transcribe",
            options=JobOptions(output_dir=Path(plan.runtime.output_dir)),
            config=config,
            events=null_sink,
        )

        with HEAVY:
            try:
                outcome = execute_plan(plan, ctx)
            except PorterError as exc:
                return {"ok": False, "error": exc.message, "details": dict(exc.details)}

        workflow = outcome.workflow
        if workflow.state is not JobState.DONE:
            error = workflow.error
            payload: dict[str, Any] = {
                "ok": False,
                "error": error.message if error is not None else "recognition produced nothing",
                "state": workflow.state.value,
                "plan_id": workflow.plan_id,
            }
            if error is not None:
                payload["alternatives"] = list(error.alternatives)
                payload["suggested_workflow"] = (
                    error.suggested_workflow.value
                    if error.suggested_workflow is not None
                    else None
                )
            return payload

        srt_path = workflow.artifacts.get("source_transcript")
        if not srt_path:
            return {
                "ok": False,
                "error": "recognition finished without writing a source transcript",
                "state": workflow.state.value,
                "plan_id": workflow.plan_id,
            }

        try:
            items = _read_srt(Path(srt_path))
        except PorterError as exc:
            return {"ok": False, "error": exc.message, "plan_id": workflow.plan_id}

        limit = max_cues if max_cues > 0 else None
        transcript_stage = plan.stage(Stage.TRANSCRIPT)
        return {
            "ok": True,
            "plan_id": workflow.plan_id,
            "capability": transcript_stage.capability if transcript_stage else None,
            "cue_count": len(items),
            "cues": _cues(items, limit),
            "cues_truncated": limit is not None and len(items) > limit,
            "subtitle_srt": srt_path,
            "transcript_txt": workflow.artifacts.get("transcript_txt"),
            "task_dir": workflow.task_dir,
        }
