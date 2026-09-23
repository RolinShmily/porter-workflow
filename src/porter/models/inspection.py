"""Pre-flight inspection result.

Answers "is this link usable, and what will happen to it?" without downloading.
Backs ``porter inspect`` and the MCP ``porter_inspect`` tool, both of which exist
so a human or an agent can decide whether to commit to a long job.

Ported from ``v0.1`` with the fields unchanged, because the CLI renders them and
the MCP schema exposes them: the names are a public contract.

One deliberate difference: :meth:`InspectionResult.format_summary` **returns** a
string instead of printing it. v0.1 assembled the report with four ``print()``
calls, which would corrupt the MCP stdout channel; the rendering itself is kept
here rather than moved to the frontend so the ``v0.1`` assertions that read
``format_summary()`` still hold — see ``tests/regression/test_inspector_port.py``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = ["InspectionResult"]

#: ``raw_info`` is a full yt-dlp info dict — megabytes for a video with hundreds
#: of formats. Only this subset reaches ``to_dict()``, so a JSON round-trip stays
#: small enough to hand to an agent.
_RAW_INFO_KEYS = (
    "id",
    "extractor",
    "extractor_key",
    "webpage_url",
    "upload_date",
    "live_status",
    "availability",
    "language",
)


class InspectionResult(BaseModel):
    """Structured pre-flight result for one media link."""

    input_url: str
    canonical_url: str
    platform: str
    is_valid: bool
    has_video: bool

    video_id: str | None = None
    title: str | None = None
    safe_title: str | None = None
    uploader: str | None = None
    channel: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    is_vertical: bool = False
    has_subtitles: bool = False
    thumbnail_url: str | None = None
    error_message: str | None = None
    raw_info: dict[str, Any] = Field(default_factory=dict)

    # -- derived -----------------------------------------------------------

    @property
    def aspect_label(self) -> str | None:
        """``"Vertical 9:16"`` / ``"Horizontal 16:9"``, or None when unmeasured."""
        if not (self.width and self.height):
            return None
        return "Vertical 9:16" if self.is_vertical else "Horizontal 16:9"

    @property
    def duration_label(self) -> str:
        """``"MM:SS (Ns)"``, or ``"Unknown"``."""
        if not self.duration_seconds:
            return "Unknown"
        total = int(self.duration_seconds)
        return f"{total // 60:02d}:{total % 60:02d} ({self.duration_seconds:.0f}s)"

    # -- rendering ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping, with ``raw_info`` reduced to a small subset."""
        data = self.model_dump(mode="json", exclude={"raw_info"})
        data["raw_info"] = {k: self.raw_info[k] for k in _RAW_INFO_KEYS if k in self.raw_info}
        return data

    def format_summary(self) -> str:
        """Human-readable report.

        Returns the text; the caller decides the stream. Nothing here prints.
        """
        if not self.is_valid:
            return (
                "Pre-flight check failed\n"
                f"  URL:   {self.input_url}\n"
                f"  Error: {self.error_message or 'Unknown error'}"
            )

        lines = [
            f"Platform:      {self.platform.upper()}",
            f"Canonical URL: {self.canonical_url}",
            f"Video ID:      {self.video_id or 'N/A'}",
            f"Title:         {self.title or 'N/A'}",
            f"Author:        {self.uploader or self.channel or 'N/A'}",
            f"Duration:      {self.duration_label}",
            f"Resolution:    {self._resolution_label()}",
            f"Subtitles:     {'Available' if self.has_subtitles else 'None (ASR will be used)'}",
        ]
        return "\n".join(lines)

    def _resolution_label(self) -> str:
        size = f"{self.width}x{self.height}" if self.width and self.height else None
        if size is None:
            return "Unknown"
        return f"{size} ({self.aspect_label})"
