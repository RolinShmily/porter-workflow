"""Shared planning surface for the MCP tools.

``porter_plan`` and ``porter_job_start`` must resolve a plan the same way, or an
agent that approved one plan could start a different one. This module is the
single place where MCP arguments become
:class:`~porter.core.workflow.resolver.PlanOverrides`; the CLI has its own
``porter.cli.planning`` for the same reason, and neither frontend borrows the
other's parser.

**Cookies are not parameters.** They are read from the resolved configuration,
which only the CLI writes. Accepting a cookie path here would put credentials in
the conversation transcript and in whatever telemetry the client keeps.
"""

from __future__ import annotations

from pathlib import Path

from porter.core.config import resolve
from porter.core.context import RunContext
from porter.core.errors import ConfigError, PorterError
from porter.core.models.request import BurnMode, JobOptions
from porter.core.workflow.model import Task, WorkflowPlan
from porter.core.workflow.resolver import PlanOverrides, resolve_plan

__all__ = ["plan_for"]


def _task(value: str | None) -> Task:
    if value is None:
        return Task.RELEASE
    try:
        return Task(value)
    except ValueError as exc:
        choices = ", ".join(task.value for task in Task)
        raise ValueError(f"unknown task {value!r}; choose one of: {choices}") from exc


def _burn(value: str | None) -> BurnMode:
    if value is None:
        return BurnMode.DUAL
    try:
        return BurnMode(value)
    except ValueError as exc:
        choices = ", ".join(mode.value for mode in BurnMode)
        raise ValueError(f"unknown burn mode {value!r}; choose one of: {choices}") from exc


def plan_for(
    source: str,
    *,
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
) -> WorkflowPlan:
    """Resolve the plan MCP would run for ``source``.

    Raises ``ValueError`` for an argument an agent can correct, and
    ``PorterError`` for a genuine engine fault. Both are reported as data by the
    tools; neither is allowed to reach the protocol as an exception.
    """
    try:
        config = resolve(None)
    except PorterError as exc:
        # A broken config is an environment fault, not a planning choice. Say
        # which part failed, so an agent does not go hunting through the plan.
        raise ConfigError(f"configuration could not be resolved: {exc.message}") from exc
    overrides = PlanOverrides(
        task=_task(task),
        output_dir=Path(output_dir) if output_dir else Path(config.output_dir),
        target_lang=target_lang or "zh-Hans",
        burn=_burn(burn),
        transcript=transcript,
        translate=translate,
        render=render,
        subtitle_file=Path(subtitle_file) if subtitle_file else None,
        # From the config, never from a tool argument.
        cookies_file=config.cookies_file,
        cookies_browser=config.cookies_browser,
        llm_model=llm_model,
        force=force,
        audio_denoise=config.asr.audio_denoise if audio_denoise is None else audio_denoise,
        allow_paid=allow_paid,
        force_translate=force_translate,
    )
    ctx = RunContext(
        job_id="plan",
        options=JobOptions(
            output_dir=overrides.output_dir,
            cookies_file=config.cookies_file,
            cookies_browser=config.cookies_browser,
        ),
        config=config,
    )
    return resolve_plan(source, overrides, ctx)
