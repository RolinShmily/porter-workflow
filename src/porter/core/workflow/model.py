"""The resolved workflow plan.

This module is the contract between the resolver, the executor and every
frontend. The rules it encodes are deliberate:

* a plan contains **one capability per stage**, never a candidate list. Cross-
  capability substitution is a planning decision, not an execution side effect;
* the stage list is fully resolved. There are no runtime conditions: whether a
  stage exists is decided while the plan is built;
* every model is JSON-serialisable, because an agent is expected to read it,
  approve it, and hand it back to ``porter run --plan``.

The old :mod:`porter.core.plan` model described the pipeline as a prediction.
This model is the thing the executor actually runs.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from porter.core.events import JobState

__all__ = [
    "CapabilityDescriptor",
    "Cost",
    "Egress",
    "PlanIssue",
    "PlanRuntime",
    "SourceKind",
    "SourceProbe",
    "Stage",
    "StageError",
    "StagePlan",
    "StageResult",
    "Task",
    "TrackInfo",
    "WorkflowId",
    "WorkflowPlan",
    "WorkflowResult",
]


class SourceKind(StrEnum):
    """What the single positional input actually is."""

    URL = "url"
    LOCAL_VIDEO = "local_video"
    SUBTITLE = "subtitle"
    AUDIO = "audio"


class Stage(StrEnum):
    """One unit of work in a workflow."""

    PROBE = "probe"
    ACQUIRE = "acquire"
    NORMALIZE = "normalize"
    TRANSCRIPT = "transcript"
    TRANSLATE = "translate"
    WRITE = "write"
    RENDER = "render"


class Task(StrEnum):
    """What the caller asked for. A task truncates a workflow; it never picks a backend."""

    PROBE = "probe"
    TRANSCRIBE = "transcribe"
    SUBTITLE = "subtitle"
    RELEASE = "release"
    CONVERT = "convert"
    AUDIO = "audio"


class Cost(StrEnum):
    """What a capability costs to use.

    The resolver's default policy excludes :attr:`PAID`: a key present in the
    environment is not consent to spend money.
    """

    FREE = "free"
    NETWORK = "network"
    PAID = "paid"


class Egress(StrEnum):
    """What leaves the machine when a capability runs."""

    NONE = "none"
    TEXT = "text"
    AUDIO = "audio"


class WorkflowId(StrEnum):
    """Named workflows. The resolver picks one; users do not author them."""

    URL_SUBTITLED = "url.subtitled"
    URL_ASR = "url.asr"
    LOCAL_SUBTITLED = "local.subtitled"
    LOCAL_ASR = "local.asr"
    SUBTITLE_ONLY = "subtitle.only"
    AUDIO_ASR = "audio.asr"


class PlanIssue(BaseModel):
    """A blocker or warning found while resolving a plan."""

    model_config = ConfigDict(frozen=True)

    code: str
    severity: Literal["blocker", "warning"] = "blocker"
    stage: Stage | None = None
    capability: str | None = None
    message: str
    fix: str | None = None
    alternatives: tuple[str, ...] = ()
    suggested_workflow: WorkflowId | None = None


class TrackInfo(BaseModel):
    """One subtitle track the source offers (or a supplied file)."""

    model_config = ConfigDict(frozen=True)

    lang: str
    role: Literal["source", "target", "other"] = "other"
    auto_generated: bool = False
    #: True only after the track was actually fetched and parsed, not merely
    #: listed by the platform.
    verified: bool = False
    path: str | None = None


class SourceProbe(BaseModel):
    """Facts about the source, gathered before any heavy work starts."""

    model_config = ConfigDict(frozen=True)

    kind: SourceKind
    source: str
    platform: str | None = None
    title: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    is_vertical: bool | None = None
    has_video: bool = False
    has_audio: bool = False
    source_lang: str | None = None
    tracks: tuple[TrackInfo, ...] = ()
    warnings: tuple[PlanIssue, ...] = ()

    def track(self, role: Literal["source", "target"]) -> TrackInfo | None:
        """The first verified track with ``role``, else the first listed one."""
        matching = [track for track in self.tracks if track.role == role]
        for track in matching:
            if track.verified:
                return track
        return matching[0] if matching else None


class CapabilityDescriptor(BaseModel):
    """A peer-level implementation of one stage."""

    model_config = ConfigDict(frozen=True)

    id: str
    stage: Stage
    available: bool
    verified: bool = False
    requires: tuple[str, ...] = ()
    cost: Cost = Cost.FREE
    egress: Egress = Egress.NONE
    download_mb: int | None = None
    params_schema: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class StagePlan(BaseModel):
    """One stage of a resolved workflow: exactly one capability, no alternatives."""

    model_config = ConfigDict(frozen=True)

    id: str
    stage: Stage
    capability: str
    params: dict[str, Any] = Field(default_factory=dict)
    needs: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    optional: bool = False


class PlanRuntime(BaseModel):
    """Execution settings that are not capability choices."""

    model_config = ConfigDict(frozen=True)

    output_dir: str
    target_lang: str = "zh-Hans"
    force: bool = False
    audio_denoise: bool = True


class WorkflowPlan(BaseModel):
    """The unit an agent approves and the executor obeys."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    plan_id: str = ""
    porter_version: str = ""
    created_at: str = ""
    workflow: WorkflowId
    task: Task
    source: str
    runtime: PlanRuntime
    config_fingerprint: str = ""
    source_probe: SourceProbe
    stages: tuple[StagePlan, ...] = ()
    feasible: bool = True
    issues: tuple[PlanIssue, ...] = ()

    def compute_id(self) -> str:
        """A content hash over the semantic plan.

        ``plan_id`` and ``created_at`` are excluded so two identical plans made
        at different times share an id. Everything else is included: a changed
        capability, parameter, probe fact or config fingerprint changes the id.
        """
        payload = self.model_dump(mode="json", exclude={"plan_id", "created_at"})
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def sealed(self) -> WorkflowPlan:
        """Return a copy carrying its own ``plan_id``."""
        return self.model_copy(update={"plan_id": self.compute_id()})

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping for ``--json`` and ``plan.json``."""
        return self.model_dump(mode="json")

    @property
    def blockers(self) -> tuple[PlanIssue, ...]:
        """The issues that make the plan infeasible."""
        return tuple(issue for issue in self.issues if issue.severity == "blocker")

    def stage(self, stage: Stage) -> StagePlan | None:
        """The first planned stage of ``stage``, or None."""
        for planned in self.stages:
            if planned.stage is stage:
                return planned
        return None


class StageError(BaseModel):
    """A failure at one stage, with the decision material an agent needs."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    retryable: bool = False
    alternatives: tuple[str, ...] = ()
    suggested_workflow: WorkflowId | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class StageResult(BaseModel):
    """What actually happened at one stage."""

    model_config = ConfigDict(frozen=True)

    id: str
    stage: Stage
    capability: str
    status: Literal["done", "reused", "skipped", "failed"]
    params: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    error: StageError | None = None
    duration_seconds: float = 0.0


class WorkflowResult(BaseModel):
    """The terminal result of executing one workflow plan."""

    model_config = ConfigDict(frozen=True)

    job_id: str
    state: JobState
    plan_id: str
    workflow: WorkflowId
    task: Task
    task_dir: str | None = None
    stages: tuple[StageResult, ...] = ()
    artifacts: dict[str, str] = Field(default_factory=dict)
    error: StageError | None = None
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.state is JobState.DONE

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
