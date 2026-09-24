"""porter — automated video localization engine.

This package is the **library** layer. It contains no command-line parsing and
never writes to stdout: two thin frontends consume it.

* ``porter_cli`` — the ``porter`` console script, for humans.
* ``porter_mcp`` — the ``porter-mcp`` MCP server, for AI agents.

Architectural rules (enforced by ``lint-imports`` and a test that scans for
``print`` calls):

1. This package must never import ``porter_cli`` or ``porter_mcp``.
2. Nothing under this package may call ``print()`` — in an MCP stdio server,
   stdout *is* the JSON-RPC channel. Use :func:`porter.logging.get_logger`.
3. Nothing in this package may assume a deployment layout (agent skill
   directories, caller-supplied config paths, ...). Configuration is resolved
   from explicit arguments, environment variables and platform user dirs.

Importing this module is cheap and dependency-free. The heavier symbols
(``JobOptions``, ``Pipeline``, ...) are resolved lazily on first attribute
access via :pep:`562` — so ``import porter; porter.__version__`` needs nothing
but the standard library.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from porter.errors import (
    AsrError,
    CapabilityMissingError,
    ConfigError,
    ExtractionError,
    JobCancelled,
    MediaError,
    PorterError,
    RenderError,
    SubtitleError,
    TranslationError,
    UnsupportedPlatformError,
)
from porter.logging import configure as configure_logging
from porter.logging import get_logger

__version__ = "0.2.4"

#: Public API, resolved lazily. Maps attribute name -> defining module.
_LAZY_EXPORTS: dict[str, str] = {
    # context
    "RunContext": "porter.context",
    # events
    "ArtifactKind": "porter.events",
    "Event": "porter.events",
    "EventSink": "porter.events",
    "JobState": "porter.events",
    "Phase": "porter.events",
    # models
    "BurnMode": "porter.models",
    "BurnResult": "porter.models",
    "JobOptions": "porter.models",
    "JobRequest": "porter.models",
    "JobResult": "porter.models",
    "RawMaterials": "porter.models",
    "SubtitleItem": "porter.models",
    "SubtitleSet": "porter.models",
    "TaskLayout": "porter.models",
    "TranscriptSentence": "porter.models",
    "VideoMetadata": "porter.models",
    # orchestration
    "Pipeline": "porter.pipeline",
}

if TYPE_CHECKING:  # pragma: no cover - import-time only, for type checkers
    from porter.context import RunContext
    from porter.events import ArtifactKind, Event, EventSink, JobState, Phase
    from porter.models import (
        BurnMode,
        BurnResult,
        JobOptions,
        JobRequest,
        JobResult,
        RawMaterials,
        SubtitleItem,
        SubtitleSet,
        TaskLayout,
        TranscriptSentence,
        VideoMetadata,
    )
    from porter.pipeline import Pipeline


def __getattr__(name: str) -> Any:
    """Resolve the lazily-exported public API (PEP 562)."""
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value  # cache so subsequent lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS})


__all__ = [
    "ArtifactKind",
    "AsrError",
    "BurnMode",
    "BurnResult",
    "CapabilityMissingError",
    "ConfigError",
    "Event",
    "EventSink",
    "ExtractionError",
    "JobCancelled",
    "JobOptions",
    "JobRequest",
    "JobResult",
    "JobState",
    "MediaError",
    "Phase",
    "Pipeline",
    "PorterError",
    "RawMaterials",
    "RenderError",
    "RunContext",
    "SubtitleError",
    "SubtitleItem",
    "SubtitleSet",
    "TaskLayout",
    "TranscriptSentence",
    "TranslationError",
    "UnsupportedPlatformError",
    "VideoMetadata",
    "__version__",
    "configure_logging",
    "get_logger",
]
