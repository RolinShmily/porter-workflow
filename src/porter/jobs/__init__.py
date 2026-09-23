"""Job tracking, in two layers.

:mod:`~porter.jobs.store`
    The **live** layer: :class:`Job` and :class:`JobStore`. Thread-safe, in
    memory, authoritative for the process that owns the work. Answers "what is
    running right now, and what should I show the user".

:mod:`~porter.jobs.records`
    The **durable** layer: :class:`JobRecord` and :class:`JobRegistry`. A shared
    JSON file. Answers "what has been run", for a user in another terminal or an
    MCP client that reconnected.

Both frontends use both. The store publishes into the registry, so the registry
is a projection and never a second source of truth.

Why jobs at all: an MCP client times out long before a 1080p encode finishes, so
long work is exposed as start/status/result/cancel rather than one blocking call.
"""

from __future__ import annotations

from porter.jobs.records import (
    SCHEMA_VERSION,
    JobRecord,
    JobRegistry,
    process_marker,
    record_from_request,
    record_from_result,
    registry_file,
)
from porter.jobs.store import DEFAULT_REPLAY_SIZE, Job, JobStore

__all__ = [
    "DEFAULT_REPLAY_SIZE",
    "SCHEMA_VERSION",
    "Job",
    "JobRecord",
    "JobRegistry",
    "JobStore",
    "process_marker",
    "record_from_request",
    "record_from_result",
    "registry_file",
]
