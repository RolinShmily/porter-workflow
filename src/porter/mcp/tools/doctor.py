"""Tool: the structured capability report.

This is the payoff of splitting facts from prose: v0.1's ``CheckResult`` carried
a boolean *and* a multi-paragraph Chinese
installation guide, which is fine in a terminal and useless over MCP — an agent
cannot act on a wall of text, and a protocol has no terminal to render it in.

So the tool returns two things:

``report``
    :meth:`~porter.core.doctor.probes.CapabilityReport.to_dict` — pure facts, one
    entry per capability, each with a ``severity`` and a ``remediation_key``.
``guidance``
    The prose for the keys that actually failed, keyed by that same
    ``remediation_key``.

Returning guidance inline rather than as a separate resource is deliberate: an
agent that finds a blocker needs the fix in the same step, and a second round
trip to fetch it is a step the agent may not know to take. The full guide table
is *also* available as the ``porter://doctor/guides`` resource for a client that
wants to render documentation up front.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from porter.core.config import resolve
from porter.core.doctor import GUIDES, probe_all, report_payload
from porter.core.errors import PorterError
from porter.mcp.stdout_guard import protect

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]

GUIDES_RESOURCE_URI = "porter://doctor/guides"


def register(server: FastMCP) -> None:
    """Attach the doctor tool and the guide resource to ``server``."""

    @server.tool(
        name="porter_doctor",
        description=(
            "Check what this machine can actually do before starting a job: "
            "ffmpeg and libass, a JavaScript runtime for yt-dlp, the Chinese "
            "subtitle font, and which video encoder will be used. Returns "
            "structured findings plus step-by-step remediation for anything "
            "that failed. Call this before starting a job on an unfamiliar host."
        ),
    )
    @protect
    def porter_doctor() -> dict[str, Any]:
        """Return the capability report and remediation for its failures."""
        try:
            config = resolve(None)
        except PorterError as exc:
            return {
                "ok": False,
                "error": f"configuration could not be resolved: {exc.message}",
            }

        # The same payload the CLI's --json mode emits, so the two frontends
        # cannot disagree about what a blocker means or how to fix it.
        return report_payload(probe_all(config))

    @server.resource(GUIDES_RESOURCE_URI)
    @protect
    def doctor_guides() -> str:
        """The full remediation guide table, as Markdown.

        Exposed as a resource because it is reference material, not a result:
        a client renders it when the operator asks "what does this check mean",
        not on every call.
        """
        return _render_guides()


def _render_guides() -> str:
    """Render every guide as one Markdown document."""
    lines = ["# porter capability guides", ""]
    for key in sorted(GUIDES):
        guide = GUIDES[key]
        lines.append(f"## `{key}`")
        lines.append("")
        lines.append(guide.summary)
        if guide.steps:
            lines.append("")
            for step in guide.steps:
                lines.append(f"- `{step}`")
        if guide.note:
            lines.append("")
            lines.append(guide.note)
        if guide.url:
            lines.append("")
            lines.append(guide.url)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
