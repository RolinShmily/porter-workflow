"""Tool: pre-flight link inspection.

Answers "is this link usable, and what will happen to it?" in a few seconds
without downloading anything, so an agent can decide whether to commit to a job
that may run for an hour. The engine side is
:func:`porter.platforms.inspector.inspect_url`; this module is the MCP shape of
it, and the shape has to decide three things the CLI does not.

**An unusable link is a successful call.** ``ok`` describes the *call*;
``is_valid`` describes the *link*. Reporting ``ok: false`` for a 404 would tell
an agent the tool malfunctioned, and its likely response -- retry -- is exactly
wrong for a dead link. Both fields are returned because they answer different
questions, and conflating them costs an agent its retry budget.

**No cookie parameters.** The CLI has ``--cookies`` /
``--cookies-from-browser``; over MCP those values would be written into the
conversation transcript, and into any telemetry along with it (see
``docs/MCP.md`` §5.2). Cookies configured once through the CLI are
picked up here from the resolved config, so an authenticated link still works --
it just cannot be authenticated *from* a tool call.

**A concurrency cap.** Inspection is cheap for this machine and not for the
remote one: an agent fanning out over fifty links should not open fifty sockets,
and every attempt may retry. Four at a time -- see :mod:`porter_mcp.limits`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from porter.errors import PorterError
from porter_mcp.limits import LIGHT
from porter_mcp.stdout_guard import protect

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]


def register(server: FastMCP) -> None:
    """Attach the inspection tool to ``server``."""

    @server.tool(
        name="porter_inspect",
        description=(
            "Check a video link before committing to a job: platform, title, "
            "duration, resolution, orientation, and whether the platform already "
            "has subtitles (which decides whether speech recognition runs). Takes "
            "a few seconds -- it fetches metadata, but downloads no media. An "
            "unusable link is a normal "
            "result with is_valid=false and an error_message -- not a tool error, "
            "so do not retry those."
        ),
    )
    @protect
    def porter_inspect(source: str) -> dict[str, Any]:
        """Probe ``source`` and describe what a job on it would do.

        ``source`` is a link. It is named the same as ``porter_job_start``'s
        parameter on purpose: an agent that has learned one tool should not have
        to learn a second name for the same input, and passing a local path here
        gets an explanation rather than a misleading "unsupported platform".
        """
        return _inspect(source)


def _inspect(source: str) -> dict[str, Any]:
    """Resolve config, probe, and shape the result for an agent."""
    from porter.config import resolve
    from porter.context import RunContext
    from porter.models.request import JobOptions, JobRequest
    from porter.platforms.inspector import inspect_url

    try:
        config = resolve(None)
    except PorterError as exc:
        return {
            "ok": False,
            "error": f"configuration could not be resolved: {exc.message}",
        }

    # The same source-classification rule the CLI and porter_job_start use, so
    # "is this a link?" has exactly one answer across the whole product.
    request = JobRequest.from_source(source, JobOptions(output_dir=config.output_dir))
    if request.local_video is not None:
        return {
            "ok": True,
            "is_valid": False,
            "platform": "local",
            "input_url": source,
            "error_message": (
                "porter_inspect probes links, not local files. A local file needs "
                "no pre-flight check: porter_job_start validates it directly and "
                "reports a specific error for a missing, empty, undecodable or "
                "unsupported file."
            ),
        }

    options = JobOptions(
        output_dir=config.output_dir,
        # From the config, never from a tool argument. The CLI is the only place
        # credentials are allowed to be entered.
        cookies_file=config.cookies_file,
        cookies_browser=config.cookies_browser,
    )
    ctx = RunContext(job_id="inspect", options=options, config=config)

    with LIGHT:
        try:
            result = inspect_url(source, ctx)
        except PorterError as exc:
            # Genuine faults still propagate out of inspect_url (a missing
            # dependency, a cancelled job). Those *are* tool errors.
            return {"ok": False, "error": exc.message}

    payload = result.to_dict()
    payload["ok"] = True
    # The rendered report is already built by the model, and an agent needs
    # something to say to its user. Cheap to include, and asking for a second
    # tool call just to get prose would be a step the agent may not know to take.
    payload["summary"] = result.format_summary()
    return payload
