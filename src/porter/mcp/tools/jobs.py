"""Tools: long-running work as a job.

The problem these tools exist for: an MCP tool call times out in tens of seconds
to a few minutes, and encoding a 1080p video takes tens of minutes. A blocking
``porter_run`` therefore cannot work, no matter how it is written.

So the workflow runs on a background thread and the client polls::

    porter_job_start  -> {job_id}            returns immediately
    porter_job_status -> phase, percent      cheap, safe to call often
    porter_job_result -> WorkflowResult      once the state is terminal
    porter_job_cancel -> {cancelled}         cooperative, not a signal
    porter_job_list   -> [summary]           everything this server knows

**Start takes a plan.** An agent should call ``porter_plan``, show the result,
and pass it back unchanged as ``plan=...``. The executor then runs exactly those
stages and capabilities. ``source`` plus planning arguments is accepted as a
convenience and resolves the same plan first; an infeasible plan is refused
before any work starts, rather than failing after a download.

The store is process-global and long-lived, which is what makes polling work: a
job outlives the tool call that created it. It also publishes into the shared
registry file, so ``porter jobs`` in a terminal sees the same jobs, and a cancel
issued from there stops this server's work.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from porter.core.errors import JobCancelled, PorterError
from porter.core.events import ErrorInfo, JobState, null_sink
from porter.core.jobs import JobRecord, JobRegistry, JobStore
from porter.mcp.limits import HEAVY
from porter.mcp.stdout_guard import protect

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from porter.core.workflow.model import StageError, WorkflowPlan, WorkflowResult

__all__ = ["LOG_RESOURCE_PREFIX", "job_store", "register"]

LOG_RESOURCE_PREFIX = "porter://jobs/"

#: One heavy job at a time; additional jobs wait in PENDING. See
#: :mod:`porter.mcp.limits` for why queuing beats racing.
_HEAVY_JOBS = HEAVY

#: Process-global, because a job must outlive the tool call that started it.
_STORE = JobStore(registry=JobRegistry())

#: Job ids that this server owns, so `porter_job_status` knows when to fall back
#: to the shared registry (a job started by a CLI in another terminal).
_OWNED: set[str] = set()
_OWNED_LOCK = threading.Lock()

#: The stage-level result of jobs this process executed. The registry record is
#: flat by design (it crosses a process boundary); this holds the full
#: ``WorkflowResult`` for jobs whose executor ran here.
_WORKFLOWS: dict[str, WorkflowResult] = {}
_WORKFLOWS_LOCK = threading.Lock()
_MAX_REMEMBERED_WORKFLOWS = 128


def job_store() -> JobStore:
    """The process-global job store.

    Public so the shutdown handler can reach it: stopping jobs on a signal is the
    same operation as ``porter_job_cancel``, and it needs the same store. See
    :mod:`porter.mcp.shutdown`.
    """
    return _STORE


def register(server: FastMCP) -> None:
    """Attach the job tools and the log resource to ``server``."""

    @server.tool(
        name="porter_job_start",
        description=(
            "Execute a resolved plan in the background and return a job_id "
            "immediately. Pass the plan from porter_plan unchanged as "
            "plan={...}; alternatively pass source plus planning arguments and "
            "the same plan is resolved first. An infeasible plan is refused "
            "before any work starts. Poll porter_job_status until terminal, then "
            "call porter_job_result for the stage results and artifacts."
        ),
    )
    @protect
    def porter_job_start(
        source: str | None = None,
        plan: dict[str, Any] | None = None,
        output_dir: str | None = None,
        task: str | None = None,
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
        """Accept a plan (or resolve one), start it, and return its id.

        ``source`` and the planning arguments are ignored when ``plan`` is
        given -- except that passing both is an error rather than a silent
        precedence rule. Use ``porter_plan`` to build a plan, and pass it back
        unchanged.
        """
        return _start(
            source=source,
            plan=plan,
            output_dir=output_dir,
            task=task,
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

    @server.tool(
        name="porter_job_status",
        description=(
            "Report a job's state, current phase, progress percent, elapsed time "
            "and the last progress message. Cheap and safe to call repeatedly. "
            "Also reports jobs started by another porter process (for example "
            "`porter run` in a terminal), which are read from the shared registry."
        ),
    )
    @protect
    def porter_job_status(job_id: str) -> dict[str, Any]:
        """Where is this job now."""
        record = _lookup(job_id)
        if record is None:
            return {"ok": False, "error": f"unknown job id: {job_id}"}

        payload: dict[str, Any] = {
            "ok": True,
            "job_id": record.job_id,
            "state": record.state,
            "terminal": record.is_finished,
            "phase": record.phase,
            "percent": record.percent,
            "message": record.message,
            "elapsed_seconds": round(record.elapsed_seconds, 1),
            "source": record.source,
            "cancel_requested": record.cancel_requested,
        }
        workflow = _workflow_for(job_id)
        if workflow is not None:
            payload["plan_id"] = workflow.plan_id
            payload["workflow"] = workflow.workflow.value
            payload["task"] = workflow.task.value
        if record.error:
            payload["error"] = record.error
        if record.is_finished:
            payload["next"] = "call porter_job_result for the stage results and artifacts"
        return payload

    @server.tool(
        name="porter_job_result",
        description=(
            "The stage results and output paths of a finished job. Call it once "
            "porter_job_status reports a terminal state. Owned jobs return the "
            "full WorkflowResult (per-stage capability, status, outputs, and a "
            "structured error with alternatives if it failed). Jobs started by "
            "another process return the flat registry record with a note. While "
            "the job is still running it says so instead of blocking."
        ),
    )
    @protect
    def porter_job_result(job_id: str) -> dict[str, Any]:
        """What did this job produce."""
        record = _lookup(job_id)
        if record is None:
            return {"ok": False, "error": f"unknown job id: {job_id}"}

        if not record.is_finished:
            return {
                "ok": False,
                "job_id": job_id,
                "state": record.state,
                "terminal": False,
                "error": "the job is still running; poll porter_job_status",
            }

        workflow = _workflow_for(job_id)
        if workflow is not None:
            payload = workflow.to_dict()
            payload["ok"] = workflow.state is JobState.DONE
            payload["terminal"] = True
            return payload

        # A job owned by another process: it wrote its flat record, not a
        # stage-level result. Saying so is honest; inventing stages would not be.
        payload = {
            "ok": record.state == JobState.DONE.value,
            "job_id": record.job_id,
            "state": record.state,
            "terminal": True,
            "artifacts": record.artifacts,
            "elapsed_seconds": round(record.elapsed_seconds, 1),
            "detail": (
                "this job was started by another porter process; only its flat "
                "registry record is available here, not per-stage results"
            ),
        }
        if record.task_dir:
            payload["task_dir"] = record.task_dir
        if record.error:
            payload["error"] = record.error
        return payload

    @server.tool(
        name="porter_job_cancel",
        description=(
            "Ask a running job to stop. The request is cooperative: the job stops "
            "at its next checkpoint, so the state may read 'running' briefly "
            "afterwards. Poll porter_job_status until it reads 'cancelled'."
        ),
    )
    @protect
    def porter_job_cancel(job_id: str) -> dict[str, Any]:
        """Request cancellation, from this process or another one."""
        with _OWNED_LOCK:
            owned = job_id in _OWNED

        if owned:
            job = _STORE.get(job_id)
            if job is None:
                return {"ok": False, "error": f"unknown job id: {job_id}"}
            if job.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
                return {
                    "ok": False,
                    "job_id": job_id,
                    "state": job.state.value,
                    "error": f"job already finished: {job.state.value}",
                }
            job.cancel.set()
            return {"ok": True, "job_id": job_id, "cancelled": True}

        # Not ours: ask through the shared file, which the owner observes.
        if _STORE.registry is None:  # pragma: no cover - the store always has one
            return {"ok": False, "error": "no job registry available"}
        requested = _STORE.registry.request_cancel(job_id)
        if not requested:
            return {
                "ok": False,
                "job_id": job_id,
                "error": "unknown job, or it has already finished",
            }
        return {"ok": True, "job_id": job_id, "cancelled": True}

    @server.tool(
        name="porter_job_list",
        description=(
            "Every job this server and any other porter process knows about, "
            "newest first. Use it to recover a job_id after losing track of one."
        ),
    )
    @protect
    def porter_job_list() -> dict[str, Any]:
        """The job history, from both layers."""
        return {"ok": True, "jobs": [_summary(record) for record in _all_records()]}

    @server.resource(f"{LOG_RESOURCE_PREFIX}{{job_id}}/log")
    @protect
    def job_log(job_id: str) -> str:
        """A job's recent events, as text.

        A resource rather than a tool because it is reference material: a client
        fetches it when someone asks what a job is actually doing, not on every
        poll. Only jobs owned by this process have an event buffer; others get
        the record's summary, which is all the shared file holds.
        """
        return _render_log(job_id)


# ----------------------------------------------------------------------
# Starting
# ----------------------------------------------------------------------


def _start(
    *,
    source: str | None,
    plan: dict[str, Any] | None,
    output_dir: str | None,
    task: str | None,
    target_lang: str | None,
    burn: str | None,
    transcript: str | None,
    translate: str | None,
    render: str | None,
    subtitle_file: str | None,
    llm_model: str | None,
    force: bool,
    audio_denoise: bool | None,
    allow_paid: bool,
    force_translate: bool,
) -> dict[str, Any]:
    """Validate the request, resolve if needed, and launch the job."""
    from pathlib import Path

    from porter.core.config import resolve
    from porter.core.context import RunContext
    from porter.core.models.request import JobOptions, JobRequest
    from porter.core.workflow.model import WorkflowPlan
    from porter.mcp.tools.planning import plan_for

    try:
        config = resolve(None)
    except PorterError as exc:
        return {
            "ok": False,
            "error": f"configuration could not be resolved: {exc.message}",
        }

    if plan is not None:
        # Mixing a plan with planning arguments has no defensible meaning: the
        # plan already fixed them. Refuse instead of silently preferring one.
        conflicting = [
            name
            for name, value in (
                ("source", source),
                ("output_dir", output_dir),
                ("task", task),
                ("target_lang", target_lang),
                ("burn", burn),
                ("transcript", transcript),
                ("translate", translate),
                ("render", render),
                ("subtitle_file", subtitle_file),
                ("llm_model", llm_model),
                ("force", force),
                ("audio_denoise", audio_denoise),
                ("allow_paid", allow_paid),
                ("force_translate", force_translate),
            )
            if value
        ]
        if conflicting:
            return {
                "ok": False,
                "error": (
                    "plan fixes every planning choice; do not also pass "
                    + ", ".join(conflicting)
                ),
            }
        try:
            resolved = WorkflowPlan.model_validate(plan)
        except ValueError as exc:
            return {"ok": False, "error": f"invalid plan: {exc}"}
        if not resolved.plan_id:
            resolved = resolved.sealed()
    else:
        if not source:
            return {"ok": False, "error": "pass source or plan"}
        try:
            resolved = plan_for(
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
        except (PorterError, ValueError) as exc:
            message = exc.message if isinstance(exc, PorterError) else str(exc)
            return {"ok": False, "error": message}

    if not resolved.feasible:
        return {
            "ok": False,
            "error": "the plan is not feasible; resolve its blockers and plan again",
            "plan": resolved.to_dict(),
        }

    options = JobOptions(output_dir=Path(resolved.runtime.output_dir), force=resolved.runtime.force)
    try:
        request = JobRequest.from_source(resolved.source, options)
    except (PorterError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    job = _STORE.create(request)
    with _OWNED_LOCK:
        _OWNED.add(job.job_id)

    ctx = RunContext(
        job_id=job.job_id,
        options=options,
        config=config,
        # Jobs do not stream progress notifications: the client polls, and MCP
        # progress tokens only exist for the duration of one call. Events still
        # need a sink so the store can record them; it installs its own in
        # attach() on top of this one.
        events=null_sink,
    )
    # Before the thread starts, so a cancel arriving in the first
    # milliseconds is not lost.
    _STORE.attach(job, ctx)

    thread = threading.Thread(
        target=_run_job,
        args=(job, ctx, resolved),
        name=f"porter-{job.job_id}",
        daemon=True,
    )
    thread.start()

    return {
        "ok": True,
        "job_id": job.job_id,
        "state": job.state.value,
        "source": request.source,
        "plan_id": resolved.plan_id,
        "workflow": resolved.workflow.value,
        "task": resolved.task.value,
        "stages": [
            {"stage": stage.stage.value, "capability": stage.capability}
            for stage in resolved.stages
        ],
        "plan": resolved.to_dict(),
        "note": (
            "Poll porter_job_status until the state is 'done', 'failed' or "
            "'cancelled', then call porter_job_result for the stage results and "
            "output paths."
        ),
    }


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


def _run_job(job: Any, ctx: Any, plan: WorkflowPlan) -> None:
    """Run the plan on a background thread, then record both result shapes.

    Never lets an exception escape: a thread that dies silently would leave the
    job at ``running`` forever, which is exactly the failure the registry's
    staleness detection exists to paper over. Recording it properly is better
    than relying on that.
    """
    from porter.core.models.request import JobResult
    from porter.core.workflow.executor import execute_plan

    result: JobResult | None = None
    workflow: WorkflowResult | None = None
    try:
        with _HEAVY_JOBS:
            # Checked inside the semaphore: a job cancelled while queued should
            # not start work when its turn finally comes.
            ctx.check_cancelled()
            outcome = execute_plan(plan, ctx)
        result = outcome.job
        workflow = outcome.workflow
    except JobCancelled:
        result = JobResult(job_id=job.job_id, state=JobState.CANCELLED)
        workflow = _failure_workflow(plan, ctx, JobState.CANCELLED)
    except PorterError as exc:
        info = ErrorInfo.from_exception(exc)
        result = JobResult(job_id=job.job_id, state=JobState.FAILED, error=info)
        workflow = _failure_workflow(
            plan,
            ctx,
            JobState.FAILED,
            _stage_error(info.code, info.message, info.details),
        )
    except Exception as exc:
        ctx.logger.exception("job %s crashed", job.job_id)
        info = ErrorInfo(
            code="internal_error",
            message=f"unexpected failure: {exc.__class__.__name__}: {exc}",
        )
        result = JobResult(job_id=job.job_id, state=JobState.FAILED, error=info)
        workflow = _failure_workflow(
            plan,
            ctx,
            JobState.FAILED,
            _stage_error(info.code, info.message, info.details),
        )
    finally:
        # Before finish(): job_result becomes callable the moment the store
        # reports a terminal state, so the workflow must already be visible.
        if workflow is not None:
            _remember(job.job_id, workflow)
        if result is not None:
            _STORE.finish(job, result)


def _stage_error(code: str, message: str, details: dict[str, Any]) -> StageError:
    from porter.core.workflow.model import StageError

    return StageError(code=code, message=message, details=dict(details))


def _failure_workflow(
    plan: WorkflowPlan,
    ctx: Any,
    state: JobState,
    error: StageError | None = None,
) -> WorkflowResult:
    from porter.core.workflow.model import WorkflowResult

    return WorkflowResult(
        job_id=ctx.job_id,
        state=state,
        plan_id=plan.plan_id,
        workflow=plan.workflow,
        task=plan.task,
        error=error,
    )


def _remember(job_id: str, workflow: WorkflowResult) -> None:
    with _WORKFLOWS_LOCK:
        _WORKFLOWS[job_id] = workflow
        while len(_WORKFLOWS) > _MAX_REMEMBERED_WORKFLOWS:
            oldest = next(iter(_WORKFLOWS))
            del _WORKFLOWS[oldest]


def _workflow_for(job_id: str) -> WorkflowResult | None:
    with _WORKFLOWS_LOCK:
        return _WORKFLOWS.get(job_id)


# ----------------------------------------------------------------------
# Lookup and rendering
# ----------------------------------------------------------------------


def _lookup(job_id: str) -> JobRecord | None:
    """Find a job in this process first, then in the shared registry.

    The fallback is what makes the MCP tools and the CLI agree: a job started by
    ``porter run`` in a terminal is not in this process's store, but it is in the
    file, and reporting "unknown job id" for it would be wrong.
    """
    with _OWNED_LOCK:
        owned = job_id in _OWNED

    if owned:
        job = _STORE.get(job_id)
        if job is not None:
            return job.to_record()

    registry = _STORE.registry
    return registry.get(job_id, reap=True) if registry is not None else None


def _all_records() -> list[JobRecord]:
    """Owned jobs and registry jobs merged, newest first, without duplicates."""
    records: dict[str, JobRecord] = {}
    registry = _STORE.registry
    if registry is not None:
        for record in registry.read(reap=True):
            records[record.job_id] = record
    # The in-memory view wins: it is fresher than the throttled projection.
    for job in _STORE.list():
        records[job.job_id] = job.to_record()
    return sorted(records.values(), key=lambda r: r.created_at, reverse=True)


def _summary(record: JobRecord) -> dict[str, Any]:
    return {
        "job_id": record.job_id,
        "state": record.state,
        "source": record.source,
        "phase": record.phase,
        "percent": record.percent,
        "elapsed_seconds": round(record.elapsed_seconds, 1),
    }


def _render_log(job_id: str) -> str:
    """The recent event stream for a job this process owns."""
    with _OWNED_LOCK:
        owned = job_id in _OWNED

    job = _STORE.get(job_id) if owned else None
    if job is None:
        record = _lookup(job_id)
        if record is None:
            return f"# job {job_id}\n\nunknown job id\n"
        return (
            f"# job {record.job_id}\n\n"
            f"- state: {record.state}\n"
            f"- source: {record.source}\n"
            f"- phase: {record.phase or '-'} {record.percent:.0f}%\n"
            f"- message: {record.message or '-'}\n"
            f"- elapsed: {record.elapsed_seconds:.1f}s\n\n"
            "Event history is only available for jobs started by this server.\n"
        )

    lines = [
        f"# job {job.job_id}",
        "",
        f"- state: {job.state.value}",
        f"- source: {job.request.source}",
        f"- elapsed: {job.elapsed_seconds:.1f}s",
        "",
        "## recent events",
        "",
    ]
    for event in job.events:
        lines.append(f"- `{event.__class__.__name__}` {event.model_dump_json()}")
    if not job.events:
        lines.append("- (none yet)")
    return "\n".join(lines) + "\n"
