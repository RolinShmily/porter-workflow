"""Output helpers for the ``porter`` command-line interface.

This is the *only* package allowed to call :func:`print`. The engine
(``porter.*``) must log to stderr instead, because in an MCP stdio server
stdout carries the JSON-RPC protocol.

Rule of thumb used throughout:

* **stdout** — the command's actual result, so it can be piped or captured.
* **stderr** — progress, warnings, diagnostics, errors.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any

from porter.config import mask_secret
from porter.events import (
    ArtifactReady,
    Event,
    JobState,
    LogRecord,
    PhaseCompleted,
    PhaseFailed,
    PhaseStarted,
    ProgressUpdated,
    StepCompleted,
)

__all__ = [
    "EXIT_CANCELLED",
    "EXIT_ERROR",
    "EXIT_MISUSE",
    "EXIT_OK",
    "banner",
    "emit_json",
    "info",
    "mask_secret",
    "not_implemented",
    "render_event",
    "value",
    "warn",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISUSE = 2
EXIT_CANCELLED = 130


def make_stdout_safe() -> None:
    """Stop a non-UTF-8 stdout from turning output into a traceback.

    Called once at CLI start-up. ``sys.stderr`` already defaults to
    ``errors="backslashreplace"`` (PEP 528), so stderr never raises -- but
    ``sys.stdout`` defaults to ``strict``, and under ``PYTHONUTF8=0`` on a C
    locale that means any CJK byte is fatal:

    .. code-block:: text

        UnicodeEncodeError: 'ascii' codec can't encode character '\u4e2d'

    A video title is remote data and is routinely non-ASCII, so this is not a
    hypothetical. Escaping degrades the display rather than aborting the command
    that produced it, which is the right trade for a diagnostics tool.

    ``reconfigure`` needs a real ``TextIOWrapper``; a test harness replacing
    ``sys.stdout`` with a StringIO does not have one, hence the guard.
    """
    # getattr rather than a direct attribute access: `sys.stdout` is typed as
    # TextIO, and a test harness may have replaced it with a StringIO that has no
    # `reconfigure` at all.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return

    with contextlib.suppress(ValueError, OSError):
        reconfigure(errors="backslashreplace")


_PHASE_LABELS = {
    "prepare": "Preparing raw materials",
    "transcribe": "Transcribing audio",
    "translate": "Translating subtitles",
    "burn": "Burning hardsubs",
}


def value(text: str) -> None:
    """Print a command result to stdout."""
    print(text)


def info(text: str) -> None:
    """Print human-facing commentary to stderr."""
    print(text, file=sys.stderr)


def warn(text: str) -> None:
    """Print a warning to stderr."""
    print(f"warning: {text}", file=sys.stderr)


def banner(title: str, width: int = 65) -> str:
    """Return a centred, rule-boxed heading."""
    rule = "=" * width
    return f"{rule}\n{title.center(width)}\n{rule}"


def emit_json(payload: Any) -> None:
    """Print a JSON document to stdout.

    ``default=str`` keeps ``Path`` and enum values serialisable without forcing
    every caller to pre-convert.

    ``ensure_ascii=True`` is deliberate, and overrides the readability that
    ``ensure_ascii=False`` would give a human reading raw output. It makes the
    document pure ASCII, which matters because stdout is not always UTF-8: under
    ``PYTHONUTF8=0`` on a C locale ``sys.stdout.encoding`` is ASCII and
    ``sys.stdout.errors`` is ``strict``, so a video title containing Chinese
    characters raised ``UnicodeEncodeError`` and killed ``porter inspect``.

    Escaping to a six-character ``uXXXX`` sequence is valid JSON that ``jq``
    decodes transparently, and it is the only form that cannot be corrupted by a
    downstream consumer's encoding.

    This also cannot be fixed by ``errors="backslashreplace"`` alone: for
    characters outside the BMP Python emits an eight-character uppercase escape,
    which is *not* valid JSON, whereas ``json.dumps`` always emits a surrogate
    pair.
    """
    print(json.dumps(payload, indent=2, ensure_ascii=True, default=str))


def not_implemented(command: str) -> int:
    """Report a command that is specified but not yet built.

    A partially built CLI must fail loudly and honestly rather than silently
    doing nothing, and it must not hand the user a design document to go read:
    the message has to be actionable on its own.
    """
    warn(f"'{command}' is not implemented in this build yet")
    info("  see https://github.com/RolinShmily/porter-workflow/issues for status")
    return EXIT_MISUSE


def render_event(event: Event) -> None:
    """Render a pipeline event for a terminal.

    Human output goes to stderr so that ``porter run --json`` can still write a
    clean document to stdout.
    """
    if isinstance(event, PhaseStarted):
        info(f"→ {_PHASE_LABELS.get(event.phase.value, event.phase.value)}")
    elif isinstance(event, PhaseCompleted):
        info(f"  ✓ {_PHASE_LABELS.get(event.phase.value, event.phase.value)} done")
    elif isinstance(event, StepCompleted):
        info(f"    · {event.name}")
    elif isinstance(event, ProgressUpdated):
        message = f" {event.message}" if event.message else ""
        info(f"    {event.percent:5.1f}%{message}")
    elif isinstance(event, ArtifactReady):
        info(f"    + {event.kind.value}: {event.path}")
    elif isinstance(event, PhaseFailed):
        info(f"  ✗ {event.phase.value} failed: {event.error.message}")
    elif isinstance(event, LogRecord) and event.level in ("warning", "error"):
        warn(event.message)


def render_job_state(state: JobState) -> str:
    """Colour-free single-glyph rendering of a job state."""
    return {
        JobState.PENDING: "…",
        JobState.RUNNING: "▶",
        JobState.DONE: "✓",
        JobState.FAILED: "✗",
        JobState.CANCELLED: "⊘",
    }[state]
