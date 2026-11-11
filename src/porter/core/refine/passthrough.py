"""No-op transcript refiner for when refinement is disabled or unavailable."""

from __future__ import annotations

from porter.core.context import RunContext
from porter.core.models.subtitle import TranscriptSentence

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
