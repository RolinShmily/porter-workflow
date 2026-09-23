"""Bridge pipeline events onto MCP progress notifications.

The engine emits frontend-agnostic :class:`~porter.events.Event` objects. This
module translates them into the shape an MCP client understands: a monotonic
``percent`` plus a short human-readable message.

MCP requires progress to be reported against a ``progressToken`` supplied by the
client, and to advance monotonically, so each phase is mapped onto its own slice
of the overall 0-100 range:

===========================  =============
phase                        share of 100
===========================  =============
``PREPARE``                       50
``TRANSCRIBE``                    25
``TRANSLATE``                     15
``BURN``                          10
===========================  =============
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from porter.events import ArtifactReady, Event, Phase, PhaseCompleted, PhaseStarted, ProgressUpdated

__all__ = ["ProgressBridge", "phase_weight"]

#: Relative cost of each phase. Weights, not percentages — they are normalised.
_PHASE_WEIGHTS: dict[Phase, float] = {
    Phase.PREPARE: 50.0,
    Phase.TRANSCRIBE: 25.0,
    Phase.TRANSLATE: 15.0,
    Phase.BURN: 10.0,
}

#: Called with (overall_percent, message). Supplied by the MCP tool.
ProgressCallback = Callable[[float, str], Awaitable[None]]

_PHASE_LABEL = {
    Phase.PREPARE: "preparing raw materials",
    Phase.TRANSCRIBE: "transcribing audio",
    Phase.TRANSLATE: "translating subtitles",
    Phase.BURN: "burning hardsubs",
}


def phase_weight(phase: Phase) -> float:
    """Relative cost of ``phase``; unknown phases get a weight of 1."""
    return _PHASE_WEIGHTS.get(phase, 1.0)


@dataclass
class ProgressBridge:
    """Convert an :class:`~porter.events.Event` stream into monotonic progress.

    ``report`` is optional: when the client supplies no progress token, pass
    ``None`` and the bridge becomes a no-op that still tracks state (so
    ``porter_job_status`` can answer).
    """

    report: ProgressCallback | None = None
    _completed: float = field(default=0.0, init=False)
    _total: float = field(init=False)
    _last_percent: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._total = sum(_PHASE_WEIGHTS.values())

    async def __call__(self, event: Event) -> None:
        """Handle one event. Never raises."""
        if isinstance(event, PhaseStarted):
            await self._maybe_report(
                self._completed,
                f"{_PHASE_LABEL.get(event.phase, event.phase.value)}…",
            )
        elif isinstance(event, ProgressUpdated):
            base = self._completed
            share = phase_weight(event.phase)
            percent = base + share * (event.percent / 100.0)
            await self._maybe_report(
                percent,
                event.message or _PHASE_LABEL.get(event.phase, event.phase.value),
            )
        elif isinstance(event, PhaseCompleted):
            self._completed += phase_weight(event.phase)
            await self._maybe_report(
                self._completed,
                f"{_PHASE_LABEL.get(event.phase, event.phase.value)}: done",
            )
        elif isinstance(event, ArtifactReady):
            await self._maybe_report(self._completed, f"wrote {event.path.name}")

    async def _maybe_report(self, percent: float, message: str) -> None:
        if self.report is None:
            return
        # MCP requires monotonic progress; never go backwards on a retry.
        percent = max(percent, self._last_percent)
        percent = min(max(percent, 0.0), self._total)
        self._last_percent = percent
        try:
            await self.report(percent / self._total * 100.0, message)
        except Exception:  # noqa: BLE001 - a dead client must not fail the job
            self.report = None
