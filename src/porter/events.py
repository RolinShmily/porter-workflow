"""Structured pipeline events.

The engine never reports progress by printing. It emits :class:`Event` instances
through an :data:`EventSink`; each frontend renders them its own way:

* ``porter_cli`` maps events onto a terminal progress display,
* ``porter_mcp`` maps events onto MCP ``notifications/progress`` messages,
* tests collect them into a list and assert on the sequence.

Every event is JSON-serialisable so the MCP frontend can forward it verbatim.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Phase(str, Enum):
    """The four stages of the localization pipeline."""

    PREPARE = "prepare"
    TRANSCRIBE = "transcribe"
    TRANSLATE = "translate"
    BURN = "burn"


class ArtifactKind(str, Enum):
    """Kinds of files the pipeline produces.

    Mirrors the on-disk contract (``raw/`` and ``cooked/``) documented in
    ``docs/ARCHITECTURE.md``. The names must not change without a migration
    note — downstream agents match on them.
    """

    VIDEO = "video"
    AUDIO = "audio"
    AUDIO_ENHANCED = "audio_enhanced"
    COVER = "cover"
    SUBTITLE_SRC = "subtitle_src"
    SUBTITLE_BILINGUAL_SRT = "subtitle_bilingual_srt"
    SUBTITLE_BILINGUAL_ASS = "subtitle_bilingual_ass"
    SUBTITLE_ZH_SRT = "subtitle_zh_srt"
    SUBTITLE_ZH_ASS = "subtitle_zh_ass"
    TRANSCRIPT_JSON = "transcript_json"
    TRANSCRIPT_TXT = "transcript_txt"
    METADATA = "metadata"
    VIDEO_BILINGUAL = "video_bilingual"
    VIDEO_ZH = "video_zh"


class JobState(str, Enum):
    """Lifecycle of a job tracked by :mod:`porter.jobs`."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorInfo(BaseModel):
    """Serialisable projection of a :class:`porter.errors.PorterError`."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_exception(cls, exc: BaseException) -> ErrorInfo:
        from porter.errors import PorterError

        if isinstance(exc, PorterError):
            return cls(code=exc.code, message=exc.message, details=dict(exc.details))
        return cls(code="unexpected_error", message=str(exc), details={"type": type(exc).__name__})


class Event(BaseModel):
    """Base class for everything the engine emits."""

    model_config = ConfigDict(frozen=True)

    ts: float = Field(default_factory=time.time)


class PhaseStarted(Event):
    """A pipeline phase began. ``total_steps`` is a lower bound, not a promise."""

    type: Literal["phase_started"] = "phase_started"
    phase: Phase
    total_steps: int = 0


class StepCompleted(Event):
    """One unit of work inside a phase finished."""

    type: Literal["step_completed"] = "step_completed"
    phase: Phase
    step: int
    name: str


class ProgressUpdated(Event):
    """Coarse progress inside a long-running step (download, encode)."""

    type: Literal["progress_updated"] = "progress_updated"
    phase: Phase
    percent: float = Field(ge=0.0, le=100.0)
    message: str = ""


class ArtifactReady(Event):
    """A file is complete and safe to consume."""

    type: Literal["artifact_ready"] = "artifact_ready"
    kind: ArtifactKind
    path: Path
    phase: Phase


class LogRecord(Event):
    """A human-oriented message. Rendering is the frontend's decision."""

    type: Literal["log_record"] = "log_record"
    level: Literal["debug", "info", "warning", "error"]
    message: str


class PhaseFailed(Event):
    """A phase aborted. The pipeline stops after emitting this."""

    type: Literal["phase_failed"] = "phase_failed"
    phase: Phase
    error: ErrorInfo


class PhaseCompleted(Event):
    """A phase finished successfully."""

    type: Literal["phase_completed"] = "phase_completed"
    phase: Phase


AnyEvent = Annotated[
    PhaseStarted | PhaseCompleted | StepCompleted | ProgressUpdated | ArtifactReady | LogRecord | PhaseFailed,
    Field(discriminator="type"),
]

#: Consumers receive every event here. Must not raise; must not block for long.
EventSink = Callable[[Event], None]


def collect(sink: list[Event]) -> EventSink:
    """Return an :data:`EventSink` that appends into ``sink``.

    Convenience for tests and for the job store, which keeps a bounded replay
    buffer so late MCP clients can catch up.
    """

    def _sink(event: Event) -> None:
        sink.append(event)

    return _sink


def null_sink(event: Event) -> None:
    """Discard every event."""


__all__ = [
    "AnyEvent",
    "ArtifactKind",
    "ArtifactReady",
    "ErrorInfo",
    "Event",
    "EventSink",
    "JobState",
    "LogRecord",
    "Phase",
    "PhaseCompleted",
    "PhaseFailed",
    "PhaseStarted",
    "ProgressUpdated",
    "StepCompleted",
    "collect",
    "null_sink",
]
