"""Job tracking: the live store, the durable registry, and the CLI surface.

The two layers are tested separately and then together, because the interesting
failures live in the seam: a store that forgets to publish, a registry that
believes a recycled PID is still running, a throttle that never fires.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from porter.config import PorterConfig
from porter.context import RunContext
from porter.events import ArtifactReady, JobState, Phase, ProgressUpdated
from porter.jobs import (
    SCHEMA_VERSION,
    JobRecord,
    JobRegistry,
    JobStore,
    process_marker,
    record_from_request,
    record_from_result,
    registry_file,
)
from porter.models.request import (
    BurnResult,
    ErrorInfo,
    JobOptions,
    JobRequest,
    JobResult,
)
from porter.models.subtitle import SubtitleItem, SubtitleSet


@pytest.fixture
def registry(tmp_path: Path) -> JobRegistry:
    """A registry in a temp directory, never the user's real cache."""
    return JobRegistry(tmp_path / "jobs.json")


def _request(source: str = "https://example.com/v") -> JobRequest:
    return JobRequest(url=source, options=JobOptions())


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        job_id="job-1",
        options=JobOptions(output_dir=tmp_path / "out"),
        config=PorterConfig(),
    )


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


# ----------------------------------------------------------------------
# The record
# ----------------------------------------------------------------------


class TestJobRecord:
    def test_it_round_trips_through_json(self) -> None:
        original = JobRecord(job_id="a", source="u", percent=42.5, artifacts=["x"])

        restored = JobRecord.from_json(json.loads(json.dumps(original.to_json())))

        assert restored == original

    def test_unknown_keys_are_ignored_not_rejected(self) -> None:
        """A newer version's file is mostly readable; refusing it loses data.

        The alternative -- rejecting anything with an unfamiliar key -- means a
        user who upgrades, then downgrades, silently loses their job history.
        """
        record = JobRecord.from_json({"job_id": "a", "source": "u", "future_field": 1})

        assert record is not None
        assert record.job_id == "a"

    @pytest.mark.parametrize(
        "payload",
        [
            "not a dict",
            {"source": "u"},  # no job_id
            {"job_id": "a"},  # no source
            {"job_id": 1, "source": "u"},  # wrong type
            None,
        ],
    )
    def test_unusable_entries_become_none(self, payload: object) -> None:
        assert JobRecord.from_json(payload) is None

    def test_an_unknown_state_reads_as_failed(self) -> None:
        """Better a wrong-but-terminal state than a job that never finishes."""
        assert JobRecord(job_id="a", source="u", state="banana").state_enum is JobState.FAILED

    @pytest.mark.parametrize(
        ("state", "finished"),
        [
            (JobState.PENDING, False),
            (JobState.RUNNING, False),
            (JobState.DONE, True),
            (JobState.FAILED, True),
            (JobState.CANCELLED, True),
        ],
    )
    def test_is_finished(self, state: JobState, finished: bool) -> None:
        assert JobRecord(job_id="a", source="u", state=state.value).is_finished is finished

    def test_elapsed_uses_the_finish_time_when_there_is_one(self) -> None:
        record = JobRecord(
            job_id="a", source="u", started_at=100.0, finished_at=160.0
        )

        assert record.elapsed_seconds == pytest.approx(60.0)

    def test_elapsed_of_a_running_job_grows(self) -> None:
        record = JobRecord(job_id="a", source="u", started_at=time.time() - 5)

        assert record.elapsed_seconds == pytest.approx(5.0, abs=1.0)


class TestProcessMarker:
    def test_it_returns_this_process_and_a_start_time(self) -> None:
        pid, started = process_marker()

        assert pid == os.getpid()
        # None on a platform without /proc; a float on Linux.
        assert started is None or started > 0

    def test_the_same_process_yields_the_same_marker(self) -> None:
        """The marker is an identity, so it must be stable across calls."""
        assert process_marker() == process_marker()

    def test_a_missing_process_yields_no_start_time(self) -> None:
        dead = _a_definitely_dead_pid()

        pid, started = process_marker(dead)

        assert started is None
        assert pid == dead


def _a_definitely_dead_pid() -> int:
    """A PID nothing is using.

    Found by spawning a process that exits immediately and waiting for it. The
    kernel may later recycle the number, but not within a test, and the start-time
    comparison would catch it anyway.
    """
    import subprocess

    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# ----------------------------------------------------------------------
# Reaping
# ----------------------------------------------------------------------


class TestReaping:
    """A killed process cannot write its own obituary.

    Left alone, its record sits at ``running`` forever, and a polling client
    waits on a job that will never finish.
    """

    def test_a_dead_owner_is_marked_failed(self) -> None:
        record = JobRecord(
            job_id="a", source="u", state=JobState.RUNNING.value,
            pid=_a_definitely_dead_pid(), pid_start=1234.0,
        )

        reaped = record.reap()

        assert reaped.state == JobState.FAILED.value
        assert "is gone" in (reaped.error or "")
        assert reaped.finished_at is not None

    def test_a_recycled_pid_does_not_count_as_alive(self) -> None:
        """The whole reason a start time is stored.

        The PID is alive -- it is this very process -- but its start time is not
        the one recorded, so the number was reused and the original owner is gone.
        A bare PID check would call this job running forever.
        """
        record = JobRecord(
            job_id="a", source="u", state=JobState.RUNNING.value,
            pid=os.getpid(), pid_start=1.0,
        )

        assert record.reap().state == JobState.FAILED.value

    def test_the_real_owner_is_not_reaped(self) -> None:
        pid, started = process_marker()
        record = JobRecord(
            job_id="a", source="u", state=JobState.RUNNING.value, pid=pid, pid_start=started
        )

        assert record.reap() is record

    @pytest.mark.parametrize(
        "state", [JobState.DONE, JobState.FAILED, JobState.CANCELLED]
    )
    def test_a_finished_job_is_never_reaped(self, state: JobState) -> None:
        record = JobRecord(
            job_id="a", source="u", state=state.value, pid=_a_definitely_dead_pid()
        )

        assert record.reap() is record

    def test_a_record_without_an_owner_is_left_alone(self) -> None:
        """Absent evidence of death, assume life.

        Reaping a job that is genuinely running is worse than leaving a stale one:
        it reports a failure that did not happen.
        """
        record = JobRecord(job_id="a", source="u", state=JobState.RUNNING.value)

        assert record.reap() is record


# ----------------------------------------------------------------------
# The registry file
# ----------------------------------------------------------------------


class TestRegistryReading:
    def test_a_missing_file_reads_as_empty(self, registry: JobRegistry) -> None:
        assert registry.read() == []

    def test_a_corrupt_file_reads_as_empty_and_does_not_raise(
        self, registry: JobRegistry
    ) -> None:
        """A broken cache must never stop the CLI from working."""
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.path.write_text("{not json", encoding="utf-8")

        assert registry.read() == []

    def test_a_document_that_is_not_an_object_reads_as_empty(
        self, registry: JobRegistry
    ) -> None:
        _write(registry.path, [1, 2, 3])

        assert registry.read() == []

    def test_a_future_schema_version_is_ignored(self, registry: JobRegistry) -> None:
        """Guessing at an unknown format risks reading nonsense as job state."""
        _write(registry.path, {"version": SCHEMA_VERSION + 1, "jobs": [{"job_id": "a"}]})

        assert registry.read() == []

    def test_a_missing_jobs_list_reads_as_empty(self, registry: JobRegistry) -> None:
        _write(registry.path, {"version": SCHEMA_VERSION})

        assert registry.read() == []

    def test_records_come_back_newest_first(self, registry: JobRegistry) -> None:
        for index, created in enumerate([100.0, 300.0, 200.0]):
            registry.publish(JobRecord(job_id=str(index), source="u", created_at=created))

        assert [r.created_at for r in registry.read()] == [300.0, 200.0, 100.0]

    def test_get_finds_one_record(self, registry: JobRegistry) -> None:
        registry.publish(JobRecord(job_id="a", source="u"))

        assert registry.get("a") is not None
        assert registry.get("nope") is None

    def test_reading_does_not_write(self, registry: JobRegistry) -> None:
        """Reads must work on a read-only cache directory."""
        registry.publish(JobRecord(job_id="a", source="u"))
        before = registry.path.stat().st_mtime_ns

        registry.read()

        assert registry.path.stat().st_mtime_ns == before

    def test_reap_true_persists_the_correction(self, registry: JobRegistry) -> None:
        registry.publish(
            JobRecord(
                job_id="a", source="u", state=JobState.RUNNING.value,
                pid=_a_definitely_dead_pid(), pid_start=1.0,
            )
        )

        assert registry.read(reap=True)[0].state == JobState.FAILED.value
        # Persisted, not merely recomputed on every read.
        assert registry.read()[0].state == JobState.FAILED.value


class TestRegistryWriting:
    def test_publish_inserts_then_replaces(self, registry: JobRegistry) -> None:
        registry.publish(JobRecord(job_id="a", source="u", percent=1.0))
        registry.publish(JobRecord(job_id="a", source="u", percent=2.0))

        records = registry.read()
        assert len(records) == 1
        assert records[0].percent == 2.0

    def test_no_temp_file_is_left_behind(self, registry: JobRegistry) -> None:
        registry.publish(JobRecord(job_id="a", source="u"))

        leftovers = [p.name for p in registry.path.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_the_file_is_valid_json_with_a_version(self, registry: JobRegistry) -> None:
        registry.publish(JobRecord(job_id="a", source="u"))

        document = json.loads(registry.path.read_text(encoding="utf-8"))
        assert document["version"] == SCHEMA_VERSION
        assert len(document["jobs"]) == 1

    def test_the_file_is_ascii_safe(self, registry: JobRegistry) -> None:
        """Written with ensure_ascii, so an ASCII locale cannot break the write."""
        registry.publish(JobRecord(job_id="a", source="中文 视频"))

        assert registry.path.read_bytes().isascii()

    def test_writing_survives_an_unwritable_directory(self, tmp_path: Path) -> None:
        """Recording history is not worth failing a job over."""
        blocked = tmp_path / "afile"
        blocked.write_text("x", encoding="utf-8")
        registry = JobRegistry(blocked / "jobs.json")

        registry.publish(JobRecord(job_id="a", source="u"))  # must not raise

        assert registry.read() == []

    def test_concurrent_writers_do_not_lose_records(self, registry: JobRegistry) -> None:
        """Two processes share this file; the lock is what makes that safe."""
        errors: list[Exception] = []

        def publish(index: int) -> None:
            try:
                registry.publish(JobRecord(job_id=f"job-{index}", source="u"))
            except Exception as exc:  # noqa: BLE001 - recorded and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=publish, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(registry.read()) == 16


class TestRegistryCancel:
    def test_a_cancel_request_is_recorded(self, registry: JobRegistry) -> None:
        registry.publish(JobRecord(job_id="a", source="u", state=JobState.RUNNING.value))

        assert registry.request_cancel("a") is True
        assert registry.is_cancel_requested("a") is True

    def test_cancelling_an_unknown_job_is_false(self, registry: JobRegistry) -> None:
        assert registry.request_cancel("nope") is False

    @pytest.mark.parametrize(
        "state", [JobState.DONE, JobState.FAILED, JobState.CANCELLED]
    )
    def test_cancelling_a_finished_job_is_false(
        self, registry: JobRegistry, state: JobState
    ) -> None:
        registry.publish(JobRecord(job_id="a", source="u", state=state.value))

        assert registry.request_cancel("a") is False

    def test_a_stale_job_cannot_be_cancelled(self, registry: JobRegistry) -> None:
        """Its owner is gone, so the request would wait forever."""
        registry.publish(
            JobRecord(
                job_id="a", source="u", state=JobState.RUNNING.value,
                pid=_a_definitely_dead_pid(), pid_start=1.0,
            )
        )

        assert registry.request_cancel("a") is False

    def test_an_unrequested_job_reports_false(self, registry: JobRegistry) -> None:
        registry.publish(JobRecord(job_id="a", source="u", state=JobState.RUNNING.value))

        assert registry.is_cancel_requested("a") is False


class TestRegistryClear:
    def test_clear_drops_finished_jobs_only(self, registry: JobRegistry) -> None:
        registry.publish(JobRecord(job_id="done", source="u", state=JobState.DONE.value))
        registry.publish(JobRecord(job_id="failed", source="u", state=JobState.FAILED.value))
        registry.publish(JobRecord(job_id="running", source="u", state=JobState.RUNNING.value))

        assert registry.clear() == 2

        assert [r.job_id for r in registry.read()] == ["running"]

    def test_clear_on_an_empty_registry_returns_zero(self, registry: JobRegistry) -> None:
        assert registry.clear() == 0


class TestRegistryTrim:
    def test_old_finished_records_are_dropped(self, registry: JobRegistry) -> None:
        from porter.jobs.records import MAX_RECORDS

        for index in range(MAX_RECORDS + 25):
            registry.publish(
                JobRecord(
                    job_id=f"j{index}", source="u",
                    state=JobState.DONE.value, created_at=float(index),
                )
            )

        records = registry.read()
        assert len(records) == MAX_RECORDS
        # The survivors are the newest, not an arbitrary slice.
        assert max(r.created_at for r in records) == float(MAX_RECORDS + 24)

    def test_unfinished_records_are_never_trimmed(self, registry: JobRegistry) -> None:
        """Dropping a running job would hide work that is still happening."""
        from porter.jobs.records import MAX_RECORDS

        for index in range(MAX_RECORDS + 10):
            registry.publish(
                JobRecord(
                    job_id=f"done{index}", source="u",
                    state=JobState.DONE.value, created_at=float(index),
                )
            )
        registry.publish(
            JobRecord(job_id="running", source="u", state=JobState.RUNNING.value)
        )

        assert "running" in {r.job_id for r in registry.read()}


class TestRegistryFileLocation:
    def test_it_lives_under_the_platform_cache_dir(self) -> None:
        assert registry_file().name == "jobs.json"
        assert "porter" in str(registry_file())

    def test_a_default_registry_follows_the_seam_after_construction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default path is resolved per access, not frozen in ``__init__``.

        ``porter_mcp.tools.jobs`` builds its store at module import, so its
        registry is constructed before any test fixture runs. Resolving the path
        at construction froze the developer's real ``~/.cache/porter/jobs.json``
        and the suite wrote real job records into it -- four per full run, which
        is how it surfaced: ``porter jobs list`` showed ``/videos/a.mp4`` entries
        created by the test process itself. The §13.33 fixture patched the seam
        correctly but too late to matter for an already-built registry.
        """
        from porter.jobs import records as records_module

        # Stands in for import time: built while the seam pointed elsewhere.
        built_early = records_module.JobRegistry()
        later = tmp_path / "later" / "jobs.json"
        monkeypatch.setattr(records_module, "registry_file", lambda: later)

        assert built_early.path == later
        assert built_early.lock_path == later.with_name("jobs.lock")

    def test_an_explicit_path_is_still_pinned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lazy resolution must not undo injection for tests and embedders."""
        from porter.jobs import records as records_module

        pinned = tmp_path / "pinned.json"
        registry = records_module.JobRegistry(pinned)
        monkeypatch.setattr(records_module, "registry_file", lambda: tmp_path / "other.json")

        assert registry.path == pinned
        assert registry.lock_path == pinned.with_name("jobs.lock")


# ----------------------------------------------------------------------
# Record construction from engine objects
# ----------------------------------------------------------------------


class TestRecordFromRequest:
    def test_it_records_the_source_and_the_owner(self) -> None:
        record = record_from_request("j1", _request("https://example.com/v"))

        assert record.job_id == "j1"
        assert record.source == "https://example.com/v"
        assert record.pid == os.getpid()
        assert record.state == JobState.PENDING.value

    def test_a_local_file_is_recorded_as_its_path(self) -> None:
        request = JobRequest.from_source("/videos/a.mp4", JobOptions())

        assert record_from_request("j1", request).source == "/videos/a.mp4"


class TestRecordFromResult:
    def test_it_collects_the_task_dir_and_existing_artifacts(
        self, tmp_path: Path
    ) -> None:
        task_dir = tmp_path / "task"
        (task_dir / "cooked").mkdir(parents=True)
        bilingual = task_dir / "cooked" / "video_bilingual.mp4"
        bilingual.write_bytes(b"x")
        missing = task_dir / "cooked" / "video_zh.mp4"  # never written

        result = JobResult(
            job_id="j1",
            state=JobState.DONE,
            task_dir=task_dir,
            burn=BurnResult(video_bilingual=bilingual, video_zh=missing),
        )

        record = record_from_result(JobRecord(job_id="j1", source="u"), result)

        assert str(task_dir) in record.artifacts
        assert str(bilingual) in record.artifacts
        # A path that was never written is not an artifact.
        assert str(missing) not in record.artifacts

    def test_it_records_the_error_and_clears_the_cancel_flag(self) -> None:
        result = JobResult(
            job_id="j1",
            state=JobState.FAILED,
            error=ErrorInfo(code="media_error", message="ffmpeg said no"),
        )
        original = JobRecord(job_id="j1", source="u", cancel_requested=True)

        record = record_from_result(original, result)

        assert record.state == JobState.FAILED.value
        assert record.error == "ffmpeg said no"
        assert record.cancel_requested is False

    def test_subtitle_paths_are_collected(self, tmp_path: Path) -> None:
        srt = tmp_path / "subtitle_zh.srt"
        srt.write_text("1\n", encoding="utf-8")

        result = JobResult(
            job_id="j1",
            state=JobState.DONE,
            subtitles=SubtitleSet(
                # All four are required by the model; only the one that exists on
                # disk should end up as an artifact.
                subtitle_bilingual_srt=tmp_path / "subtitle_bilingual.srt",
                subtitle_bilingual_ass=tmp_path / "subtitle_bilingual.ass",
                subtitle_zh_srt=srt,
                subtitle_zh_ass=tmp_path / "subtitle_zh.ass",
                transcript_json_path=tmp_path / "transcript.json",
                transcript_txt_path=tmp_path / "transcript.txt",
                items=[
                    SubtitleItem(
                        index=1, start_ms=0, end_ms=1000,
                        source_text="a", target_text="甲",
                    )
                ],
            ),
        )

        record = record_from_result(JobRecord(job_id="j1", source="u"), result)

        assert str(srt) in record.artifacts


# ----------------------------------------------------------------------
# The live store, publishing into the registry
# ----------------------------------------------------------------------


class TestStorePublication:
    def test_create_publishes_a_pending_record(self, registry: JobRegistry) -> None:
        store = JobStore(registry=registry)

        job = store.create(_request("https://example.com/v"))

        record = registry.get(job.job_id)
        assert record is not None
        assert record.state == JobState.PENDING.value
        assert record.source == "https://example.com/v"

    def test_create_accepts_a_caller_supplied_id(self, registry: JobRegistry) -> None:
        store = JobStore(registry=registry)

        job = store.create(_request(), job_id="run-abc123")

        assert job.job_id == "run-abc123"
        assert registry.get("run-abc123") is not None

    def test_a_rerun_with_the_same_id_replaces_the_record(
        self, registry: JobRegistry, tmp_path: Path
    ) -> None:
        """The CLI derives ids from the source, so this is the normal case."""
        store = JobStore(registry=registry)
        first = store.create(_request(), job_id="run-abc123")
        store.finish(first, JobResult(job_id="run-abc123", state=JobState.FAILED))

        second = store.create(_request(), job_id="run-abc123")
        store.attach(second, _ctx(tmp_path))

        assert len(registry.read()) == 1
        assert registry.get("run-abc123").state == JobState.RUNNING.value

    def test_attach_publishes_running(self, registry: JobRegistry, tmp_path: Path) -> None:
        store = JobStore(registry=registry)
        job = store.create(_request())

        store.attach(job, _ctx(tmp_path))

        record = registry.get(job.job_id)
        assert record.state == JobState.RUNNING.value
        assert record.started_at is not None

    def test_attach_chains_the_previous_event_sink(
        self, registry: JobRegistry, tmp_path: Path
    ) -> None:
        """The frontend's own rendering must survive being wrapped."""
        seen: list[object] = []
        ctx = _ctx(tmp_path)
        ctx.events = seen.append
        store = JobStore(registry=registry)
        job = store.create(_request())

        store.attach(job, ctx)
        ctx.emit(ProgressUpdated(phase=Phase.PREPARE, percent=10.0, message="hi"))

        assert len(seen) == 1

    def test_progress_reaches_the_record(
        self, registry: JobRegistry, tmp_path: Path
    ) -> None:
        store = JobStore(registry=registry)
        job = store.create(_request())
        ctx = _ctx(tmp_path)
        store.attach(job, ctx)

        ctx.emit(ProgressUpdated(phase=Phase.PREPARE, percent=55.0, message="halfway"))
        store._publish(job, force=True)

        record = registry.get(job.job_id)
        assert record.phase == Phase.PREPARE.value
        assert record.percent == pytest.approx(55.0)
        assert record.message == "halfway"

    def test_finish_publishes_the_terminal_state(
        self, registry: JobRegistry, tmp_path: Path
    ) -> None:
        store = JobStore(registry=registry)
        job = store.create(_request())
        store.attach(job, _ctx(tmp_path))

        store.finish(job, JobResult(job_id=job.job_id, state=JobState.DONE))

        record = registry.get(job.job_id)
        assert record.state == JobState.DONE.value
        assert record.finished_at is not None

    def test_a_store_without_a_registry_publishes_nothing(self) -> None:
        """Tests and single-shot use want no shared file at all."""
        store = JobStore()

        job = store.create(_request())

        assert store.get(job.job_id) is job

    def test_publication_is_throttled(
        self, registry: JobRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The registry is one JSON file shared by every process.

        Rewriting it once per progress event would mean a locked
        read-modify-write several times a second, for information a polling
        client reads once every few seconds.
        """
        monkeypatch.setattr("porter.jobs.store.PUBLISH_INTERVAL_SECONDS", 3600.0)
        store = JobStore(registry=registry)
        job = store.create(_request())
        ctx = _ctx(tmp_path)
        store.attach(job, ctx)

        writes = 0
        original = registry.publish

        def counting_publish(record: JobRecord) -> None:
            nonlocal writes
            writes += 1
            original(record)

        monkeypatch.setattr(registry, "publish", counting_publish)

        for percent in range(0, 100, 10):
            ctx.emit(ProgressUpdated(phase=Phase.PREPARE, percent=float(percent), message=""))

        assert writes == 0, "throttled progress must not touch the file"

    def test_a_phase_change_is_not_delayed_by_the_throttle(
        self, registry: JobRegistry, tmp_path: Path
    ) -> None:
        """Artifacts are the interesting events; they must not wait."""
        store = JobStore(registry=registry)
        job = store.create(_request())
        ctx = _ctx(tmp_path)
        store.attach(job, ctx)

        ctx.emit(ArtifactReady(phase=Phase.PREPARE, kind="video", path=tmp_path / "v.mp4"))
        store._publish(job, force=True)

        assert registry.get(job.job_id) is not None


class TestStoreObservesCancel:
    """Cancellation crosses processes through the file, not a signal.

    A signal would have to be sent to a PID that may since have been recycled,
    and would behave differently for a job owned by an MCP server than for one
    owned by a terminal. A flag observed by a watchdog thread behaves identically
    in both.
    """

    def test_a_request_from_another_process_stops_the_job(
        self, registry: JobRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("porter.jobs.store.CANCEL_POLL_SECONDS", 0.01)
        store = JobStore(registry=registry)
        job = store.create(_request())
        store.attach(job, _ctx(tmp_path))

        registry.request_cancel(job.job_id)

        assert job.cancel.wait(timeout=5.0), "the watchdog never observed the request"

    def test_a_request_is_observed_without_any_events(
        self, registry: JobRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression that made the first design useless.

        Cancellation was originally observed from the event sink. A long download
        emits no events at all, so a cancel issued during one was never noticed --
        and the window where a user most wants to cancel is exactly the window in
        which nothing is being reported. Not a single event is emitted here.
        """
        monkeypatch.setattr("porter.jobs.store.CANCEL_POLL_SECONDS", 0.01)
        store = JobStore(registry=registry)
        job = store.create(_request())
        ctx = _ctx(tmp_path)
        store.attach(job, ctx)

        registry.request_cancel(job.job_id)

        assert job.cancel.wait(timeout=5.0)

    def test_the_watchdog_stops_when_the_job_finishes(
        self, registry: JobRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A leaked thread per job would accumulate for the life of the server."""
        monkeypatch.setattr("porter.jobs.store.CANCEL_POLL_SECONDS", 0.01)
        store = JobStore(registry=registry)
        job = store.create(_request())
        store.attach(job, _ctx(tmp_path))

        store.finish(job, JobResult(job_id=job.job_id, state=JobState.DONE))

        assert job._watchdog_stop.wait(timeout=5.0)
        # A request arriving after the job is over must not resurrect it.
        registry.request_cancel(job.job_id)
        time.sleep(0.05)
        assert not job.cancel.is_set()

    def test_a_store_without_a_registry_starts_no_watchdog(
        self, tmp_path: Path
    ) -> None:
        """Nothing to watch, and a thread per job is not free."""
        store = JobStore()
        job = store.create(_request())

        store.attach(job, _ctx(tmp_path))

        assert job.cancel.is_set() is False

    def test_an_unrequested_job_is_not_cancelled(
        self, registry: JobRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("porter.jobs.store.CANCEL_POLL_SECONDS", 0.01)
        store = JobStore(registry=registry)
        job = store.create(_request())
        store.attach(job, _ctx(tmp_path))

        time.sleep(0.1)

        assert not job.cancel.is_set()

    def test_a_registry_that_raises_does_not_kill_the_watchdog(
        self, registry: JobRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A watchdog that dies on a transient error stops cancelling entirely."""
        monkeypatch.setattr("porter.jobs.store.CANCEL_POLL_SECONDS", 0.01)
        store = JobStore(registry=registry)
        job = store.create(_request())
        store.attach(job, _ctx(tmp_path))

        def boom(job_id: str) -> bool:
            raise OSError("transient")

        monkeypatch.setattr(registry, "is_cancel_requested", boom)

        time.sleep(0.05)

        # The thread ended cleanly rather than propagating into the interpreter.
        assert job.cancel.is_set() is False


class TestToRecordCarriesArtifacts:
    """The live store and the shared file must agree.

    They did not: the file had the output paths and the live store had none, so
    ``porter_job_result`` reported zero artifacts for a job that had just produced
    them -- while ``porter jobs status`` on the same job listed them.
    """

    def test_a_finished_job_records_its_artifacts(self, tmp_path: Path) -> None:
        store = JobStore()
        job = store.create(_request())
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        video = task_dir / "video_zh.mp4"
        video.write_bytes(b"x")

        store.finish(
            job,
            JobResult(
                job_id=job.job_id,
                state=JobState.DONE,
                task_dir=task_dir,
                burn=BurnResult(video_bilingual=None, video_zh=video),
            ),
        )

        record = job.to_record()
        assert str(task_dir) in record.artifacts
        assert str(video) in record.artifacts

    def test_the_live_record_matches_the_published_one(self, registry: JobRegistry) -> None:
        store = JobStore(registry=registry)
        job = store.create(_request())
        store.finish(job, JobResult(job_id=job.job_id, state=JobState.DONE))

        assert job.to_record().artifacts == registry.get(job.job_id).artifacts

    def test_a_running_job_has_no_artifacts(self) -> None:
        store = JobStore()
        job = store.create(_request())

        assert job.to_record().artifacts == []


class TestStoreEviction:
    def test_in_flight_jobs_are_never_evicted(self) -> None:
        store = JobStore(max_jobs=2)
        first = store.create(_request())
        second = store.create(_request())

        store.create(_request())

        assert store.get(first.job_id) is not None
        assert store.get(second.job_id) is not None

    def test_the_oldest_finished_job_is_evicted(self) -> None:
        store = JobStore(max_jobs=2)
        first = store.create(_request())
        store.finish(first, JobResult(job_id=first.job_id, state=JobState.DONE))
        store.create(_request())

        store.create(_request())

        assert store.get(first.job_id) is None
