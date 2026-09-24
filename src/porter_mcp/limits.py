"""Concurrency limits.

The concurrency caps (see ``docs/MCP.md`` §4.2), made into one artifact instead
of a semaphore per tool. Two caps, for two different reasons:

``HEAVY`` -- one at a time. A single encode already saturates the machine, and
running two makes both take twice as long while thrashing the encoder. Queuing is
strictly better than racing. Jobs beyond the first wait in ``PENDING``.

``LIGHT`` -- four at a time, for work that is cheap *here* and not *there*. An
agent fanning out over fifty links should not open fifty sockets to one platform,
and every probe may retry twice.

Both are :class:`threading.Semaphore`, not ``asyncio.Semaphore``, because the work
they guard is synchronous -- yt-dlp and ffmpeg block the calling thread, and
FastMCP runs a synchronous tool body in a worker thread. An asyncio semaphore
would be released by a different task than the one that acquired it the moment a
tool body blocked, which is a bug that shows up only under load.
"""

from __future__ import annotations

import threading

__all__ = ["HEAVY", "HEAVY_CONCURRENCY", "LIGHT", "LIGHT_CONCURRENCY"]

#: Encoding and downloading. One at a time.
HEAVY_CONCURRENCY = 1

#: Network probes (inspection, planning) and local capability checks.
LIGHT_CONCURRENCY = 4

HEAVY = threading.Semaphore(HEAVY_CONCURRENCY)
LIGHT = threading.Semaphore(LIGHT_CONCURRENCY)
