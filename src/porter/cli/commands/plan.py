"""``porter plan`` — resolve one workflow before committing to it.

The plan is derived, not authored. The source is probed, a workflow name is
chosen from what the source actually is, and exactly one capability is selected
for each stage. The JSON form is the execution contract accepted by
``porter run --plan``: it contains every non-secret setting the run needs.

Exit code 0 means the plan is feasible. A plan that says "this will fail" is
still a successful *plan*, but scripts need the same boolean the run would use,
so infeasibility is reported non-zero.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from porter.cli import planning, render

if TYPE_CHECKING:
    from porter.core.workflow.model import WorkflowPlan

__all__ = ["configure", "render_plan", "run"]


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "plan",
        help="Resolve the workflow and capabilities for a source, without running.",
        description=(
            "Probe the source, choose a workflow (url.subtitled, url.asr, "
            "local.asr, ...), and select exactly one capability per stage. The "
            "result is a JSON plan that 'porter run --plan' executes. Nothing "
            "heavy runs here: no media is downloaded and no ffmpeg job starts."
        ),
    )
    parser.add_argument("source", metavar="SOURCE", help="Video URL, local media path, or subtitle.")
    planning.add_arguments(parser)
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the resolved plan as JSON.",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    from porter.core.config import resolve
    from porter.core.context import RunContext
    from porter.core.errors import PorterError
    from porter.core.models.request import JobOptions
    from porter.core.workflow.resolver import resolve_plan

    config = resolve(args.config_path)
    overrides = planning.overrides_from_args(args, config)
    ctx = RunContext(
        job_id="plan",
        options=JobOptions(
            output_dir=overrides.output_dir,
            cookies_file=overrides.cookies_file,
            cookies_browser=overrides.cookies_browser,
        ),
        config=config,
    )

    try:
        plan = resolve_plan(args.source, overrides, ctx)
    except PorterError as exc:
        render.warn(exc.message)
        return render.EXIT_ERROR

    if args.as_json:
        render.emit_json(plan.to_dict())
    else:
        render.info(render.banner("PORTER WORKFLOW PLAN"))
        render.value(render_plan(plan))

    if not plan.feasible:
        for issue in plan.blockers:
            render.warn(f"{issue.code}: {issue.message}")
        return render.EXIT_ERROR
    return render.EXIT_OK


def render_plan(plan: WorkflowPlan) -> str:
    """Render the plan as a compact human report.

    The source facts and the stage list are the two things that change a
    decision; the full parameter set is available with ``--json`` and printing
    it every time would bury them.
    """
    probe = plan.source_probe
    lines = [
        f"Source     : {plan.source}",
        f"Kind       : {probe.kind.value}" + (f" ({probe.platform})" if probe.platform else ""),
        f"Workflow   : {plan.workflow.value}",
        f"Task       : {plan.task.value}",
    ]
    if probe.duration_seconds:
        size = f"{probe.width}x{probe.height}" if probe.width and probe.height else "unknown"
        orientation = "vertical" if probe.is_vertical else "horizontal"
        lines.append(f"Media      : {probe.duration_seconds:.1f}s, {size}, {orientation}")
    if probe.tracks:
        lines.append(
            "Tracks     : "
            + ", ".join(
                f"{track.role}={track.lang}"
                + (" (auto)" if track.auto_generated else "")
                + ("" if track.verified else " (listed)")
                for track in probe.tracks
            )
        )
    lines.append("Stages     :")
    for stage in plan.stages:
        lines.append(f"  {stage.stage.value:<10} {stage.capability}")
    lines.append(f"Feasible   : {'yes' if plan.feasible else 'NO'}")
    for issue in plan.issues:
        label = "blocked " if issue.severity == "blocker" else "note    "
        lines.append(f"  {label}: [{issue.code}] {issue.message}")
        if issue.alternatives:
            lines.append(f"            alternatives: {', '.join(issue.alternatives)}")
        if issue.fix:
            lines.append(f"            fix: {issue.fix}")
    return "\n".join(lines)
