"""Cross-process cancellation, against a real pipeline and a real CLI.

This is the one thing about the job registry that unit tests cannot establish:
that a request written by *another process* actually stops work running here.
Every piece has a unit test -- the watchdog observes a flag, the CLI writes one,
the pipeline stops at a checkpoint -- and the composition is where it broke.

It broke in a specific, instructive way. Cancellation was first observed from the
pipeline's event sink, which is a reasonable-looking design: the sink already
exists, it already runs on every phase change, and no extra thread is needed. It
silently did nothing during a long download, because a download emits no events
at all -- precisely the window in which a user most wants to cancel. A test that
only checked "the CLI wrote the flag" would have passed throughout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from porter.config import PorterConfig
from porter.context import RunContext
from porter.events import JobState, null_sink
from porter.jobs import JobRegistry, JobStore
from porter.models.request import BurnMode, JobOptions, JobRequest
from porter.pipeline import Pipeline

pytestmark = pytest.mark.slow

#: How long the source video is. Long enough that transcoding it comfortably
#: outlasts the cancel round trip (a second or two), short enough to keep the test
#: quick. A much shorter clip makes the test race the transcode.
SOURCE_SECONDS = 60


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A video PREPARE must *transcode*, so it takes measurable time.

    mpeg4 in an AVI container, so it cannot be stream-copied into the mp4 master
    the way an h264 source would be. That is what makes room to cancel: a
    stream-copy PREPARE finishes in about a second.
    """
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")

    path = tmp_path / "source.avi"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=duration={SOURCE_SECONDS}:size=640x360:rate=15",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={SOURCE_SECONDS}",
            "-c:v", "mpeg4", "-q:v", "5", "-c:a", "mp3", "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def shared_cache(tmp_path: Path) -> Path:
    """A cache directory the subprocess CLI and this process both resolve to.

    ``registry_file`` is built from ``platformdirs.user_cache_dir``, which on
    Linux is ``$XDG_CACHE_HOME/porter``. Pointing the environment variable at a
    temp directory therefore puts both processes on the same file without either
    of them being told a path.
    """
    cache = tmp_path / "cache"
    (cache / "porter").mkdir(parents=True)
    return cache


def _cli(cache: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real CLI in a separate process."""
    return subprocess.run(
        [sys.executable, "-m", "porter_cli", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "XDG_CACHE_HOME": str(cache)},
        cwd=cache,
    )


def test_a_cancel_from_another_process_stops_the_job(
    source: Path,
    shared_cache: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Poll faster than production does. This does not weaken the test -- the
    # mechanism is identical, only the interval changes -- and it keeps PREPARE
    # (a few seconds) comfortably longer than the cancel round trip, which is the
    # only reason a longer source would be needed.
    monkeypatch.setattr("porter.jobs.store.CANCEL_POLL_SECONDS", 0.2)
    """The whole point of the shared registry.

    A job is started here, the CLI cancels it from another process, and the work
    stops. Asserted on the job's final state, not on the flag: writing the flag
    was never the part that could break.
    """
    from porter.platforms.local import LocalFileDownloader

    registry = JobRegistry(shared_cache / "porter" / "jobs.json")
    store = JobStore(registry=registry)
    request = JobRequest.from_source(
        # SKIP keeps BURN out of the phase list, so the checkpoint after PREPARE
        # is the first one available. force makes the transcode happen even if an
        # earlier run already standardised this file.
        #
        source,
        JobOptions(output_dir=tmp_path / "out", burn=BurnMode.SKIP, force=True),
    )
    job = store.create(request)
    ctx = RunContext(
        job_id=job.job_id,
        options=request.options,
        config=PorterConfig(),
        events=null_sink,
    )
    store.attach(job, ctx)

    def work() -> None:
        pipeline = Pipeline.default(ctx)
        pipeline.local = LocalFileDownloader()
        store.finish(job, pipeline.run(request, ctx))

    thread = threading.Thread(target=work, name="cancel-test-worker")
    thread.start()

    try:
        # Cancel as soon as the job is running. Waiting a fixed interval first
        # loses the race against PREPARE, and losing it looks like a cancellation
        # bug rather than a test that arrived late.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and job.state is JobState.PENDING:
            time.sleep(0.05)

        assert job.state is JobState.RUNNING, "the job finished before it could be cancelled"

        cancelled = _cli(shared_cache, "jobs", "cancel", job.job_id)
        assert cancelled.returncode == 0, cancelled.stderr

        # Cooperative: it stops at the boundary after the phase it is in.
        thread.join(timeout=120)
        assert not thread.is_alive(), "the job ignored the cancel request"

        assert job.state is JobState.CANCELLED, (
            f"expected cancelled, got {job.state.value}: "
            f"{(job.result.error.message if job.result and job.result.error else '')}"
        )
    finally:
        thread.join(timeout=10)

    # And the other process can see that it worked.
    status = _cli(shared_cache, "jobs", "status", job.job_id)
    assert "cancelled" in status.stderr


def test_the_registry_is_where_the_two_processes_meet(
    source: Path, shared_cache: Path, tmp_path: Path
) -> None:
    """A sanity check on the fixture, not on the feature.

    If the two processes resolved different paths, the test above would fail for
    a reason that has nothing to do with cancellation -- and it would look like a
    cancellation bug. This makes that failure mode legible.
    """
    registry = JobRegistry(shared_cache / "porter" / "jobs.json")
    store = JobStore(registry=registry)
    job = store.create(JobRequest.from_source(source, JobOptions()))

    listed = _cli(shared_cache, "jobs", "list", "--all", "--json")

    assert job.job_id in listed.stdout, (
        f"the subprocess did not resolve the same registry; stdout={listed.stdout!r}"
    )
