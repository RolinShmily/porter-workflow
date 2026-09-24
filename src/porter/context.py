"""Per-run execution context.

Everything a pipeline stage needs that is *not* part of the request: where to
emit events, whether the caller wants to abort, and which logger to use.

v0.1 threaded ``on_progress: Callable[[str, int], None]`` and a bare
``PorterConfig`` through every function, and had no cancellation mechanism at
all — which is unusable behind MCP, where the client may disconnect at any
moment. :class:`RunContext` replaces that ad-hoc plumbing with one object.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from porter.config import PorterConfig
from porter.errors import JobCancelled
from porter.events import Event, EventSink, Phase, ProgressUpdated, null_sink
from porter.logging import get_logger
from porter.models.request import JobOptions

__all__ = ["RunContext"]


@dataclass
class RunContext:
    """Mutable state shared by every stage of a single pipeline run."""

    job_id: str
    options: JobOptions

    events: EventSink = null_sink
    cancel: threading.Event = field(default_factory=threading.Event)
    logger: Any = None

    #: Fully resolved configuration (defaults <- config files <- env <- flags).
    #:
    #: Lives here rather than on the request because it is *not* per-job: ffmpeg
    #: paths, encoder settings and subtitle styling are properties of the
    #: installation. Stages read it instead of re-resolving config each time,
    #: which is what v0.1 did and why a changed config file could take effect
    #: halfway through a run.
    config: PorterConfig = field(default_factory=PorterConfig)

    def __post_init__(self) -> None:
        if self.logger is None:
            self.logger = get_logger("pipeline")

    @property
    def output_root(self) -> Path:
        """Root directory that task folders are created under.

        Derived from the request rather than stored, so the two can never
        disagree.
        """
        return Path(self.options.output_dir)

    # -- event emission -----------------------------------------------------

    def emit(self, event: Event) -> None:
        """Forward ``event`` to the sink.

        Never raises: a broken sink (or a closed MCP client) must not fail the
        job. Cancellation is checked separately by :meth:`check_cancelled`.
        """
        try:
            self.events(event)
        except Exception:
            self.logger.debug("event sink raised; event dropped", exc_info=True)

    def progress(self, phase: Phase, percent: float, message: str = "") -> None:
        """Emit a clamped :class:`~porter.events.ProgressUpdated`."""
        self.emit(
            ProgressUpdated(
                phase=phase,
                percent=min(max(percent, 0.0), 100.0),
                message=message,
            )
        )

    # -- cancellation -------------------------------------------------------

    def request_cancel(self) -> None:
        """Ask the pipeline to stop at the next cancellation check.

        Safe from any thread.
        """
        self.cancel.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def check_cancelled(self) -> None:
        """Raise :class:`~porter.errors.JobCancelled` if cancellation was requested.

        Stages call this between units of work — not mid-download. Long-running
        subprocesses additionally register their handle so they can be killed
        promptly; see ``porter.media.ffmpeg.run_ffmpeg``.
        """
        if self.cancel.is_set():
            raise JobCancelled(f"job {self.job_id} was cancelled")
