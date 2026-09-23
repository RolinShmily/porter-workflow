"""Standardised video metadata shared by every platform extractor."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["VideoMetadata"]


class VideoMetadata(BaseModel):
    """Platform-agnostic metadata describing a downloaded video.

    Field names match v0.1 ``extractors/base.py:VideoMetadata`` so existing
    ``metadata.json`` files stay readable.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    safe_title: str
    url: str
    platform: str = "youtube"
    uploader: str | None = None
    channel: str | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    is_vertical: bool = False
    description: str | None = None
    thumbnail_url: str | None = None
    has_official_subtitle: bool = False
    official_subtitle_lang: str | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def aspect_ratio(self) -> float | None:
        """Width divided by height, or ``None`` when dimensions are unknown."""
        if not self.width or not self.height:
            return None
        return self.width / self.height

    def to_dict(self) -> dict[str, Any]:
        """v0.1-compatible alias for :meth:`model_dump`."""
        return self.model_dump(mode="json")
