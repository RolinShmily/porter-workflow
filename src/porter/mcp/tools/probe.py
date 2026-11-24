"""Tool: source facts without a plan.

Answers "what is this input?" before any strategy exists: a link, a local video,
an audio file, or a subtitle. It is the MCP shape of
:func:`porter.core.workflow.probe.probe_source`, the same probe the planner
uses, so the facts an agent reads here cannot disagree with the facts the plan
was built on.

Three things the shape decides that the engine does not:

**An unusable source is a successful call.** ``ok`` describes the call;
``warnings`` describes the source. A 404 or a missing file comes back as
``ok: true`` with a ``SOURCE_UNUSABLE`` / ``SOURCE_NOT_FOUND`` warning, because
reporting it as a tool failure invites a retry that cannot help.

**Local files are welcome.** Unlike the old link-only inspection, a path is a
first-class source. It is probed with ffprobe, and the measured ``has_video`` /
``has_audio`` are facts, not guesses.

**No cookie parameters.** The CLI has ``--cookies`` / ``--cookies-from-browser``;
over MCP those values would be written into the conversation transcript. Cookies
configured once through the CLI are picked up from the resolved config, so an
authenticated link still works -- it just cannot be authenticated *from* a tool
call.

**A concurrency cap.** Probing is cheap for this machine and not for the remote
one: an agent fanning out over fifty links should not open fifty sockets. See
:mod:`porter.mcp.limits`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from porter.core.errors import PorterError
from porter.mcp.limits import LIGHT
from porter.mcp.stdout_guard import protect

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]


def register(server: FastMCP) -> None:
    """Attach the probe tool to ``server``."""

    @server.tool(
        name="porter_probe",
        description=(
            "Describe a source without planning or downloading it: kind "
            "(url/local_video/audio/subtitle), platform, title, duration, "
            "resolution, measured audio/video streams, and the subtitle tracks "
            "the platform lists (listed, not fetched). An unusable source is a "
            "normal result with a warning, not a tool error -- do not retry "
            "those. For a URL this fetches metadata but downloads no media."
        ),
    )
    @protect
    def porter_probe(source: str) -> dict[str, Any]:
        """Probe ``source`` and return the facts the planner would use.

        ``source`` is a link, a local media path, a ``file://`` path, or a
        subtitle file. It is named the same as ``porter_job_start``'s parameter
        on purpose: an agent that has learned one tool should not have to learn
        a second name for the same input.
        """
        return _probe(source)


def _probe(source: str) -> dict[str, Any]:
    """Resolve config, probe, and shape the result for an agent."""
    from porter.core.config import resolve
    from porter.core.context import RunContext
    from porter.core.models.request import JobOptions
    from porter.core.workflow.probe import probe_source

    try:
        config = resolve(None)
    except PorterError as exc:
        return {
            "ok": False,
            "error": f"configuration could not be resolved: {exc.message}",
        }

    options = JobOptions(
        output_dir=config.output_dir,
        # Cookies come from configuration only. The CLI is the only place
        # credentials are entered, and this tool has no parameter for them.
        cookies_file=config.cookies_file,
        cookies_browser=config.cookies_browser,
    )
    ctx = RunContext(job_id="probe", options=options, config=config)

    with LIGHT:
        try:
            probe = probe_source(source, ctx)
        except PorterError as exc:
            # Genuine faults still propagate out of probe_source (a missing
            # dependency, a cancelled job). Those *are* tool errors.
            return {"ok": False, "error": exc.message}

    payload = probe.model_dump(mode="json")
    payload["ok"] = True
    payload["blockers"] = [
        issue["code"]
        for issue in payload["warnings"]
        if issue["severity"] == "blocker"
    ]
    return payload
