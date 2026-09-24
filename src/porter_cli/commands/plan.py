"""``porter plan`` — predict what a job will do before committing to it.

The CLI counterpart of the MCP ``porter_plan`` tool. It exists because the plan
is the step that prevents wasted compute, and a plan that only exists over MCP
would leave the CLI path without it: an agent driving porter from a shell would
have to start a job to find out it was doomed.

Both frontends call the same :func:`porter.plan.plan_for`, which derives its
answer from ``Pipeline.default`` -- so the CLI cannot predict one thing and the
run do another.
"""

from __future__ import annotations

import argparse

from porter.errors import PorterError
from porter.models.plan import Plan
from porter_cli import render

__all__ = ["configure", "run"]


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``plan`` command."""
    parser = subparsers.add_parser(
        "plan",
        help="Predict phases, subtitle route and blockers without downloading.",
        description=(
            "Report what a job would do for this source: the phases that run, "
            "whether the platform's own subtitle track can be used or speech "
            "recognition is needed, whether translation is required, and whether "
            "the job is feasible at all. Inspects the source, so it takes a few "
            "seconds and downloads no media."
        ),
    )
    parser.add_argument("source", help="Video URL or local media path.")
    parser.add_argument(
        "--burn",
        choices=("dual", "zh-only", "bilingual-only", "skip"),
        default=None,
        help="Burn mode to plan for. Defaults to dual.",
    )
    parser.add_argument(
        "--target-lang",
        metavar="LANG",
        default=None,
        help="Target subtitle language. Defaults to zh-Hans.",
    )
    parser.add_argument(
        "--only-phase",
        choices=("prepare", "transcribe", "translate", "burn"),
        default=None,
        help="Plan to stop after this phase.",
    )
    parser.add_argument(
        "--cookies",
        metavar="FILE",
        help="Netscape cookies.txt, for links that require authentication.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Read cookies from a browser profile.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the plan as JSON instead of a formatted report.",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Produce the plan and report it.

    Exit codes match ``inspect``: 0 when the job is feasible, 1 when it is not.
    A plan is a prediction, and a prediction of "this will fail" is still a
    successful plan -- but callers script against "can I proceed", so an
    infeasible plan exits non-zero.
    """
    from porter.config import resolve
    from porter.context import RunContext
    from porter.models.request import BurnMode, JobOptions
    from porter.plan import plan_for

    config = resolve(args.config_path)
    options = JobOptions(
        output_dir=config.output_dir,
        burn=BurnMode(args.burn) if args.burn else BurnMode.DUAL,
        target_lang=args.target_lang or "zh-Hans",
        only_phase=args.only_phase,
        # The plan inspects the source, so it needs the same credentials the run
        # would need -- otherwise it reports "blocked: authentication required"
        # for a link that is perfectly usable, and the advice to pass cookies is
        # advice this very command could not follow.
        cookies_file=args.cookies,
        cookies_browser=args.cookies_from_browser,
    )
    # Build the context here and hand it over, rather than passing only the
    # options: ``plan_for`` resolves the configuration itself when given neither,
    # which silently dropped ``--config`` for everything the plan reads from
    # configuration (``asr.engine``, ``translator``, ffmpeg, subtitle style). The
    # plan then described a different run from the one the same command line
    # would actually perform.
    ctx = RunContext(job_id="plan", options=options, config=config)

    try:
        plan = plan_for(args.source, ctx=ctx)
    except PorterError as exc:
        render.warn(str(exc))
        return render.EXIT_ERROR

    if args.as_json:
        render.emit_json(plan.to_dict())
    else:
        render.info(render.banner("PORTER JOB PLAN"))
        render.value(_format_plan(plan))

    if not plan.feasible:
        for issue in plan.blocking_issues:
            render.warn(issue)
        return render.EXIT_ERROR
    return render.EXIT_OK


def _format_plan(plan: Plan) -> str:
    """Render a :class:`~porter.models.plan.Plan` as a readable report.

    Only the fields that change a decision are shown. The full structure is
    available with ``--json``, and printing every backend on every invocation
    would bury the two lines that matter: the route and the blockers.
    """
    lines = [
        f"Source     : {plan.source}",
        f"Kind       : {plan.kind}" + (f" ({plan.platform})" if plan.platform else ""),
        f"Phases     : {', '.join(plan.phases)}",
    ]

    if plan.duration_seconds:
        size = f"{plan.width}x{plan.height}" if plan.width and plan.height else "unknown"
        orientation = "vertical" if plan.is_vertical else "horizontal"
        lines.append(f"Media      : {plan.duration_seconds:.1f}s, {size}, {orientation}")

    route = plan.subtitles.route
    if route == "platform":
        tracks = ", ".join(f"{name} ({lang})" for name, lang in plan.subtitles.tracks.items())
        lines.append(f"Subtitles  : platform track -- {tracks}")
    elif route == "asr":
        available = [b.name for b in plan.subtitles.asr_backends if b.available]
        lines.append(f"Subtitles  : speech recognition -- {', '.join(available) or 'none available'}")
    else:
        lines.append("Subtitles  : unknown (the source could not be inspected)")

    if plan.translation.needed:
        langs = ", ".join(b.name for b in plan.translation.backends if b.available)
        lines.append(f"Translation: needed ({plan.translation.target_lang}) -- {langs or 'none available'}")
    else:
        lines.append("Translation: not needed")

    if plan.burn_runs:
        lines.append(f"Burn       : {plan.burn_mode} ({plan.renderer})")

    lines.append(f"Feasible   : {'yes' if plan.feasible else 'NO'}")

    for issue in plan.blocking_issues:
        lines.append(f"  blocked  : {issue}")
    for note in plan.notes:
        lines.append(f"  note     : {note}")

    return "\n".join(lines)
