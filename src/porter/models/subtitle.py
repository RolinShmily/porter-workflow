"""Subtitle domain models.

Two representations, deliberately:

``SubtitleItem`` / ``TranscriptSentence``
    Plain :func:`dataclasses.dataclass` types. v0.1 tests construct them
    **positionally** (``SubtitleItem(1, 0, 3000, "text", "")``), which pydantic
    does not support, and a 40-minute video produces thousands of them — so the
    hot path stays allocation-light and behaviour-identical.

``SubtitleSet``
    A pydantic envelope carrying the on-disk artifact paths plus the item list.
    This is what crosses the CLI/MCP boundary and therefore must serialise.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from porter.utils.time import ms_to_ass_time, ms_to_srt_time

__all__ = ["SubtitleItem", "SubtitleSet", "TranscriptSentence"]


@dataclass
class SubtitleItem:
    """A single subtitle cue with timing and bilingual text.

    Field names match v0.1 exactly; do not rename without a migration note.
    """

    index: int
    start_ms: int
    end_ms: int
    source_text: str  # Original language (e.g. English)
    target_text: str  # Translated language (e.g. Chinese)

    @property
    def start_srt(self) -> str:
        return ms_to_srt_time(self.start_ms)

    @property
    def end_srt(self) -> str:
        return ms_to_srt_time(self.end_ms)

    @property
    def start_ass(self) -> str:
        return ms_to_ass_time(self.start_ms)

    @property
    def end_ass(self) -> str:
        return ms_to_ass_time(self.end_ms)

    @property
    def duration_ms(self) -> int:
        return max(self.end_ms - self.start_ms, 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TranscriptSentence:
    """A reconstructed full sentence in the structured transcript ("script book").

    Rebuilt from fragmented ASR cues so translation happens on whole sentences
    rather than on shards, which is what prevents inverted word order in Chinese.
    """

    sentence_id: int
    start_ms: int
    end_ms: int
    en_text: str
    zh_text: str = ""
    fragment_indices: list[int] | None = None

    @property
    def start_srt(self) -> str:
        return ms_to_srt_time(self.start_ms)

    @property
    def end_srt(self) -> str:
        return ms_to_srt_time(self.end_ms)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SubtitleSet(BaseModel):
    """Paths and payload of a completed subtitle stage (the ``cooked/`` output)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    subtitle_bilingual_srt: Path
    subtitle_bilingual_ass: Path
    subtitle_zh_srt: Path
    subtitle_zh_ass: Path
    items: list[SubtitleItem] = Field(default_factory=list)
    #: Measured video geometry, carried from TRANSCRIBE so the translate phase can
    #: write a correct ASS ``PlayResX/Y``. ASS coordinates are absolute pixels, so
    #: a header that disagrees with the video scales every subtitle wrong -- v0.1
    #: hardcoded 1920x1080 and therefore styled vertical videos with horizontal
    #: margins. ``None`` means "not measured", never "assume 16:9"; the consumer
    #: decides the fallback.
    video_width: int | None = None
    video_height: int | None = None
    #: The structured transcript ("script book"). TRANSCRIBE sets the paths
    #: because they follow from the layout; TRANSLATE writes them because their
    #: content is the sentence reconstruction, which happens there. Required
    #: rather than optional for the same reason the four subtitle paths are:
    #: every completed subtitle stage has one, so ``None`` would only ever mean
    #: "a phase forgot to fill it in".
    transcript_json_path: Path
    transcript_txt_path: Path
    sentences: list[TranscriptSentence] = Field(default_factory=list)
    used_asr: bool = False

    @property
    def has_translation(self) -> bool:
        """True when at least one cue carries non-empty target text.

        Note that this is *not* the check that a translation happened: a backend
        echoing its input satisfies it. :func:`porter.subtitles.phrasing.has_chinese_translation`
        is that check.
        """
        return any(item.target_text.strip() for item in self.items)
