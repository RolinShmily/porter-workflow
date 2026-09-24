"""Tool: the resolved execution plan.

Answers "what would this job do, and will it work?" before committing compute.
The engine side is :func:`porter.plan.plan_for`; this module is the MCP shape of
it.

**Why this exists separately from ``porter_inspect``.** Inspection reports what a
link *is*: platform, duration, resolution, whether captions exist. It does not
report what the job would *do*, and those are different questions. Three of the
five platforms fetch no subtitle track at all however many they advertise, the
source language is chosen by a per-platform priority list, and a platform-provided
Chinese track removes the translation phase entirely. An agent that reads
``has_subtitles`` and predicts from it will be wrong on most links.

**Why it performs its own inspection.** It could take the facts as arguments, but
then it could be handed stale or invented ones, and a plan built on a guess is
worse than no plan -- an agent acts on it. So it probes the link itself and
returns the inspection facts alongside, which also means an agent can call this
instead of ``porter_inspect`` and get both answers in one round trip. The cost is
that it takes as long as an inspection (a few seconds), and it holds a LIGHT slot
for the whole of it.
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
    """Attach the planning tool to ``server``."""

    @server.tool(
        name="porter_plan",
        description=(
            "Resolve what a job would actually do before starting it: which phases "
            "run, whether subtitles come from the platform's own track or from "
            "speech recognition (and which engines are available in order), whether "
            "translation is needed at all, and what is known to be broken. Takes a "
            "few seconds and downloads nothing. Check 'feasible' and "
            "'blocking_issues': a job that cannot finish is reported here rather "
            "than after thirty minutes of encoding."
        ),
    )
    @protect
    def porter_plan(source: str) -> dict[str, Any]:
        """Describe the job ``source`` would produce.

        ``source`` is a link or a path to a local file, classified by the same rule
        the CLI and ``porter_job_start`` use. Options are not accepted: the plan
        describes the default run, and an agent that wants different flags can pass
        them to ``porter_job_start`` and read the plan's phase list to know what it
        is choosing between.
        """
        return _plan(source)


def _plan(source: str) -> dict[str, Any]:
    """Resolve config, build the plan, and shape the result for an agent."""
    from porter.config import resolve
    from porter.context import RunContext
    from porter.models.request import JobOptions
    from porter.plan import plan_for

    try:
        config = resolve(None)
    except PorterError as exc:
        return {
            "ok": False,
            "error": f"configuration could not be resolved: {exc.message}",
        }

    # Hand the resolved config over instead of letting ``plan_for`` resolve its
    # own: otherwise ``payload["output_dir"]`` below and the plan's view of the
    # config are two independent reads of the same files, and the comment further
    # down would be a claim rather than a fact.
    ctx = RunContext(
        job_id="plan",
        options=JobOptions(output_dir=config.output_dir),
        config=config,
    )

    with LIGHT:
        try:
            plan = plan_for(source, ctx=ctx)
        except PorterError as exc:
            # A genuine fault -- a missing dependency, a cancelled job. An
            # unusable *source* does not come through here; plan_for reports it as
            # a plan with feasible=false.
            return {"ok": False, "error": exc.message}

    payload = plan.to_dict()
    payload["ok"] = True
    # Resolved from the same config the run would use, so it is not a guess. An
    # agent needs to know where the artifacts will land to answer "where did it
    # go" without a second tool call.
    payload["output_dir"] = str(config.output_dir)
    return payload
