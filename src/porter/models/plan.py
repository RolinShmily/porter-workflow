"""The resolved execution plan.

Answers "what would this job actually do, and will it work?" *before* anything
runs. Backs the MCP ``porter_plan`` tool, and exists because of the shape of the
failure the whole product is arranged around: a 1080p job takes tens of minutes,
and the ways it can fail at minute thirty are mostly knowable at second zero.

Ported from nothing -- v0.1 had no equivalent. The plan is derived, never
authored: every field comes from the object that would do the work
(``Pipeline.default``, the platform's own ``plan_subtitles``), so it cannot
describe a pipeline nobody runs.

## Why the route is predicted rather than guessed

The tempting implementation reads ``InspectionResult.has_subtitles`` and calls it
a day. That flag is a *platform listing*, and it is not what decides the route:

* three of the five platforms are marked ``remote=False``, which means no
  subtitle is fetched at all, however many the platform advertises;
* which language is selected depends on a per-platform priority list;
* a platform-provided *Chinese* track removes the translation phase entirely.

Every one of those is real logic that already exists. The plan calls it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = ["BackendPlan", "Plan", "SubtitlePlan", "TranslationPlan"]


class BackendPlan(BaseModel):
    """One engine in a fallback chain, and whether it could actually run.

    Two flags, because one is not enough to answer "will this work":

    ``available`` is the engine's own cheap probe -- a key is set, a binary is on
    PATH. It is advisory in exactly the way the chain treats it: it cannot predict
    a remote 429.

    ``verified`` says whether that engine's wire format has ever been checked
    against the live service. ``bcut`` and ``google_web`` are reverse-engineered
    and answered with empty results on every probe made while building this port,
    so they report ``available: true`` and still cannot transcribe. An agent that
    only read ``available`` would promise a user a job that was going to fail.
    """

    name: str
    available: bool
    verified: bool = False


class SubtitlePlan(BaseModel):
    """How the source transcript will be obtained."""

    #: ``"platform"`` when a platform track will be fetched, ``"asr"`` otherwise.
    route: str
    #: ``{filename in raw/: language tag}`` -- what PREPARE will request. Empty
    #: when the route is ASR.
    tracks: dict[str, str] = Field(default_factory=dict)
    #: Whether speech recognition will run. False when the platform track covers it.
    asr_runs: bool = True
    #: The ASR chain in order, whether or not it will run.
    asr_backends: list[BackendPlan] = Field(default_factory=list)


class TranslationPlan(BaseModel):
    """How the target language will be produced."""

    target_lang: str
    #: False when a platform-provided Chinese track is reused instead, which is
    #: free and exact -- the chain skips every backend in that case.
    needed: bool = True
    backends: list[BackendPlan] = Field(default_factory=list)


class Plan(BaseModel):
    """What one job would do, and what is known to be wrong with it."""

    source: str
    #: ``"url"`` or ``"local"``.
    kind: str
    platform: str | None = None
    #: Phase names in execution order, honouring ``only_phase`` and ``burn``.
    phases: list[str] = Field(default_factory=list)
    #: The component that acquires the source: an extractor name, or ``"local"``.
    acquisition: str

    subtitles: SubtitlePlan
    translation: TranslationPlan
    burn_mode: str
    burn_runs: bool
    renderer: str

    #: Measured facts, for a caller reasoning about cost. No duration is
    #: predicted -- see :mod:`porter.plan`.
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    is_vertical: bool | None = None

    #: False when a phase is certain to fail. The whole point of the call.
    feasible: bool = True
    blocking_issues: list[str] = Field(default_factory=list)
    #: Non-fatal observations worth passing on.
    notes: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping, with the phase list as plain strings."""
        return self.model_dump(mode="json")
