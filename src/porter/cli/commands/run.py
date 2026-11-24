"""``porter run`` — execute one resolved workflow.

The CLI no longer assembles a fallback chain. It resolves a
:class:`~porter.core.workflow.model.WorkflowPlan` (or loads one made earlier) and
the executor runs exactly the stages and capabilities that plan names. A failure
is reported with alternatives for the next plan; it is not silently retried on a
different engine.

``--plan`` is the agent-facing form: the plan carries the source, output
directory, target language, burn mode, supplied subtitle and cookies, so the
execution is reproducible from the JSON alone.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from porter.cli import planning, render

if TYPE_CHECKING:
    from porter.core.config import PorterConfig
    from porter.core.workflow.model import WorkflowPlan, WorkflowResult

__all__ = ["configure", "run"]


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "run",
        help="Resolve and execute a workflow for one source.",
        description=(
            "Resolve a workflow for the source, then execute its stages with one "
            "capability per stage. Use 'porter <URL>' as a shorthand. Pass --plan "
            "to execute a plan produced by 'porter plan --json'."
        ),
    )
    parser.add_argument(
        "source",
        nargs="?",
        metavar="SOURCE",
        help=(
            "Video URL (YouTube, X, Bilibili, TikTok, Instagram), a local media "
            "path, or a subtitle file. Optional when --plan is used."
        ),
    )
    parser.add_argument(
        "--plan",
        metavar="FILE",
        help="Execute a previously resolved plan.json instead of resolving one now.",
    )
    planning.add_arguments(parser)
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Write the WorkflowResult as JSON to stdout instead of progress text.",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    from porter.core.config import resolve
    from porter.core.context import RunContext
    from porter.core.errors import JobCancelled, PorterError
    from porter.core.events import ErrorInfo, JobState, null_sink
    from porter.core.jobs import JobRegistry, JobStore
    from porter.core.models.request import JobOptions, JobRequest, JobResult
    from porter.core.workflow.executor import execute_plan

    if args.plan is None and not args.source:
        render.warn("run needs a SOURCE, or --plan FILE")
        return render.EXIT_MISUSE

    config = resolve(args.config_path)
    try:
        plan = (
            _load_plan(args.plan, args)
            if args.plan
            else _resolve(args, config)
        )
    except (PorterError, OSError, ValueError) as exc:
        render.warn(str(exc))
        return render.EXIT_ERROR

    job_id = _job_id(plan.source)
    options = JobOptions(output_dir=Path(plan.runtime.output_dir))
    ctx = RunContext(
        job_id=job_id,
        options=options,
        config=config,
        # With --json the terminal must stay quiet, but the engine still emits:
        # null_sink discards rather than suppressing, so no stage has to know
        # whether anyone is listening.
        events=null_sink if args.as_json else render.render_event,
    )

    render.info(render.banner("PORTER WORKFLOW"))

    store = JobStore(registry=JobRegistry())
    request = JobRequest.from_source(plan.source, options)
    job = store.create(request, job_id=job_id)
    store.attach(job, ctx)

    try:
        outcome = execute_plan(plan, ctx)
    except (JobCancelled, PorterError) as exc:
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
        store.finish(job, JobResult(job_id=ctx.job_id, state=JobState.CANCELLED))
        render.warn("interrupted; partial output is left in the task directory")
        return render.EXIT_CANCELLED

    store.finish(job, outcome.job)

    if args.as_json:
        render.emit_json(outcome.workflow.to_dict())
    else:
        _report(outcome.workflow)

    if outcome.workflow.state is JobState.DONE:
        return render.EXIT_OK
    if outcome.workflow.state is JobState.CANCELLED:
        return render.EXIT_CANCELLED
    return render.EXIT_ERROR


def _resolve(args: argparse.Namespace, config: PorterConfig) -> WorkflowPlan:
    from porter.core.context import RunContext
    from porter.core.models.request import JobOptions
    from porter.core.workflow.resolver import resolve_plan

    overrides = planning.overrides_from_args(args, config)
    ctx = RunContext(
        job_id=_job_id(args.source),
        options=JobOptions(
            output_dir=overrides.output_dir,
            cookies_file=overrides.cookies_file,
            cookies_browser=overrides.cookies_browser,
        ),
        config=config,
    )
    return resolve_plan(args.source, overrides, ctx)


def _load_plan(path: str, args: argparse.Namespace) -> WorkflowPlan:
    """Read and validate a plan, applying the few runtime overrides that make sense.

    Capability choices are frozen: changing one would make the plan describe a
    pipeline nobody approved. Output directory and ``--force`` are execution
    settings, so they may be overridden and the plan is resealed.
    """
    from pydantic import ValidationError

    from porter.core.workflow.model import WorkflowPlan

    text = Path(path).read_text(encoding="utf-8")
    try:
        plan = WorkflowPlan.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError(f"invalid plan file {path}: {exc}") from exc

    ignored = [
        flag
        for flag, value in (
            ("--task", args.task),
            ("--burn", args.burn),
            ("--target-lang", args.target_lang),
            ("--transcript", args.transcript),
            ("--translate", args.translate),
            ("--render", args.render),
            ("--subtitle-file", args.subtitle_file),
            ("--llm-model", args.llm_model),
            ("--cookies", args.cookies),
            ("--cookies-from-browser", args.cookies_from_browser),
            ("--no-denoise", args.no_denoise),
            ("--allow-paid", args.allow_paid),
            ("--force-translate", args.force_translate),
            ("--only-phase", args.only_phase),
        )
        if value
    ]
    if ignored:
        render.warn(
            "these flags are ignored with --plan because the plan already fixes "
            f"those choices: {', '.join(ignored)}"
        )

    runtime = plan.runtime.model_copy(
        update={
            "output_dir": (
                str(args.output_dir) if args.output_dir else plan.runtime.output_dir
            ),
            "force": plan.runtime.force or bool(args.force),
        }
    )
    return plan.model_copy(update={"runtime": runtime}).sealed()


def _job_id(source: str) -> str:
    """A short, stable-per-source id, so reruns replace rather than pile up."""
    import hashlib

    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
    return f"run-{digest}"


def _report(result: WorkflowResult) -> None:
    """Render the workflow result. Paths go to stdout; commentary to stderr."""
    render.info(f"{render.render_job_state(result.state)} {result.state.value}")

    if result.error is not None:
        render.warn(result.error.message)
        for key, value in (result.error.details or {}).items():
            render.info(f"    {key}: {value}")
        if result.error.alternatives:
            render.info(f"    alternatives: {', '.join(result.error.alternatives)}")
        if result.error.suggested_workflow is not None:
            render.info(f"    suggested workflow: {result.error.suggested_workflow.value}")

    if result.task_dir is not None:
        render.value(result.task_dir)

    seen: set[str] = set()
    for path in result.artifacts.values():
        if path not in seen:
            seen.add(path)
            render.value(path)
