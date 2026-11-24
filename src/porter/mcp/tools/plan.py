"""Tool: the resolved workflow plan.

Answers "what would this job do, and will it work?" before committing compute.
The engine side is :func:`porter.core.workflow.resolver.resolve_plan`; this
module is the MCP shape of it.

**Why this exists separately from ``porter_probe``.** A probe reports what a
source *is*. A plan reports what the job would *do*: which workflow was chosen,
which single capability runs each stage, what each costs, and what leaves the
machine. An agent that reads ``has_subtitles`` and predicts the pipeline will be
wrong on most links; the plan is the authoritative answer.

**Plans are executable, not advisory.** The returned JSON is exactly what
``porter_job_start(plan=...)`` accepts. An agent can show it to the user, get
consent, and run those stages and capabilities -- not a re-derived equivalent.

**It never downloads media.** It probes the source, probes the local machine's
capabilities, and returns. That holds a LIGHT slot for a few seconds, not a
HEAVY one for an hour.
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
    """Attach the planning tool to ``server``."""

    @server.tool(
        name="porter_plan",
        description=(
            "Resolve the executable plan for a source before starting it: the "
            "workflow (url.subtitled, url.asr, local.asr, subtitle.only, ...), "
            "exactly one capability per stage, target language, output directory, "
            "and blockers with fixes. The result can be passed unchanged to "
            "porter_job_start(plan=...). Check 'feasible' and 'issues': a plan "
            "that cannot finish is reported here rather than after a long "
            "download. Downloads nothing."
        ),
    )
    @protect
    def porter_plan(
        source: str,
        task: str | None = None,
        output_dir: str | None = None,
        target_lang: str | None = None,
        burn: str | None = None,
        transcript: str | None = None,
        translate: str | None = None,
        render: str | None = None,
        subtitle_file: str | None = None,
        llm_model: str | None = None,
        force: bool = False,
        audio_denoise: bool | None = None,
        allow_paid: bool = False,
        force_translate: bool = False,
    ) -> dict[str, Any]:
        """Describe the job ``source`` would produce.

        Capability arguments name exactly one peer (``whisper-local``,
        ``whisper-api``, ``bing``, ``llm``, ...). A named capability that is
        unavailable blocks the plan; it is not replaced. ``allow_paid`` is the
        only way a paid capability is chosen without being named.

        ``ok`` describes the call; ``feasible`` describes the plan. An unusable
        source is ``ok: true, feasible: false`` with a ``SOURCE_UNUSABLE`` issue.
        """
        return _plan(
            source,
            task=task,
            output_dir=output_dir,
            target_lang=target_lang,
            burn=burn,
            transcript=transcript,
            translate=translate,
            render=render,
            subtitle_file=subtitle_file,
            llm_model=llm_model,
            force=force,
            audio_denoise=audio_denoise,
            allow_paid=allow_paid,
            force_translate=force_translate,
        )


def _plan(source: str, **choices: Any) -> dict[str, Any]:
    """Resolve the plan and shape it for an agent."""
    from porter.mcp.tools.planning import plan_for

    with LIGHT:
        try:
            plan = plan_for(source, **choices)
        except (PorterError, ValueError) as exc:
            # A bad argument is the caller's problem and worth reporting as
            # data; a genuine fault is a failed call. Both are data here,
            # because a raised exception would look like the server broke.
            message = exc.message if isinstance(exc, PorterError) else str(exc)
            return {"ok": False, "error": message}

    payload = plan.to_dict()
    payload["ok"] = True
    # Convenience for the common check. ``issues`` remains the full list;
    # ``blockers`` is the subset that makes the plan infeasible.
    payload["blockers"] = [issue.model_dump(mode="json") for issue in plan.blockers]
    return payload
