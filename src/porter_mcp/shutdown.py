"""Graceful shutdown for the stdio server.

A signal cannot be handed to the pipeline, and is not a cancel request: the only
thing that stops an encode is the pipeline noticing a flag. So the handler does
exactly what ``porter_job_cancel`` does -- flip the flag on every job this process
owns -- and then waits, briefly, for the threads to unwind.

What the wait buys:

* **Jobs that can notice stop where they are.** Cancellation is checked between
  steps, and the downloader checks it too, so the common interrupt (a long
  download, a long translation) ends promptly instead of being killed mid-write.
* **The registry gets an accurate record.** ``JobStore.finish`` runs on the job
  thread, so a job that unwinds writes ``cancelled`` itself. Exit without waiting
  and the record sits at ``running`` until another process reaps it from a dead
  PID -- inferring, badly, what we could have said outright.

What it deliberately does not do:

* **Interrupt an in-flight ffmpeg encode.** That is a single subprocess call with
  no cancellation point inside it, so a burn finishes its current step. The grace
  period is a courtesy, not a guarantee, which is why it is bounded.
* **Delete scratch.** ``.tmp/`` is removed by PREPARE and ``cooked/.tmp_*`` is
  unlinked by the next burn, so a leftover is already self-healing. Deriving task
  directories from a signal handler would be a way to delete the wrong thing:
  a task directory is named after the video's *title*, which is not known until
  the metadata probe has already run.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType
from typing import TYPE_CHECKING

from porter.events import JobState
from porter.logging import get_logger

if TYPE_CHECKING:
    from porter.jobs import Job, JobStore

__all__ = ["GRACE_SECONDS", "SIGNALS", "cancel_and_wait", "install"]

_logger = get_logger(__name__)

#: How long to let cancelled jobs unwind before exiting anyway.
#:
#: Long enough for a download or a translation to reach its next cancellation
#: check, short enough that a stuck job cannot hold the terminal hostage.
GRACE_SECONDS = 5.0

#: How often to look at a job's state while waiting. Small enough to feel
#: immediate, large enough not to spin.
_POLL_SECONDS = 0.05

#: Signals that mean "stop". ``SIGBREAK`` exists only on Windows, so it is looked
#: up rather than named; on POSIX the lookup yields nothing and it is dropped.
SIGNALS: tuple[int, ...] = tuple(
    sig
    for sig in (
        getattr(signal, "SIGINT", None),
        getattr(signal, "SIGTERM", None),
        getattr(signal, "SIGBREAK", None),
    )
    if sig is not None
)


def _wait_for(job: Job, timeout: float) -> bool:
    """Wait until ``job`` reaches a terminal state, or ``timeout`` elapses.

    Polled rather than event-driven: ``JobStore.finish`` sets ``finished_at`` on
    the job thread and signals no event, and adding one would mean the engine
    carrying a mechanism used by exactly one caller.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while job.finished_at is None and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
    return job.finished_at is not None


def cancel_and_wait(store: JobStore, *, grace_seconds: float = GRACE_SECONDS) -> list[str]:
    """Ask every job ``store`` owns to stop, and wait up to ``grace_seconds``.

    Returns the ids that were asked to stop, so a caller can report what it did
    rather than assume. Jobs that do not unwind in time are reported, not
    hidden: their records will be reaped later from a dead PID, and that is worth
    a line in the log.
    """
    running: list[Job] = [
        job for job in store.list() if job.state in (JobState.PENDING, JobState.RUNNING)
    ]
    if not running:
        return []

    store.cancel_all()
    deadline = time.monotonic() + grace_seconds
    stopped = 0
    for job in running:
        if _wait_for(job, deadline - time.monotonic()):
            stopped += 1

    unfinished = len(running) - stopped
    if unfinished:
        _logger.warning(
            "%d of %d job(s) did not stop within %.0fs; they are still running and the "
            "next porter process will reap their records",
            unfinished,
            len(running),
            grace_seconds,
        )
    else:
        _logger.info("stopped %d job(s) cleanly", stopped)
    return [job.job_id for job in running]


def _leave(code: int) -> None:
    """Exit immediately, without unwinding.

    ``os._exit`` rather than ``sys.exit``: this runs inside a signal handler while
    the main thread is blocked in the transport's event loop, so raising
    ``SystemExit`` there would have to unwind through code that does not expect
    it. stdout is flushed first because it *is* the JSON-RPC channel -- a
    buffered response left unflushed is a protocol error on the way out.
    """
    for stream in (sys.stdout, sys.stderr):
        # A stream that is already closed is not worth failing a shutdown over.
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
    os._exit(code)


def _make_handler(
    store: JobStore | None, *, grace_seconds: float, exit_code: int | None
) -> Callable[[int, FrameType | None], None]:
    """Build the signal handler.

    A second signal during the grace period skips the wait. Someone who presses
    Ctrl+C twice means it, and making them sit through a courtesy timeout is how
    "graceful" turns into "unkillable".
    """
    stopping = threading.Event()

    def handle(signum: int, _frame: FrameType | None) -> None:
        code = exit_code if exit_code is not None else 128 + signum
        if stopping.is_set():
            _logger.warning("second interrupt; exiting without waiting")
            _leave(code)
            # Not merely cosmetic: ``_leave`` does not return in production, and
            # relying on that would make this branch's correctness depend on
            # another function's implementation detail.
            return
        stopping.set()

        _logger.warning("received signal %d; asking jobs to stop", signum)
        target = store
        if target is None:
            # Imported here, not at module scope: this module is imported by the
            # server entry point, and ``tools.jobs`` builds the process-global
            # store as a side effect of import.
            from porter_mcp.tools.jobs import job_store

            target = job_store()
        cancel_and_wait(target, grace_seconds=grace_seconds)
        _leave(code)

    return handle


def install(
    *,
    store: JobStore | None = None,
    grace_seconds: float = GRACE_SECONDS,
    exit_code: int | None = None,
) -> bool:
    """Install the shutdown handlers. Returns ``False`` when it cannot be done.

    Refusing is the honest answer in two real cases: ``signal.signal`` only works
    on the main thread (so a test, or an embedder that called this from a worker,
    gets ``False`` instead of a ``ValueError``), and a platform may not define a
    given signal at all.

    ``exit_code=None`` means the conventional ``128 + signum`` -- 130 for SIGINT,
    143 for SIGTERM.
    """
    if threading.current_thread() is not threading.main_thread():
        _logger.debug("not installing signal handlers: not on the main thread")
        return False

    handler = _make_handler(store, grace_seconds=grace_seconds, exit_code=exit_code)
    installed = False
    for sig in SIGNALS:
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):  # pragma: no cover - platform dependent
            continue
        installed = True
    return installed
