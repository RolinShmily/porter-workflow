"""Base protocol and types for transcript refinement.

Transcript refinement sits after whole-sentence reconstruction and before
translation. It receives full sentences assembled from ASR cues, corrects speech
recognition mis-hearings, restores punctuation and capitalization, and fixes
domain terms/proper nouns, without changing the sentence count or language.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from porter.context import RunContext
from porter.models.subtitle import TranscriptSentence

__all__ = ["TranscriptRefiner"]


@runtime_checkable
class TranscriptRefiner(Protocol):
    """A sentence-level transcript proofreader/refiner."""

    name: str

    def available(self, ctx: RunContext) -> bool:
        """Whether this refiner can run in the current environment."""
        ...

    def refine(
        self,
        sentences: list[TranscriptSentence],
        ctx: RunContext,
    ) -> list[TranscriptSentence]:
        """Proofread and correct ``sentences``, populating ``refined_en_text``."""
        ...
