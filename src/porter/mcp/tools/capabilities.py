"""Tool: the peer-level capability catalog.

``porter_doctor`` answers "is this machine healthy"; this tool answers "which
implementations can this machine run, stage by stage". They are different
questions, and an agent choosing a transcript engine needs the second one.

Every capability is listed, available or not. Hiding unavailable entries would
turn the catalog into a menu of things that work and erase the distinction
between "not installed" and "not supported" -- exactly the distinction an agent
needs to decide whether to install something or replan.

Cost and egress travel with each entry. ``cost=paid`` is never selected by the
resolver unless the capability is named explicitly or the plan was allowed to
spend; ``egress=audio`` means audio leaves the machine, which the user has to
agree to separately from the price.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from porter.core.errors import PorterError
from porter.mcp.stdout_guard import protect

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]


def register(server: FastMCP) -> None:
    """Attach the capability catalog tool to ``server``."""

    @server.tool(
        name="porter_capabilities",
        description=(
            "List stage capabilities with availability, cost and data egress. "
            "One capability per stage is all a plan can use; use this to choose "
            "between peers (for example local Whisper vs the paid Whisper API) "
            "or to explain why a plan is blocked. Unavailable capabilities are "
            "still listed. Nothing is downloaded or executed."
        ),
    )
    @protect
    def porter_capabilities(
        stage: str | None = None,
        available_only: bool = False,
    ) -> dict[str, Any]:
        """Describe every capability, optionally narrowed to one stage.

        ``stage`` is one of probe/acquire/normalize/transcript/translate/write/
        render. An unknown stage is reported as data rather than silently
        returning nothing.
        """
        return _catalog(stage, available_only)


def _catalog(stage: str | None, available_only: bool) -> dict[str, Any]:
    from porter.core.config import resolve
    from porter.core.context import RunContext
    from porter.core.models.request import JobOptions
    from porter.core.workflow.capabilities import capability_catalog
    from porter.core.workflow.model import Stage

    try:
        config = resolve(None)
    except PorterError as exc:
        return {
            "ok": False,
            "error": f"configuration could not be resolved: {exc.message}",
        }

    wanted: Stage | None = None
    if stage is not None:
        try:
            wanted = Stage(stage)
        except ValueError:
            choices = ", ".join(item.value for item in Stage)
            return {
                "ok": False,
                "error": f"unknown stage {stage!r}; choose one of: {choices}",
            }

    ctx = RunContext(
        job_id="capabilities",
        options=JobOptions(output_dir=config.output_dir),
        config=config,
    )
    catalog = capability_catalog(ctx)
    if wanted is not None:
        catalog = tuple(item for item in catalog if item.stage is wanted)
    if available_only:
        catalog = tuple(item for item in catalog if item.available)

    return {
        "ok": True,
        "stage": wanted.value if wanted is not None else None,
        "available_only": available_only,
        "count": len(catalog),
        "capabilities": [item.model_dump(mode="json") for item in catalog],
    }
