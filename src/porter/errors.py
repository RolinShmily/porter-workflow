"""Exception hierarchy for the porter engine.

Every error raised by the engine derives from :class:`PorterError` so that the
CLI and the MCP frontend can map failures to a stable, serialisable shape
(``ErrorInfo``) without inspecting tracebacks.

The engine must never raise bare ``Exception`` from a public entry point.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class PorterError(Exception):
    """Base class for every error raised by the porter engine."""

    #: Process exit code the CLI uses when this error escapes to the top level.
    exit_code: int = 1
    #: Stable machine-readable identifier, surfaced through ``ErrorInfo.code``.
    code: str = "porter_error"

    def __init__(self, message: str, /, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def __str__(self) -> str:  # pragma: no cover - trivial
        if not self.details:
            return self.message
        rendered = ", ".join(f"{k}={v!r}" for k, v in self.details.items())
        return f"{self.message} ({rendered})"


class ConfigError(PorterError):
    """Configuration is missing, malformed, or self-contradictory."""

    code = "config_error"


class CapabilityMissingError(PorterError):
    """A required external capability (ffmpeg, libass, deno, ...) is absent.

    ``capability`` names the missing item so callers can offer remediation
    without parsing the message. See ``porter.doctor``.
    """

    code = "capability_missing"

    def __init__(self, capability: str, message: str, /, **details: Any) -> None:
        super().__init__(message, capability=capability, **details)
        self.capability = capability


class UnsupportedPlatformError(PorterError):
    """No registered platform extractor claims the given URL.

    Carries the list of platforms that *are* registered, so the CLI and the MCP
    frontend can tell the user what would have worked instead of only what
    failed.
    """

    code = "unsupported_platform"

    def __init__(self, url: str, *, supported: Sequence[str] = ()) -> None:
        listing = ", ".join(supported) if supported else "none registered"
        super().__init__(
            f"no platform extractor recognises this URL: {url}\n"
            f"  supported platforms: {listing}",
            url=url,
            supported=list(supported),
        )
        self.url = url
        self.supported: tuple[str, ...] = tuple(supported)


class ExtractionError(PorterError):
    """Phase PREPARE failed: metadata probe, media download, or remux."""

    code = "extraction_error"


class SubtitleError(PorterError):
    """Subtitle parsing, phrasing, or serialisation failed."""

    code = "subtitle_error"


class AsrError(PorterError):
    """Every ASR backend in the fallback chain failed."""

    code = "asr_error"


class TranslationError(PorterError):
    """Every translation backend in the fallback chain failed."""

    code = "translation_error"


class MediaError(PorterError):
    """An ffmpeg/ffprobe invocation failed."""

    code = "media_error"


class RenderError(PorterError):
    """Hardsub burning or release assembly failed."""

    code = "render_error"


class JobCancelled(PorterError):
    """The caller cancelled the job via :class:`porter.context.RunContext`.

    Not a failure: the CLI maps it to exit code 130 and the MCP frontend maps
    it to ``JobState.CANCELLED``. The name deliberately avoids an ``Error``
    suffix because this is a control-flow signal, not a fault.
    """

    code = "cancelled"
    exit_code = 130


__all__ = [
    "AsrError",
    "CapabilityMissingError",
    "ConfigError",
    "ExtractionError",
    "JobCancelled",
    "MediaError",
    "PorterError",
    "RenderError",
    "SubtitleError",
    "TranslationError",
    "UnsupportedPlatformError",
]
