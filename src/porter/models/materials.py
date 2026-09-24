"""Task layout and Phase PREPARE output.

``TaskLayout`` exists to kill a specific v0.1 duplication: all five extractors
re-derived ``task_dir = output_base_dir / f"{video_id}_{safe_title}"`` and then
created ``raw/``, ``cooked/`` and ``.tmp/`` by hand. Path derivation now lives
here, in one place, and the on-disk contract is unchanged.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from porter.logging import get_logger
from porter.models.metadata import VideoMetadata
from porter.utils.text import sanitize_filename

__all__ = ["RawMaterials", "TaskLayout"]

_logger = get_logger(__name__)

RAW_DIRNAME = "raw"
COOKED_DIRNAME = "cooked"
TMP_DIRNAME = ".tmp"


class TaskLayout(BaseModel):
    """Filesystem layout of one localization task.

    The public contract is::

        <output_root>/<video_id>_<safe_title>/
            raw/      standardised master assets
            cooked/   subtitles and burned release videos
            .tmp/     scratch, safe to delete between runs
    """

    model_config = ConfigDict(frozen=True)

    task_dir: Path
    video_id: str
    safe_title: str

    @classmethod
    def build(cls, output_root: Path, video_id: str, title: str) -> TaskLayout:
        """Derive the layout for a task without touching the filesystem."""
        safe_title = sanitize_filename(title)
        return cls(
            task_dir=Path(output_root) / f"{video_id}_{safe_title}",
            video_id=video_id,
            safe_title=safe_title,
        )

    @property
    def raw_dir(self) -> Path:
        return self.task_dir / RAW_DIRNAME

    @property
    def cooked_dir(self) -> Path:
        return self.task_dir / COOKED_DIRNAME

    @property
    def tmp_dir(self) -> Path:
        return self.task_dir / TMP_DIRNAME

    def ensure_dirs(self) -> TaskLayout:
        """Create ``raw/``, ``cooked/`` and ``.tmp/``. Idempotent."""
        for directory in (self.raw_dir, self.cooked_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def write_metadata(self, metadata: VideoMetadata) -> Path:
        """Write ``raw/metadata.json`` and return its path.

        Lives on the layout because the layout is what knows where the file
        goes; both PREPARE producers used to carry their own copy of this.
        ``ensure_ascii=False`` keeps a Chinese title readable in the file, and
        the JSON is written whole rather than streamed, so a crash cannot leave
        a half-written ``metadata.json`` that parses as valid.
        """
        path = self.raw_dir / "metadata.json"
        path.write_text(
            json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def cleanup_tmp(self) -> None:
        """Remove ``.tmp``. Failure is logged, never fatal.

        Scratch space is safe to delete between runs, and a leftover file is not
        worth failing a job that has already produced its real artifacts.
        """
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except OSError as exc:  # pragma: no cover - rmtree(ignore_errors=True)
            _logger.warning("could not remove %s: %s", self.tmp_dir, exc)


class RawMaterials(BaseModel):
    """Phase PREPARE output: everything under ``raw/``.

    v0.1 called this ``RawMaterialResult``; the fields are the same, minus the
    redundant ``task_dir``/``raw_dir`` which now come from :class:`TaskLayout`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    layout: TaskLayout
    video: Path
    audio: Path
    audio_enhanced: Path | None = None
    cover: Path | None = None
    subtitle_src: Path | None = None
    subtitle_zh: Path | None = None
    metadata_path: Path | None = None
    info: VideoMetadata | None = None

    @property
    def task_dir(self) -> Path:
        return self.layout.task_dir

    @property
    def raw_dir(self) -> Path:
        return self.layout.raw_dir
