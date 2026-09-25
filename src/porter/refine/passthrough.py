"""No-op transcript refiner for when refinement is disabled or unavailable."""

from __future__ import annotations

from porter.context import RunContext
from porter.models.subtitle import TranscriptSentence

__all__ = ["PassthroughRefiner"]


class PassthroughRefiner:
    """Leaves transcript sentences unchanged."""

    name = "passthrough"

    def available(self, ctx: RunContext) -> bool:
        """Always available unconditionally."""
        return True

    def refine(
        self,
        sentences: list[TranscriptSentence],
        ctx: RunContext,
    ) -> list[TranscriptSentence]:
        """Return the sentences unmodified."""
        return sentences
