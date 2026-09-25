"""Job request/result contracts.

These models are the frozen interface between the engine and both frontends.
The MCP frontend derives its tool input schemas from them, so field names here
are effectively a public API: rename only with a migration note.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from pydantic import BaseModel, ConfigDict, Field

from porter.events import ErrorInfo, JobState, Phase
from porter.models.materials import RawMaterials
from porter.models.subtitle import SubtitleSet

__all__ = ["BurnMode", "BurnResult", "JobOptions", "JobRequest", "JobResult"]


class BurnMode(str, Enum):
    """Which release videos to render in Phase BURN.

    Replaces the v0.1 ``skip_burn`` / ``only_bilingual`` / ``only_zh`` flag trio,
    whose combinations included two impossible states.
    """

    DUAL = "dual"  # both video_bilingual.mp4 and video_zh.mp4
    ZH_ONLY = "zh_only"
    BILINGUAL_ONLY = "bilingual_only"
    SKIP = "skip"

    @property
    def wants_zh(self) -> bool:
        return self in (BurnMode.DUAL, BurnMode.ZH_ONLY)

    @property
    def wants_bilingual(self) -> bool:
        return self in (BurnMode.DUAL, BurnMode.BILINGUAL_ONLY)


class JobOptions(BaseModel):
    """Per-invocation options. Never carries secrets — those live in config."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    output_dir: Path = Path("./porter_output")
    burn: BurnMode = BurnMode.DUAL

    #: ``None`` means "walk the full fallback chain".
    asr_engine: str | None = None
    #: ``None`` means "pick the best available translation backend".
    translator: str | None = None
    llm_model: str | None = None
    target_lang: str = "zh-Hans"
    refine: bool = True

    cookies_file: Path | None = None
    cookies_browser: str | None = None

    #: An existing subtitle file to use as the source track, instead of the
    #: platform's track or speech recognition. ``.srt`` and ``.vtt``.
    #:
    #: Added to close a gap deliberately left open: a ``.srt`` beside
    #: a local video is still not picked up automatically, because it could be the
    #: source or the translation and guessing wrong either skips ASR for no reason
    #: or overwrites the user's file. Naming the file removes the ambiguity rather
    #: than resolving it by guesswork -- and it is the way out when a video has no
    #: subtitle track and no ASR engine is available.
    subtitle_file: Path | None = None

    audio_denoise: bool = True

    #: Run exactly one phase and stop. ``None`` runs the whole pipeline.
    only_phase: Phase | None = None
    #: Ignore cached stage results and redo the work.
    force: bool = False


class JobRequest(BaseModel):
    """One unit of work. Exactly one of ``url`` / ``local_video`` must be set."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str | None = None
    local_video: Path | None = None
    options: JobOptions = Field(default_factory=JobOptions)

    def model_post_init(self, _context: object, /) -> None:
        if (self.url is None) == (self.local_video is None):
            raise ValueError("JobRequest requires exactly one of 'url' or 'local_video'")

    @property
    def source(self) -> str:
        """The one input, as text, whichever kind it is.

        Exists because every display and record wants a single string: the job
        registry stores one, ``porter jobs list`` prints one, and a log line
        names one. Deriving it here means none of them has to remember that the
        request can be either kind.
        """
        if self.url is not None:
            return self.url
        return str(self.local_video)

    @classmethod
    def from_source(cls, source: str | Path, options: JobOptions) -> JobRequest:
        """Build a request from one user-supplied string that may be either.

        Both frontends accept a single positional argument, because making the
        user choose between ``--url`` and ``--file`` asks them to classify their
        own input when the program can do it. Lives here rather than in the CLI so
        the MCP server gets the same rule instead of a second, drifting copy.

        The rule, in order:

        1. ``file://`` -- an explicit local file. Converted rather than treated as
           a URL, because no platform extractor claims that scheme and the user
           plainly meant the file.
        2. any other ``scheme://`` -- a URL, handed to the platform registry. Even
           an unsupported scheme, so the caller gets ``UnsupportedPlatformError``
           (which names the supported platforms) rather than a confusing
           "file not found: ftp://...".
        3. anything else -- a local path.

        Existence is *not* checked here. That is
        :meth:`~porter.platforms.local.LocalFileDownloader.prepare`'s job, and
        keeping it there means the error can name the resolved path.
        """
        text = str(source)
        if text.startswith("file://"):
            return cls(local_video=Path(url2pathname(urlparse(text).path)), options=options)
        if "://" in text:
            return cls(url=text, options=options)
        return cls(local_video=Path(text), options=options)


class BurnResult(BaseModel):
    """Phase BURN output. v0.1 called this ``DualReleaseResult``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    video_bilingual: Path | None = None
    video_zh: Path | None = None


class JobResult(BaseModel):
    """Terminal state of a job. Fully JSON-serialisable."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    job_id: str
    state: JobState
    task_dir: Path | None = None
    raw: RawMaterials | None = None
    subtitles: SubtitleSet | None = None
    burn: BurnResult | None = None
    error: ErrorInfo | None = None
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.state is JobState.DONE
