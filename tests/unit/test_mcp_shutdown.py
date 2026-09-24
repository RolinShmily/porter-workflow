"""Graceful shutdown on a signal — see ``porter_mcp.shutdown``.

The behaviour under test is cooperative cancellation plus an honest record, not
signal delivery itself: a test that sent itself a real signal would either kill
the test process or be testing CPython.
"""

from __future__ import annotations

import signal
import threading

import pytest

from porter.events import JobState
from porter.jobs import JobStore
from porter.models.request import JobOptions, JobRequest, JobResult
from porter_mcp import shutdown


def _store_with_running_jobs(count: int = 1) -> tuple[JobStore, list[object]]:
    """A store holding ``count`` jobs in RUNNING, and no registry file."""
    store = JobStore(registry=None)
    jobs = []
    for index in range(count):
        request = JobRequest.from_source(f"/videos/{index}.mp4", JobOptions())
        job = store.create(request, job_id=f"j{index}")
        job.state = JobState.RUNNING
        jobs.append(job)
    return store, jobs


class _Recorder:
    """Stands in for the module logger, which does not propagate to caplog."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str, *args: object) -> None:
        self.messages.append(message % args if args else message)

    def info(self, message: str, *args: object) -> None:
        self.messages.append(message % args if args else message)

    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


class TestCancelAndWait:
    def test_a_cancelled_job_is_waited_for(self) -> None:
        """The wait is the point: it is what lets the job record its own outcome."""
        store, jobs = _store_with_running_jobs(1)
        job = jobs[0]

        def worker() -> None:
            # Stands in for the job thread: notices the flag, then records.
            assert job.cancel.wait(5.0)  # type: ignore[attr-defined]
            store.finish(job, JobResult(job_id=job.job_id, state=JobState.CANCELLED))

        threading.Thread(target=worker, daemon=True).start()

        ids = shutdown.cancel_and_wait(store, grace_seconds=5.0)

        assert ids == [job.job_id]
        assert job.finished_at is not None  # type: ignore[attr-defined]
        assert job.state is JobState.CANCELLED  # type: ignore[attr-defined]

    def test_the_cancel_flag_is_actually_set(self) -> None:
        store, jobs = _store_with_running_jobs(2)

        shutdown.cancel_and_wait(store, grace_seconds=0.0)

        assert all(job.cancel.is_set() for job in jobs)  # type: ignore[attr-defined]

    def test_nothing_running_is_not_an_error(self) -> None:
        store = JobStore(registry=None)

        assert shutdown.cancel_and_wait(store, grace_seconds=0.0) == []

    def test_a_finished_job_is_left_alone(self) -> None:
        store, jobs = _store_with_running_jobs(1)
        job = jobs[0]
        store.finish(job, JobResult(job_id=job.job_id, state=JobState.DONE))

        assert shutdown.cancel_and_wait(store, grace_seconds=0.0) == []
        assert not job.cancel.is_set()  # type: ignore[attr-defined]

    def test_a_job_that_ignores_the_cancel_is_reported_not_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An encode in flight cannot be interrupted, so this case is real.

        It must not hang the shutdown, and it must not be silent: the record will
        be reaped later from a dead PID, which is worse information than a line
        in the log now.
        """
        recorder = _Recorder()
        monkeypatch.setattr(shutdown, "_logger", recorder)
        store, jobs = _store_with_running_jobs(1)
        # No worker thread, so nothing will ever record an outcome.

        ids = shutdown.cancel_and_wait(store, grace_seconds=0.05)

        assert ids == [jobs[0].job_id]
        assert jobs[0].finished_at is None  # type: ignore[attr-defined]
        assert any("did not stop" in message for message in recorder.messages)


class TestTheHandler:
    def test_it_cancels_then_leaves_with_128_plus_the_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, jobs = _store_with_running_jobs(1)
        codes: list[int] = []
        monkeypatch.setattr(shutdown, "_leave", codes.append)

        handler = shutdown._make_handler(store, grace_seconds=0.0, exit_code=None)
        handler(signal.SIGINT, None)

        assert codes == [128 + signal.SIGINT]
        assert jobs[0].cancel.is_set()  # type: ignore[attr-defined]

    def test_an_explicit_exit_code_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store, _jobs = _store_with_running_jobs(1)
        codes: list[int] = []
        monkeypatch.setattr(shutdown, "_leave", codes.append)

        shutdown._make_handler(store, grace_seconds=0.0, exit_code=7)(signal.SIGTERM, None)

        assert codes == [7]

    def test_a_second_signal_does_not_wait_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing Ctrl+C twice means it; a courtesy timeout must not trap them."""
        store, _jobs = _store_with_running_jobs(1)
        codes: list[int] = []
        cancels: list[str] = []
        monkeypatch.setattr(shutdown, "_leave", codes.append)
        monkeypatch.setattr(
            shutdown,
            "cancel_and_wait",
            lambda _store, **_kw: cancels.append("cancel") or [],
        )
        # A long grace period, so an unskipped wait would be obvious rather than
        # merely slow.
        handler = shutdown._make_handler(store, grace_seconds=30.0, exit_code=None)

        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)

        assert cancels == ["cancel"], "the second signal must not cancel again"
        assert codes == [128 + signal.SIGINT, 128 + signal.SIGINT]


class TestInstall:
    def test_it_refuses_off_the_main_thread(self) -> None:
        """``signal.signal`` raises off the main thread; False is the honest answer."""
        result: dict[str, bool] = {}

        def run() -> None:
            result["installed"] = shutdown.install(grace_seconds=0.0)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()

        assert result["installed"] is False

    def test_it_installs_on_the_main_thread_and_can_be_undone(self) -> None:
        original = {sig: signal.getsignal(sig) for sig in shutdown.SIGNALS}
        try:
            assert shutdown.install(grace_seconds=0.0) is True
            assert signal.getsignal(signal.SIGINT) is not original[signal.SIGINT]
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)

        assert signal.getsignal(signal.SIGINT) is original[signal.SIGINT]

    def test_the_signal_set_covers_the_ways_a_server_is_stopped(self) -> None:
        assert signal.SIGINT in shutdown.SIGNALS, "Ctrl+C"
        assert signal.SIGTERM in shutdown.SIGNALS, "systemd / docker stop"
