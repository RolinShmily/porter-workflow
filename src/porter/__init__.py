"""porter — automated video localization engine.

This package is the distribution root and the **stable public API**. The engine
implementation lives in :mod:`porter.core`, and the bundled workbench lives in
:mod:`porter.webui`:

* ``porter.core`` — the engine (workflow planner/executor, pipeline, adapters, job store).
* ``porter.webui`` — the ``porter-workflow`` / ``porter-web`` Gradio workbench.

Architectural rules (enforced by ``lint-imports`` and a test that scans for
``print`` calls):

1. The engine must never import a frontend: ``porter.core`` and this module may
   not import ``porter.webui``.
2. Nothing in the engine may call ``print()`` — keep stdout clean for callers
   embedding the engine or building JSON wrappers. Use
   :func:`porter.core.logging.get_logger`.
3. Nothing in the engine may assume a deployment layout (agent skill
   directories, caller-supplied config paths, ...). Configuration is resolved
   from explicit arguments, environment variables and platform user dirs.

Importing this module is cheap and dependency-free. The heavier symbols
(``JobOptions``, ``Pipeline``, ...) are resolved lazily on first attribute
access via :pep:`562` — so ``import porter; porter.__version__`` needs nothing
but the standard library. Only :mod:`porter.core` is implementation detail;
``from porter import ...`` is the supported library surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from porter.core.errors import (
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
from porter.core.logging import configure as configure_logging
from porter.core.logging import get_logger

__version__ = "0.4.0"

#: Public API, resolved lazily. Maps attribute name -> defining module.
_LAZY_EXPORTS: dict[str, str] = {
    # config
    "PorterConfig": "porter.core.config",
    "resolve": "porter.core.config",
    "save_key": "porter.core.config",
    "writable_config_path": "porter.core.config",
    # context
    "RunContext": "porter.core.context",
    # doctor
    "run_doctor": "porter.core.doctor",
    # events
    "ArtifactKind": "porter.core.events",
    "AwaitingUserAction": "porter.core.events",
    "Event": "porter.core.events",
    "EventSink": "porter.core.events",
    "JobState": "porter.core.events",
    "Phase": "porter.core.events",
    # jobs
    "JobRegistry": "porter.core.jobs",
    "JobStore": "porter.core.jobs",
    # models
    "BurnMode": "porter.core.models",
    "BurnResult": "porter.core.models",
    "JobOptions": "porter.core.models",
    "JobRequest": "porter.core.models",
    "JobResult": "porter.core.models",
    "RawMaterials": "porter.core.models",
    "SubtitleItem": "porter.core.models",
    "SubtitleSet": "porter.core.models",
    "TaskLayout": "porter.core.models",
    "TranscriptSentence": "porter.core.models",
    "VideoMetadata": "porter.core.models",
    # orchestration
    "Pipeline": "porter.core.pipeline",
    # workflow (plan-driven public API)
    "CapabilityDescriptor": "porter.core.workflow.model",
    "ExecutionOutcome": "porter.core.workflow.executor",
    "PlanIssue": "porter.core.workflow.model",
    "PlanOverrides": "porter.core.workflow.resolver",
    "SourceKind": "porter.core.workflow.model",
    "SourceProbe": "porter.core.workflow.model",
    "Stage": "porter.core.workflow.model",
    "StageError": "porter.core.workflow.model",
    "StagePlan": "porter.core.workflow.model",
    "StageResult": "porter.core.workflow.model",
    "Task": "porter.core.workflow.model",
    "WorkflowId": "porter.core.workflow.model",
    "WorkflowPlan": "porter.core.workflow.model",
    "WorkflowResult": "porter.core.workflow.model",
    "capability_catalog": "porter.core.workflow.capabilities",
    "execute_plan": "porter.core.workflow.executor",
    "plan_source": "porter.core.workflow.resolver",
    "probe_source": "porter.core.workflow.probe",
    "resolve_plan": "porter.core.workflow.resolver",
    "run_plan": "porter.core.workflow.executor",
}

if TYPE_CHECKING:  # pragma: no cover - import-time only, for type checkers
    from porter.core.config import PorterConfig, resolve, save_key, writable_config_path
    from porter.core.context import RunContext
    from porter.core.doctor import run_doctor
    from porter.core.events import (
        ArtifactKind,
        AwaitingUserAction,
        Event,
        EventSink,
        JobState,
        Phase,
    )
    from porter.core.jobs import JobRegistry, JobStore
    from porter.core.models import (
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
    from porter.core.pipeline import Pipeline
    from porter.core.workflow.capabilities import capability_catalog
    from porter.core.workflow.executor import ExecutionOutcome, execute_plan, run_plan
    from porter.core.workflow.model import (
        CapabilityDescriptor,
        PlanIssue,
        SourceKind,
        SourceProbe,
        Stage,
        StageError,
        StagePlan,
        StageResult,
        Task,
        WorkflowId,
        WorkflowPlan,
        WorkflowResult,
    )
    from porter.core.workflow.probe import probe_source
    from porter.core.workflow.resolver import PlanOverrides, plan_source, resolve_plan


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
    "AwaitingUserAction",
    "BurnMode",
    "BurnResult",
    "CapabilityDescriptor",
    "CapabilityMissingError",
    "ConfigError",
    "Event",
    "EventSink",
    "ExecutionOutcome",
    "ExtractionError",
    "JobCancelled",
    "JobOptions",
    "JobRegistry",
    "JobRequest",
    "JobResult",
    "JobState",
    "JobStore",
    "MediaError",
    "Phase",
    "Pipeline",
    "PlanIssue",
    "PlanOverrides",
    "PorterConfig",
    "PorterError",
    "RawMaterials",
    "RenderError",
    "RunContext",
    "SourceKind",
    "SourceProbe",
    "Stage",
    "StageError",
    "StagePlan",
    "StageResult",
    "SubtitleError",
    "SubtitleItem",
    "SubtitleSet",
    "Task",
    "TaskLayout",
    "TranscriptSentence",
    "TranslationError",
    "UnsupportedPlatformError",
    "VideoMetadata",
    "WorkflowId",
    "WorkflowPlan",
    "WorkflowResult",
    "__version__",
    "capability_catalog",
    "configure_logging",
    "execute_plan",
    "get_logger",
    "plan_source",
    "probe_source",
    "resolve",
    "resolve_plan",
    "run_doctor",
    "run_plan",
    "save_key",
    "writable_config_path",
]
