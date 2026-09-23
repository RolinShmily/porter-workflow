"""Job registry shared by the CLI and the MCP frontend.

Why this exists: the MCP frontend cannot run a full pipeline inside one tool
call. Encoding a 1080p video takes tens of minutes, and MCP clients time out
long before that. So long work is modelled as a *job*:

    porter_job_start(...)   -> {job_id}          returns immediately
    porter_job_status(id)   -> phase, percent    cheap polling
    porter_job_result(id)   -> artifacts         once DONE
    porter_job_cancel(id)   -> flips RunContext.cancel

The store is deliberately in-memory and process-local. Jobs do not survive a
restart — that is acceptable because ``RunContext.checkpoint_dir`` lets a
restarted job resume from the last completed stage instead.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from porter.context import RunContext
from porter.events import Event, JobState, ProgressUpdated
from porter.jobs.records import (
    JobRecord,
    JobRegistry,
    process_marker,
    record_from_result,
)
from porter.logging import get_logger
from porter.models.request import JobRequest, JobResult

__all__ = ["Job", "JobStore"]

_logger = get_logger(__name__)

#: How many recent events to keep per job for late-joining clients.
DEFAULT_REPLAY_SIZE = 200

#: How often the owner republishes progress to the shared registry. Status
#: polling tolerates a couple of seconds of staleness, and the registry is a
#: single JSON file shared by every process, so it must not be rewritten once per
#: progress event.
PUBLISH_INTERVAL_SECONDS = 2.0

#: How often the owner looks for a cancel request from another process.
#:
#: Checked by a dedicated watchdog thread, **not** from the event sink. Observing
#: cancellation through events was the first design and it did not work: a long
#: download emits no progress events at all, so a cancel issued during one was
#: never noticed. Cancellation must not depend on the work happening to report
#: progress.
CANCEL_POLL_SECONDS = 1.0


@dataclass
class Job:
    """State of one submitted job."""

    job_id: str
    request: JobRequest
    state: JobState = JobState.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: JobResult | None = None

    #: Most recent events, for ``porter job status`` and MCP progress replay.
    events: deque[Event] = field(
        default_factory=lambda: deque(maxlen=DEFAULT_REPLAY_SIZE)
    )

    #: Set to ask the running pipeline to stop. Backed by the RunContext.
    cancel: threading.Event = field(default_factory=threading.Event)

    #: Identity of the owning process, recorded so another process can tell
    #: whether this job is still alive (see :func:`porter.jobs.records.reap`).
    pid: int | None = None
    pid_start: float | None = None

    #: Throttle clock for the registry, in ``time.monotonic`` terms.
    _published_at: float = 0.0
    #: Tells the cancel watchdog to stop when the job is over.
    _watchdog_stop: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(end - (self.started_at or self.created_at), 0.0)

    def last_progress(self) -> ProgressUpdated | None:
        """Most recent progress event, if any."""
        for event in reversed(self.events):
            if isinstance(event, ProgressUpdated):
                return event
        return None

    def to_record(self) -> JobRecord:
        """Project this live job into its durable form.

        Only the newest progress event is carried: the registry answers "where is
        it now", and replaying the whole event stream belongs to the store, which
        is in the same process as the reader.

        A finished job delegates to :func:`record_from_result`, so the artifacts
        are the same whether a reader asks the live store or the shared file. It
        did not, and the two disagreed: the file had the output paths and the
        live store had none, so ``porter_job_result`` reported zero artifacts for
        a job that had just produced them.
        """
        progress = self.last_progress()
        result = self.result
        base = JobRecord(
            job_id=self.job_id,
            source=self.request.source,
            state=self.state.value,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            phase=progress.phase.value if progress is not None else None,
            percent=progress.percent if progress is not None else 0.0,
            message=progress.message if progress is not None else "",
            task_dir=(
                str(result.task_dir) if result is not None and result.task_dir else None
            ),
            error=result.error.message if result is not None and result.error else None,
            pid=self.pid,
            pid_start=self.pid_start,
        )
        if result is None:
            return base
        return record_from_result(base, result)


class JobStore:
    """Thread-safe registry of jobs.

    Both frontends share one store instance. The MCP server additionally
    serialises heavy jobs behind a semaphore — the store itself imposes no
    concurrency limit.
    """

    def __init__(self, max_jobs: int = 64, registry: JobRegistry | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque()
        self._lock = threading.RLock()
        self._max_jobs = max_jobs
        #: When set, every state change is mirrored into the shared file so other
        #: processes can see it. ``None`` keeps the store purely in memory, which
        #: is what tests and the single-shot CLI want.
        self._registry = registry

    @property
    def registry(self) -> JobRegistry | None:
        """The shared file this store publishes into, if any.

        Public because a frontend that owns jobs in memory still has to answer
        questions about jobs it does *not* own -- ``porter run`` in another
        terminal, say -- and the only place those live is the registry.
        """
        return self._registry

    # -- lifecycle ----------------------------------------------------------

    def create(self, request: JobRequest, job_id: str | None = None) -> Job:
        """Register a new job in ``PENDING`` state.

        ``job_id`` is optional because the two frontends want different things
        from an id. The MCP server tracks concurrent jobs and takes the random
        one; the CLI runs one job per process and supplies a stable id derived
        from the source, so a rerun of the same video replaces the previous
        record instead of accumulating a near-duplicate.
        """
        with self._lock:
            job = Job(job_id=job_id or uuid.uuid4().hex[:12], request=request)
            job.pid, job.pid_start = process_marker()
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._evict()
            self._publish(job, force=True)
            return job

    def attach(self, job: Job, ctx: RunContext) -> None:
        """Connect a job's cancellation flag and event buffer to ``ctx``.

        Must be called before the pipeline starts so that a cancel request
        arriving mid-run is observed. Also starts the cancel watchdog, which is
        what makes cross-process cancellation work independently of whether the
        current phase emits any events.
        """
        with self._lock:
            job.cancel = ctx.cancel
            job.state = JobState.RUNNING
            job.started_at = time.time()
        self._publish(job, force=True)

        previous = ctx.events

        def sink(event: Event) -> None:
            with self._lock:
                job.events.append(event)
            previous(event)
            self._publish(job)

        ctx.events = sink
        self._start_watchdog(job)

    def finish(self, job: Job, result: JobResult) -> None:
        """Record the terminal result of a job."""
        with self._lock:
            job.result = result
            job.state = result.state
            job.finished_at = time.time()
        job._watchdog_stop.set()
        if self._registry is not None:
            self._registry.publish(record_from_result(job.to_record(), result))

    # -- cross-process cancellation -----------------------------------------

    def _start_watchdog(self, job: Job) -> None:
        """Watch the shared file for a cancel request and apply it.

        A thread rather than a signal, and a thread rather than an event hook:

        * Not a signal, because the requester would have to signal a PID that may
          since have been recycled, and because a job owned by an MCP server and
          one owned by a terminal would then cancel differently.
        * Not an event hook, because that was the first attempt and it silently
          did nothing during a long download -- no events, no checks. The window
          in which a user most wants to cancel is exactly the window in which
          nothing is being reported.
        """
        if self._registry is None:
            return

        def watch() -> None:
            while not job._watchdog_stop.wait(CANCEL_POLL_SECONDS):
                if job.cancel.is_set():
                    return
                if self._registry is None:  # pragma: no cover - cannot change
                    return
                try:
                    requested = self._registry.is_cancel_requested(job.job_id)
                except Exception as exc:  # noqa: BLE001 - never kill the watchdog
                    _logger.warning("cancel watchdog for %s failed: %s", job.job_id, exc)
                    return
                if requested:
                    _logger.info("job %s: cancellation observed", job.job_id)
                    job.cancel.set()
                    return

        threading.Thread(
            target=watch, name=f"porter-cancel-{job.job_id}", daemon=True
        ).start()

    # -- registry projection ------------------------------------------------

    def _publish(self, job: Job, *, force: bool = False) -> None:
        """Mirror ``job`` into the shared file, at most every few seconds.

        Writing once per event would mean a locked read-modify-write of a JSON
        document shared by every process, several times a second, for information
        a polling client reads once every few seconds.
        """
        if self._registry is None:
            return
        now = time.monotonic()
        with self._lock:
            if not force and now - job._published_at < PUBLISH_INTERVAL_SECONDS:
                return
            job._published_at = now
            record = job.to_record()
        self._registry.publish(record)

    def _observe_cancel_now(self, job: Job) -> None:
        """One immediate cancellation check.

        Exists for the watchdog's own tests and for a caller that wants the check
        to happen at a specific moment; production uses the thread.
        """
        if self._registry is None or job.cancel.is_set():
            return
        if self._registry.is_cancel_requested(job.job_id):
            job.cancel.set()

    # -- access -------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def require(self, job_id: str) -> Job:
        """Like :meth:`get` but raises for an unknown id."""
        from porter.errors import PorterError

        job = self.get(job_id)
        if job is None:
            raise PorterError(f"unknown job id: {job_id}", job_id=job_id)
        return job

    def list(self) -> list[Job]:
        """Jobs, newest first."""
        with self._lock:
            return [self._jobs[jid] for jid in reversed(self._order) if jid in self._jobs]

    def cancel(self, job_id: str) -> bool:
        """Request cancellation. Returns ``False`` for unknown or finished jobs."""
        job = self.get(job_id)
        if job is None or job.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
            return False
        job.cancel.set()
        return True

    def cancel_all(self) -> int:
        """Request cancellation for every running job. Used on SIGTERM."""
        count = 0
        with self._lock:
            for job in self._jobs.values():
                if job.state in (JobState.PENDING, JobState.RUNNING):
                    job.cancel.set()
                    count += 1
        return count

    # -- internals ----------------------------------------------------------

    def _evict(self) -> None:
        """Drop the oldest *finished* jobs once over capacity."""
        while len(self._order) > self._max_jobs:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            if job is not None and job.state in (
                JobState.PENDING,
                JobState.RUNNING,
            ):
                break  # never evict in-flight work
            self._order.popleft()
            self._jobs.pop(oldest, None)
