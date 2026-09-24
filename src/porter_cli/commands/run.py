"""``porter run`` — execute the localization pipeline.

Options here are the CLI's projection of :class:`porter.models.JobOptions`.
Secrets (API keys) are deliberately *not* accepted as flags — they would land in
shell history and process listings. Set them in config or the environment
instead.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from porter.events import Phase
from porter.models import BurnMode
from porter_cli import render

if TYPE_CHECKING:
    # Only ever used in annotations. Importing them at runtime would make the CLI
    # pay for pydantic model construction before argparse has even parsed, and
    # `from __future__ import annotations` means they are never evaluated.
    from porter.config import PorterConfig
    from porter.models.request import JobResult

__all__ = ["configure", "run"]


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``run`` command."""
    parser = subparsers.add_parser(
        "run",
        help="Download, transcribe, translate and burn a video.",
        description=(
            "Run the full pipeline for one video. Use 'porter <URL>' as a shorthand."
        ),
    )
    parser.add_argument(
        "source",
        metavar="SOURCE",
        help=(
            "Video URL (YouTube, X, Bilibili, TikTok, Instagram), or a path to a "
            "local video file. file:// URLs are accepted for local files."
        ),
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        metavar="DIR",
        help="Where to create the task folder. Defaults to the configured output_dir.",
    )
    parser.add_argument(
        "--burn",
        choices=[mode.value for mode in BurnMode],
        help="Which release videos to render. 'skip' produces subtitles only.",
    )
    parser.add_argument(
        "--asr-engine",
        metavar="ENGINE",
        help=(
            "Try this ASR engine first. The other engines stay as fallback: "
            "whisper-local, whisper-api, bcut, google-web, videocaptioner, "
            "or a VideoCaptioner engine (bijian, jianying, whisper-cpp)."
        ),
    )
    parser.add_argument(
        "--translator",
        metavar="BACKEND",
        help=(
            "Try this translation backend first. The others stay as fallback: "
            "llm, bing, google, mymemory, videocaptioner-llm, videocaptioner."
        ),
    )
    parser.add_argument(
        "--llm-model",
        metavar="MODEL",
        help="Override the configured LLM model for this run, e.g. deepseek-chat.",
    )
    parser.add_argument(
        "--target-lang",
        metavar="LANG",
        help="Target language tag. Defaults to zh-Hans.",
    )
    parser.add_argument("--cookies", metavar="FILE", help="Netscape cookies.txt for gated media.")
    parser.add_argument(
        "--subtitle-file",
        metavar="FILE",
        help=(
            "Use an existing .srt/.vtt as the source track, instead of the "
            "platform's subtitles or speech recognition."
        ),
    )
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Read cookies from a browser profile (chrome, firefox, edge, brave, ...).",
    )
    parser.add_argument(
        "--no-denoise",
        action="store_true",
        default=None,
        help=(
            "Skip the ASR vocal-enhancement pass. Left unset, asr.audio_denoise "
            "from the configuration decides."
        ),
    )
    parser.add_argument(
        "--only-phase",
        choices=["prepare", "transcribe", "translate", "burn"],
        metavar="PHASE",
        help="Stop after this phase. Earlier phases still run, because each phase "
        "consumes the previous one's output.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore cached stage results and redo the work.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Write the JobResult as JSON to stdout instead of progress text.",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Execute the pipeline and report the outcome.

    Exit codes are the machine-readable result: 0 done, 1 failed, 2 misuse, 130
    cancelled. ``--json`` writes the :class:`~porter.models.JobResult` to stdout
    and nothing else, so progress and warnings stay on stderr where they cannot
    corrupt the document.
    """
    from porter.config import resolve
    from porter.context import RunContext
    from porter.errors import JobCancelled, PorterError
    from porter.events import ErrorInfo, JobState, null_sink
    from porter.jobs import JobRegistry, JobStore
    from porter.models.request import JobOptions, JobRequest, JobResult
    from porter.pipeline import Pipeline

    config = resolve(args.config_path)
    options = JobOptions(
        output_dir=_output_dir(args, config),
        burn=BurnMode(args.burn) if args.burn else BurnMode.DUAL,
        asr_engine=args.asr_engine,
        translator=args.translator,
        llm_model=args.llm_model,
        target_lang=args.target_lang or "zh-Hans",
        cookies_file=args.cookies,
        cookies_browser=args.cookies_from_browser,
        subtitle_file=args.subtitle_file,
        audio_denoise=config.asr.audio_denoise if args.no_denoise is None else False,
        only_phase=Phase(args.only_phase) if args.only_phase else None,
        force=args.force,
    )

    # One positional argument covers both kinds of source; JobRequest decides
    # which it is. See JobRequest.from_source for the rule.
    request = JobRequest.from_source(args.source, options)

    ctx = RunContext(
        job_id=_job_id(args.source),
        options=options,
        config=config,
        # With --json the terminal must stay quiet, but the pipeline still emits:
        # null_sink discards rather than suppressing, so no phase has to know
        # whether anyone is listening.
        events=null_sink if args.as_json else render.render_event,
    )

    render.info(render.banner("PORTER WORKFLOW"))

    # Register the job before running it, so that an interrupted run is visible
    # from another terminal: `porter jobs list` finds it, and because the record
    # carries the owner's identity, a run that was killed shows up as failed
    # rather than sitting at "running" forever.
    #
    # The id is derived from the source rather than random. A rerun of the same
    # video then replaces the previous record instead of accumulating a
    # near-duplicate, which matches how the task directory is already reused.
    store = JobStore(registry=JobRegistry())
    job = store.create(request, job_id=ctx.job_id)
    store.attach(job, ctx)

    pipeline = Pipeline.default(ctx)
    try:
        result = pipeline.run(request, ctx)
    except (JobCancelled, PorterError) as exc:
        # Expected failures normally come back as a JobResult rather than an
        # exception. Reaching here means it happened before the pipeline could
        # build one, or escaped its own handlers. Record it either way: a job
        # left at "running" in the shared file is a lie that another terminal
        # (or a polling MCP client) would wait on.
        cancelled = isinstance(exc, JobCancelled)
        store.finish(
            job,
            JobResult(
                job_id=ctx.job_id,
                state=JobState.CANCELLED if cancelled else JobState.FAILED,
                error=None if cancelled else ErrorInfo.from_exception(exc),
            ),
        )
        render.warn("cancelled" if cancelled else str(exc))
        return render.EXIT_CANCELLED if cancelled else render.EXIT_ERROR

    except KeyboardInterrupt:
        # Ctrl+C. Recorded here rather than left to the reaper: this process is
        # still alive and knows exactly what happened, and making another terminal
        # infer an interruption from a dead PID is strictly worse information.
        store.finish(job, JobResult(job_id=ctx.job_id, state=JobState.CANCELLED))
        render.warn("interrupted; partial output is left in the task directory")
        return render.EXIT_CANCELLED

    store.finish(job, result)

    if args.as_json:
        render.emit_json(result.model_dump(mode="json"))
    else:
        _report(result)

    return render.EXIT_OK if result.error is None else render.EXIT_ERROR


def _output_dir(args: argparse.Namespace, config: PorterConfig) -> Path:
    """``-o`` wins over the configured output dir."""
    if args.output_dir:
        return Path(args.output_dir)
    return Path(config.output_dir)


def _job_id(source: str) -> str:
    """A short, stable-per-source id, so two runs of one input do not collide.

    The MCP frontend uses random ids because it tracks concurrent jobs; the CLI
    runs one job per process, and a human-readable id makes the task folder
    easier to correlate with the command they typed.

    This is the *job* id, distinct from the task directory's ``video_id``. A
    local file gets its own id from its resolved path
    (:func:`~porter.platforms.local.local_video_id`), so the same video always
    lands in the same task directory however it was spelled.
    """
    import hashlib

    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
    return f"run-{digest}"


def _report(result: JobResult) -> None:
    """Render the finished job to the terminal.

    Everything goes to stderr except the file paths, which are the result and
    therefore go to stdout where they can be captured.
    """
    render.info(f"{render.render_job_state(result.state)} {result.state.value}")

    if result.error is not None:
        render.warn(result.error.message)
        for key, value in (result.error.details or {}).items():
            render.info(f"    {key}: {value}")

    if result.task_dir is not None:
        render.value(str(result.task_dir))

    subtitles = result.subtitles
    if subtitles is not None:
        for path in (
            subtitles.subtitle_bilingual_srt,
            subtitles.subtitle_zh_srt,
            subtitles.subtitle_bilingual_ass,
            subtitles.subtitle_zh_ass,
        ):
            if path.is_file():
                render.value(str(path))
