"""The on-disk job registry.

Why this exists on top of :class:`~porter.jobs.store.JobStore`: the store answers
*"what is this process running"*, which is the wrong question for a user in
another terminal, or for an MCP client that lost its connection. The registry
answers *"what has been run"* — across processes and across restarts.

The store stays authoritative for live state; this file is a projection it
publishes into. That split is deliberate: every progress event would otherwise
cost a locked read-modify-write of a shared JSON document.

Layout::

    {"version": 1, "jobs": [ {...}, ... ]}

Every write is atomic (temp file + :func:`os.replace`) and serialised behind an
advisory lock, because the MCP server and any number of CLI invocations share one
file. A corrupt or unreadable file reads as empty rather than raising: a broken
cache must never stop the CLI from working.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import platformdirs

from porter.events import JobState
from porter.logging import get_logger
from porter.models.request import JobRequest, JobResult

__all__ = [
    "SCHEMA_VERSION",
    "JobRecord",
    "JobRegistry",
    "process_marker",
    "registry_file",
]

_logger = get_logger(__name__)

APP_NAME = "porter"
REGISTRY_FILENAME = "jobs.json"
LOCK_FILENAME = "jobs.lock"

#: ``ERROR_INVALID_PARAMETER``: what ``OpenProcess`` returns for a PID that does
#: not exist. ``ERROR_ACCESS_DENIED`` is the other failure and means the opposite
#: -- the process is there, and is not ours to inspect.
_ERROR_INVALID_PARAMETER = 87

#: ``STILL_ACTIVE``: what ``GetExitCodeProcess`` reports while a process runs.
_STILL_ACTIVE = 259

#: ``PROCESS_QUERY_LIMITED_INFORMATION``: granted for processes the caller does not
#: own, and all ``GetProcessTimes`` needs.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: Bumped when the record shape changes incompatibly. A file with any other
#: version is ignored rather than migrated: this is a cache, and the cost of
#: losing it is one round of "which job was that".
SCHEMA_VERSION = 1

#: Records to keep. Older *finished* records are dropped on write.
MAX_RECORDS = 200

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - platform dependent
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


def registry_file() -> Path:
    """Where the registry lives (``platformdirs`` cache dir)."""
    return Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False)) / REGISTRY_FILENAME


def _kernel32() -> Any:
    """``kernel32`` with the argtypes these process queries need.

    The ``argtypes`` are not decoration. Without them ctypes passes a HANDLE as a
    C ``int``, which truncates it to 32 bits on a 64-bit process and silently
    asks about a different handle.
    """
    import ctypes
    from ctypes import wintypes

    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    lib.OpenProcess.restype = wintypes.HANDLE
    lib.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    lib.GetExitCodeProcess.restype = wintypes.BOOL
    lib.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    lib.GetProcessTimes.restype = wintypes.BOOL
    lib.CloseHandle.argtypes = [wintypes.HANDLE]
    lib.CloseHandle.restype = wintypes.BOOL
    return lib


def _windows_pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` is a running process -- Windows' answer to ``os.kill(pid, 0)``.

    ``os.kill(pid, 0)`` is not usable for this. Windows keeps a terminated process
    openable for a moment after it exits -- measured 8 times out of 8 on a process
    reaped by ``wait()`` -- so the POSIX-style liveness check reports a just-dead
    owner as alive and its record sits at ``running`` forever. That is exactly the
    failure the reaper exists to prevent, so liveness is asked directly.

    A process that exists but cannot be opened (access denied) counts as **alive**:
    "does it exist" is the question, and reaping another user's running job
    because it was not ours to inspect would be the worst available answer.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER is "no such process"; anything else -- access
        # denied, most importantly -- means it is there.
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_start_time(pid: int) -> float | None:
    """Creation time of ``pid``, or ``None`` if it has no identity to give.

    Windows has no ``/proc``, so the identity marker comes from
    ``GetProcessTimes``: a ``FILETIME`` of 100-nanosecond ticks since 1601-01-01.
    Only *equality* matters, so the epoch is irrelevant -- but the value must be
    stable across calls, and it is.

    ``None`` is returned for a process that has already exited, and that check is
    not optional. ``OpenProcess`` keeps succeeding for a terminated process for a
    moment after it dies -- measured 8 times out of 8 on a process reaped by
    ``wait()`` -- so the creation time alone would report a dead owner as
    identifiable. The caller would then treat the record as owned by a live
    process, which is precisely the "polling a job that will never finish" bug
    the marker exists to prevent. ``GetExitCodeProcess`` settles it.

    Liveness is :func:`_windows_pid_is_alive`'s question; this function answers
    only "what is its identity". They are separate because the fallback in
    :func:`_owner_is_alive` needs the liveness answer on its own.
    """
    import ctypes
    from ctypes import wintypes

    if not _windows_pid_is_alive(pid):
        # A terminated process has no identity to give, and ``OpenProcess`` keeps
        # succeeding for one for a moment after it dies, so the creation time
        # alone would report a dead owner as identifiable.
        return None

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:  # pragma: no cover - lost the race with the process exiting
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 10_000_000.0
    finally:
        kernel32.CloseHandle(handle)


def process_marker(pid: int | None = None) -> tuple[int, float | None]:
    """Identify this process as ``(pid, start time)``.

    A bare PID is not an identity: the kernel recycles them, so a record saying
    "owned by PID 4321" can silently come to mean an unrelated process that
    happens to have inherited the number. The start time disambiguates.

    On POSIX it is field 22 of ``/proc/<pid>/stat`` (clock ticks since boot).
    Field 2 is the command name and may itself contain spaces and parentheses, so
    the fields are split after the final ``)``. On Windows, where there is no
    ``/proc``, it is the creation time from ``GetProcessTimes``. Returns ``None``
    where neither is available, and callers then fall back to a plain liveness
    check -- which cannot detect recycling, so this returning ``None`` is a real
    loss of protection, not a detail.
    """
    target = os.getpid() if pid is None else pid
    if sys.platform == "win32":
        return target, _windows_start_time(target)
    try:
        stat = Path(f"/proc/{target}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return target, None

    tail = stat.rpartition(")")[2].split()
    # Field 22 overall, and field 2 is the command name, so it is index 19 here.
    if len(tail) < 20:
        return target, None
    try:
        return target, float(tail[19])
    except ValueError:
        return target, None


def _owner_is_alive(pid: int | None, pid_start: float | None) -> bool:
    """Whether the recorded owner process is still the process it claims to be."""
    if pid is None:
        # Written by something that did not record an owner: assume alive rather
        # than reap a job that may well be running.
        return True

    # The PID itself cannot disagree: it was the lookup key. Only the start time
    # can, and when it does the number was recycled.
    _current_pid, current_start = process_marker(pid)
    if current_start is not None and pid_start is not None:
        return current_start == pid_start

    if sys.platform == "win32":
        # Windows needs its own answer. A terminated process stays openable for a
        # moment, so ``os.kill(pid, 0)`` reports a just-dead owner as alive and
        # its record sits at ``running`` forever -- and it raises a bare
        # ``OSError`` rather than ``ProcessLookupError`` for a gone one, which
        # aborted the whole registry read. One stale record, exactly what a killed
        # job leaves behind, made ``porter jobs list`` die with a traceback.
        return _windows_pid_is_alive(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else. Alive, and not ours to reap.
        return True
    except OSError as exc:
        # Unknown, and "dead" is the wrong guess: reaping a job that is still
        # running is a lie, while keeping a stale record is merely untidy.
        _logger.warning("could not tell whether pid %s is alive: %s", pid, exc)
        return True
    return True


@dataclass
class JobRecord:
    """One job as recorded on disk.

    Flat and JSON-friendly on purpose: this crosses a process boundary, and a
    reader in another terminal has none of the live objects the owner holds.
    """

    job_id: str
    source: str
    state: str = JobState.PENDING.value
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    phase: str | None = None
    percent: float = 0.0
    message: str = ""

    task_dir: str | None = None
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None

    #: Owner identity, for staleness detection.
    pid: int | None = None
    pid_start: float | None = None

    #: Set by another process to ask the owner to stop. The owner observes it
    #: through the event sink it already has, so no signal is involved.
    cancel_requested: bool = False

    @property
    def state_enum(self) -> JobState:
        try:
            return JobState(self.state)
        except ValueError:
            return JobState.FAILED

    @property
    def is_finished(self) -> bool:
        return self.state in {
            JobState.DONE.value,
            JobState.FAILED.value,
            JobState.CANCELLED.value,
        }

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(end - (self.started_at or self.created_at), 0.0)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> JobRecord | None:
        """Build a record, or ``None`` if the entry is not usable.

        Unknown keys are ignored rather than rejected: a newer version's file is
        still mostly readable, and refusing it would lose information a user
        might need.
        """
        if not isinstance(data, dict):
            return None
        job_id = data.get("job_id")
        source = data.get("source")
        if not isinstance(job_id, str) or not isinstance(source, str):
            return None

        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def reap(self) -> JobRecord:
        """Return this record with a dead owner marked failed.

        A process that is ``kill -9``'d cannot write its own obituary, so a
        record can sit at ``running`` forever. Reporting that as still-running is
        worse than useless: it is a lie a polling client will wait on.
        """
        if self.is_finished or _owner_is_alive(self.pid, self.pid_start):
            return self

        return JobRecord(
            **{
                **asdict(self),
                "state": JobState.FAILED.value,
                "finished_at": self.finished_at or time.time(),
                "error": (
                    f"the process that owned this job (pid {self.pid}) is gone; "
                    "it was killed or crashed before finishing"
                ),
            }
        )


def record_from_result(record: JobRecord, result: JobResult) -> JobRecord:
    """Fill in the terminal fields of ``record`` from a finished job."""
    artifacts: list[str] = []
    if result.task_dir is not None:
        artifacts.append(str(result.task_dir))
    for group in (result.subtitles, result.burn):
        if group is None:
            continue
        for value in group.model_dump().values():
            if isinstance(value, Path) and value.is_file():
                artifacts.append(str(value))

    return JobRecord(
        **{
            **asdict(record),
            "state": result.state.value,
            "finished_at": time.time(),
            "task_dir": str(result.task_dir) if result.task_dir is not None else None,
            "artifacts": artifacts,
            "error": result.error.message if result.error is not None else None,
            "cancel_requested": False,
        }
    )


def record_from_request(job_id: str, request: JobRequest) -> JobRecord:
    """A fresh record for a job that has just been accepted."""
    pid, pid_start = process_marker()
    return JobRecord(
        job_id=job_id,
        source=request.source,
        pid=pid,
        pid_start=pid_start,
    )


def _lock_exclusive(handle: Any) -> None:
    """Take an exclusive advisory lock on an open file, on either platform.

    ``cast(Any, ...)`` rather than a per-line ``type: ignore``: ``fcntl`` and
    ``msvcrt`` are each declared only for their own platform, so under ``strict``
    the ignore would be reported as *unused* on the platform that does have the
    module. The runtime branch below is what makes this safe, not the type system.
    """
    if fcntl is not None:
        module = cast(Any, fcntl)
        module.flock(handle.fileno(), module.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        module = cast(Any, msvcrt)
        module.locking(handle.fileno(), module.LK_LOCK, 1)


def _unlock(handle: Any) -> None:
    """Release the lock taken by :func:`_lock_exclusive`."""
    if fcntl is not None:
        module = cast(Any, fcntl)
        module.flock(handle.fileno(), module.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        module = cast(Any, msvcrt)
        module.locking(handle.fileno(), module.LK_UNLCK, 1)


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock, creating the file if needed.

    Advisory, so it only excludes other users of this module -- which is exactly
    the set of writers there is. Where neither ``fcntl`` nor ``msvcrt`` exists the
    lock degrades to nothing: a lost update in an exotic environment is a better
    outcome than refusing to record anything at all.
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError as exc:
        # No lock file means no mutual exclusion, which risks a lost update -- but
        # refusing to proceed would mean a job is not recorded at all, and a
        # read-only cache directory is a legitimate setup. Losing one history
        # entry beats losing the job.
        _logger.warning("could not open the job registry lock %s: %s", lock_path, exc)
        yield
        return

    try:
        _lock_exclusive(handle)
        yield
    finally:
        try:
            _unlock(handle)
        finally:
            handle.close()


class JobRegistry:
    """Read and write the shared job file.

    Constructed with an explicit path in tests; defaults to
    :func:`registry_file` otherwise.

    The default is resolved **per access, not in ``__init__``**. ``registry_file``
    is the seam the test suite redirects, and freezing it at construction time
    means freezing whatever it pointed at *then* -- which, for a module-level
    store such as ``porter_mcp.tools.jobs._STORE``, is import time, before any
    fixture can run. That is how the suite came to write real job records into
    the developer's ``~/.cache/porter/jobs.json`` (four per full run: the
    cancellation and failure tests of ``porter_job_*``), which the isolation
    fixture did not catch because it patched the seam too late.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._explicit = Path(path) if path is not None else None

    @property
    def path(self) -> Path:
        """The registry file. Follows :func:`registry_file` unless pinned."""
        return self._explicit if self._explicit is not None else registry_file()

    @property
    def lock_path(self) -> Path:
        """The sidecar lock file, always beside :attr:`path`."""
        return self.path.with_name(LOCK_FILENAME)

    # -- reading ------------------------------------------------------------

    def read(self, *, reap: bool = False) -> list[JobRecord]:
        """Records, newest first. Stale ones are corrected in memory.

        ``reap=True`` also persists the corrections. Reading normally does not
        write, so a read-only mount or a read-only cache directory still works.
        """
        raw = self._load()
        records = [record.reap() for record in raw]

        if reap:
            reaped = [
                corrected
                for original, corrected in zip(raw, records, strict=True)
                if corrected != original
            ]
            if reaped:
                self._replace(records)
                for record in reaped:
                    _logger.warning("reaped stale job %s: %s", record.job_id, record.error)

        return sorted(records, key=lambda r: r.created_at, reverse=True)

    def get(self, job_id: str, *, reap: bool = False) -> JobRecord | None:
        for record in self.read(reap=reap):
            if record.job_id == job_id:
                return record
        return None

    def is_cancel_requested(self, job_id: str) -> bool:
        """Whether another process has asked ``job_id`` to stop.

        Deliberately uncorrected: reaping here would make a polling owner write
        to the file on every progress event.
        """
        for record in self._load():
            if record.job_id == job_id:
                return record.cancel_requested
        return False

    # -- writing ------------------------------------------------------------

    def publish(self, record: JobRecord) -> None:
        """Insert or replace one record."""
        with _exclusive_lock(self.lock_path):
            records = self._load_unlocked()
            for index, existing in enumerate(records):
                if existing.job_id == record.job_id:
                    records[index] = record
                    break
            else:
                records.append(record)
            self._write_unlocked(_trim(records))

    def request_cancel(self, job_id: str) -> bool:
        """Ask the owner to stop. ``False`` for unknown or already-finished jobs.

        Nothing is signalled: the flag is picked up by the owner's event sink,
        which keeps cancellation working the same way whether the request came
        from a terminal or from an MCP client.
        """
        with _exclusive_lock(self.lock_path):
            records = self._load_unlocked()
            for index, record in enumerate(records):
                if record.job_id != job_id:
                    continue
                if record.reap().is_finished:
                    return False
                records[index] = JobRecord(**{**asdict(record), "cancel_requested": True})
                self._write_unlocked(_trim(records))
                return True
        return False

    def clear(self) -> int:
        """Drop finished records. Returns how many were removed."""
        with _exclusive_lock(self.lock_path):
            records = self._load_unlocked()
            kept = [record for record in records if not record.reap().is_finished]
            removed = len(records) - len(kept)
            if removed:
                self._write_unlocked(_trim(kept))
            return removed

    # -- internals ----------------------------------------------------------

    def _load(self) -> list[JobRecord]:
        with _exclusive_lock(self.lock_path):
            return self._load_unlocked()

    def _load_unlocked(self) -> list[JobRecord]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            _logger.warning("could not read %s: %s", self.path, exc)
            return []

        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            _logger.warning("ignoring corrupt job registry %s: %s", self.path, exc)
            return []

        if not isinstance(document, dict):
            _logger.warning("ignoring job registry %s: not an object", self.path)
            return []
        if document.get("version") != SCHEMA_VERSION:
            _logger.warning(
                "ignoring job registry %s: schema version %r, expected %d",
                self.path,
                document.get("version"),
                SCHEMA_VERSION,
            )
            return []

        entries = document.get("jobs")
        if not isinstance(entries, list):
            return []
        return [record for record in map(JobRecord.from_json, entries) if record is not None]

    def _replace(self, records: list[JobRecord]) -> None:
        with _exclusive_lock(self.lock_path):
            self._write_unlocked(_trim(records))

    def _write_unlocked(self, records: list[JobRecord]) -> None:
        """Write atomically: a reader never sees a half-written file.

        ``os.replace`` is atomic within a filesystem, so the temp file has to be
        a sibling. ``ensure_ascii`` keeps the file valid under an ASCII locale,
        and ``encoding`` is explicit for the same reason it is everywhere else in
        this codebase.
        """
        document = {"version": SCHEMA_VERSION, "jobs": [r.to_json() for r in records]}
        payload = json.dumps(document, ensure_ascii=True, indent=2)

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(f".{self.path.name}.tmp")
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            # Recording history is not worth failing a job over.
            _logger.warning("could not write %s: %s", self.path, exc)


def _trim(records: list[JobRecord]) -> list[JobRecord]:
    """Keep the newest :data:`MAX_RECORDS`, never dropping unfinished work."""
    if len(records) <= MAX_RECORDS:
        return records
    ordered = sorted(records, key=lambda r: r.created_at)
    unfinished = [r for r in ordered if not r.reap().is_finished]
    finished = [r for r in ordered if r.reap().is_finished]
    room = max(MAX_RECORDS - len(unfinished), 0)
    return unfinished + finished[-room:] if room else unfinished
