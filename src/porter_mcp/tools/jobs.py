"""Tools: long-running work as a job.

The problem these tools exist for: an MCP tool call times out in tens of seconds
to a few minutes, and encoding a 1080p video takes tens of minutes. A blocking
``porter_run`` therefore cannot work, no matter how it is written.

So the pipeline runs on a background thread and the client polls::

    porter_job_start  -> {job_id}            returns immediately
    porter_job_status -> phase, percent      cheap, safe to call often
    porter_job_result -> artifacts           once the state is terminal
    porter_job_cancel -> {cancelled}         cooperative, not a signal
    porter_job_list   -> [summary]           everything this server knows

The store is process-global and long-lived, which is what makes polling work: a
job outlives the tool call that created it. It also publishes into the shared
registry file, so ``porter jobs`` in a terminal sees the same jobs, and a cancel
issued from there stops this server's work.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from porter.errors import JobCancelled, PorterError
from porter.events import ErrorInfo, JobState, null_sink
from porter.jobs import JobRecord, JobRegistry, JobStore
from porter_mcp.limits import HEAVY
from porter_mcp.stdout_guard import protect

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["LOG_RESOURCE_PREFIX", "register"]

LOG_RESOURCE_PREFIX = "porter://jobs/"

#: One heavy job at a time; additional jobs wait in PENDING. See
#: :mod:`porter_mcp.limits` for why queuing beats racing.
_HEAVY_JOBS = HEAVY

#: Process-global, because a job must outlive the tool call that started it.
_STORE = JobStore(registry=JobRegistry())

#: Job ids that this server owns, so `porter_job_status` knows when to fall back
#: to the shared registry (a job started by a CLI in another terminal).
_OWNED: set[str] = set()
_OWNED_LOCK = threading.Lock()


def register(server: FastMCP) -> None:
    """Attach the job tools and the log resource to ``server``."""

    @server.tool(
        name="porter_job_start",
        description=(
            "Start the localization pipeline for one video and return a job_id "
            "immediately. The work continues in the background: poll "
            "porter_job_status until the state is terminal, then call "
            "porter_job_result for the output paths. Use this instead of any "
            "blocking call — a 1080p encode takes tens of minutes."
        ),
    )
    @protect
    def porter_job_start(
        source: str,
        output_dir: str | None = None,
        burn: str | None = None,
        target_lang: str | None = None,
        translator: str | None = None,
        asr_engine: str | None = None,
        llm_model: str | None = None,
        only_phase: str | None = None,
        force: bool = False,
        audio_denoise: bool = True,
        subtitle_file: str | None = None,
    ) -> dict[str, Any]:
        """Accept a job, start it in the background, return its id.

        ``source`` is a URL or a path to a local file; the same rule the CLI uses
        applies (``JobRequest.from_source``), so ``file://`` and bare paths both
        work and a local file needs no separate tool.
        """
        from pathlib import Path

        from porter.config import resolve
        from porter.context import RunContext
        from porter.models.request import BurnMode, JobOptions, JobRequest

        try:
            options = JobOptions(
                output_dir=_output_dir(output_dir, resolve(None)),
                burn=BurnMode(burn) if burn else BurnMode.DUAL,
                target_lang=target_lang or "zh-Hans",
                translator=translator,
                asr_engine=asr_engine,
                llm_model=llm_model,
                only_phase=_phase(only_phase),
                force=force,
                audio_denoise=audio_denoise,
                subtitle_file=Path(subtitle_file) if subtitle_file else None,
            )
            request = JobRequest.from_source(source, options)
        except (PorterError, ValueError) as exc:
            # A bad request is the caller's problem and is worth reporting as
            # data: an agent can fix "unknown burn mode" and retry, where a
            # raised exception just looks like the server broke.
            return {"ok": False, "error": str(exc)}

        job = _STORE.create(request)
        with _OWNED_LOCK:
            _OWNED.add(job.job_id)

        config = resolve(None)
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
            target=_run_job, args=(job, ctx, request), name=f"porter-{job.job_id}", daemon=True
        )
        thread.start()

        return {
            "ok": True,
            "job_id": job.job_id,
            "state": job.state.value,
            "source": request.source,
            "note": (
                "Poll porter_job_status until the state is 'done', 'failed' or "
                "'cancelled', then call porter_job_result for the output paths."
            ),
        }

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

        payload = {
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
        if record.error:
            payload["error"] = record.error
        if record.is_finished:
            payload["next"] = "call porter_job_result for the output paths"
        return payload

    @server.tool(
        name="porter_job_result",
        description=(
            "The output paths of a finished job. Call it once "
            "porter_job_status reports a terminal state. While the job is still "
            "running it says so instead of blocking, so it is safe to call at any "
            "time."
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

        payload: dict[str, Any] = {
            "ok": record.state == JobState.DONE.value,
            "job_id": record.job_id,
            "state": record.state,
            "terminal": True,
            "artifacts": record.artifacts,
            "elapsed_seconds": round(record.elapsed_seconds, 1),
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
# Internals
# ----------------------------------------------------------------------


def _output_dir(explicit: str | None, config: Any) -> Any:
    from pathlib import Path

    return Path(explicit) if explicit else Path(config.output_dir)


def _phase(value: str | None) -> Any:
    """``only_phase`` as a ``Phase``, or ``None``. Raises ``ValueError`` if bogus."""
    from porter.events import Phase

    return Phase(value) if value else None


def _run_job(job: Any, ctx: Any, request: Any) -> None:
    """Run the pipeline on a background thread, then record the outcome.

    Never lets an exception escape: a thread that dies silently would leave the
    job at ``running`` forever, which is exactly the failure the registry's
    staleness detection exists to paper over. Recording it properly is better
    than relying on that.
    """
    from porter.models.request import JobResult
    from porter.pipeline import Pipeline

    result: JobResult | None = None
    try:
        with _HEAVY_JOBS:
            # Checked inside the semaphore: a job cancelled while queued should
            # not start work when its turn finally comes.
            ctx.check_cancelled()
            result = Pipeline.default(ctx).run(request, ctx)
    except JobCancelled:
        result = JobResult(job_id=job.job_id, state=JobState.CANCELLED)
    except PorterError as exc:
        result = JobResult(
            job_id=job.job_id, state=JobState.FAILED, error=ErrorInfo.from_exception(exc)
        )
    except Exception as exc:
        ctx.logger.exception("job %s crashed", job.job_id)
        result = JobResult(
            job_id=job.job_id,
            state=JobState.FAILED,
            error=ErrorInfo(
                code="internal_error",
                message=f"unexpected failure: {exc.__class__.__name__}: {exc}",
            ),
        )
    finally:
        if result is not None:
            _STORE.finish(job, result)


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
