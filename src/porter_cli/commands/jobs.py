"""``porter jobs`` — inspect and cancel long-running jobs.

Job state lives in a shared file (see :mod:`porter.jobs.records`), so this
command sees work started by *any* porter process: an MCP server running in
another terminal, an earlier ``porter run``, or a session that was interrupted.

That is the whole point. A long encode takes tens of minutes; the terminal that
started it is often gone by the time anyone asks "did that finish, and where did
it put the files".
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from porter.events import JobState
from porter_cli import render

if TYPE_CHECKING:
    # Annotations only; `from __future__ import annotations` means these are never
    # evaluated, so the CLI does not pay for pydantic import before argparse runs.
    from porter.jobs import JobRecord, JobRegistry

__all__ = ["configure", "run"]


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``jobs`` command."""
    parser = subparsers.add_parser(
        "jobs",
        help="List, inspect, or cancel pipeline jobs.",
        description=(
            "Manage the job registry shared by every porter process. Jobs are "
            "recorded in a file, so a job started by the MCP server in another "
            "terminal is visible here."
        ),
    )
    actions = parser.add_subparsers(dest="jobs_action", metavar="<action>")

    listing = actions.add_parser("list", help="List tracked jobs, newest first.")
    listing.add_argument(
        "--all",
        dest="show_all",
        action="store_true",
        help="Include finished jobs as well as running ones.",
    )
    listing.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Write the job records as JSON to stdout.",
    )

    status = actions.add_parser("status", help="Show phase, progress, and elapsed time.")
    status.add_argument("job_id", help="Job identifier from 'jobs list'.")
    status.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Write the job record as JSON to stdout.",
    )

    cancel = actions.add_parser("cancel", help="Ask a running job to stop.")
    cancel.add_argument("job_id", help="Job identifier to cancel.")

    actions.add_parser("clear", help="Drop finished jobs from the registry.")

    # Bare `porter jobs` means `porter jobs list`. The defaults have to live on the
    # parent parser, because with no action the `list` subparser never runs and
    # never sets them -- reading `args.show_all` then raised AttributeError.
    parser.set_defaults(
        handler=run,
        jobs_action="list",
        show_all=False,
        as_json=False,
        job_id=None,
    )


def run(args: argparse.Namespace) -> int:
    """Execute the selected ``jobs`` action."""
    from porter.jobs import JobRegistry

    registry = JobRegistry()
    action = args.jobs_action or "list"

    if action == "list":
        return _list(registry, show_all=args.show_all, as_json=args.as_json)
    if action == "status":
        return _status(registry, args.job_id, as_json=args.as_json)
    if action == "cancel":
        return _cancel(registry, args.job_id)
    if action == "clear":
        return _clear(registry)

    render.warn(f"unknown jobs action: {action}")
    return render.EXIT_MISUSE


def _list(registry: JobRegistry, *, show_all: bool, as_json: bool) -> int:
    """Show the registry, newest first.

    ``reap=True`` writes back any job whose owning process has died. Without it a
    killed job stays ``running`` in the listing forever, which is exactly the
    question this command exists to answer.
    """
    records = registry.read(reap=True)
    if not show_all:
        records = [record for record in records if not record.is_finished]

    if as_json:
        render.emit_json([record.to_json() for record in records])
        return render.EXIT_OK

    if not records:
        # "No jobs" and "no *running* jobs" are different answers, and the second
        # one is not a problem, so say which it is.
        render.info("no running jobs" if not show_all else "no jobs recorded")
        render.info("pass --all to include finished jobs" if not show_all else "")
        return render.EXIT_OK

    for record in records:
        render.value(_summary_line(record))
    return render.EXIT_OK


def _summary_line(record: JobRecord) -> str:
    """One aligned line: state, id, progress, elapsed, source."""
    state = record.state_enum
    glyph = render.render_job_state(state)
    progress = ""
    if state is JobState.RUNNING:
        phase = record.phase or "starting"
        progress = f" {phase} {record.percent:3.0f}%"
    elif state is JobState.PENDING:
        progress = " waiting"

    return (
        f"{glyph} {record.job_id}  {state.value:<9}{progress}"
        f"  {_duration(record.elapsed_seconds):>7}  {record.source}"
    )


def _status(registry: JobRegistry, job_id: str, *, as_json: bool) -> int:
    """Show one job in full, including where its artifacts are."""
    record = registry.get(job_id, reap=True)
    if record is None:
        render.warn(f"unknown job id: {job_id}")
        return render.EXIT_ERROR

    if as_json:
        render.emit_json(record.to_json())
        return render.EXIT_OK

    render.info(f"{render.render_job_state(record.state_enum)} {record.state}")
    render.info(f"  source:   {record.source}")
    render.info(f"  elapsed:  {_duration(record.elapsed_seconds)}")
    if record.phase is not None:
        render.info(f"  phase:    {record.phase} {record.percent:.0f}%")
    if record.message:
        render.info(f"  last:     {record.message}")
    if record.error:
        render.warn(record.error)
    if record.cancel_requested and not record.is_finished:
        render.info("  cancelling: requested, waiting for the owner to stop")

    # Artifacts are the answer to "where did my files go", so they go to stdout
    # where they can be captured; everything above is commentary.
    for path in record.artifacts:
        render.value(path)
    return render.EXIT_OK


def _cancel(registry: JobRegistry, job_id: str) -> int:
    """Ask the owner to stop.

    Nothing is signalled: the request is recorded, and the owning process notices
    it through the event sink it already has. That keeps a cancel from a terminal
    identical to one from an MCP client, and avoids sending signals to a process
    that may since have been replaced.
    """
    record = registry.get(job_id, reap=True)
    if record is None:
        render.warn(f"unknown job id: {job_id}")
        return render.EXIT_ERROR
    if record.is_finished:
        render.warn(f"job {job_id} already finished: {record.state}")
        return render.EXIT_ERROR

    if not registry.request_cancel(job_id):
        render.warn(f"job {job_id} could not be cancelled")
        return render.EXIT_ERROR

    render.info(f"cancellation requested for {job_id}")
    render.info("the owning process stops at the next cancellation check")
    return render.EXIT_OK


def _clear(registry: JobRegistry) -> int:
    """Drop finished records, leaving running jobs alone."""
    removed = registry.clear()
    render.info(f"removed {removed} finished job{'s' if removed != 1 else ''}")
    return render.EXIT_OK


def _duration(seconds: float) -> str:
    """``1h02m`` / ``3m07s`` / ``12s`` — short enough to sit in a column."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"
