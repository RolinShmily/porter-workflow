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
from functools import cache
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
    "symbol",
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


def _stream_encoding(stream: Any) -> str | None:
    """The codec ``stream`` writes with, or ``None`` when it is not a text stream."""
    return getattr(stream, "encoding", None)


@cache
def _can_encode(text: str, encoding: str | None) -> bool:
    if not encoding:
        return True
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def symbol(preferred: str, fallback: str) -> str:
    """Return ``preferred`` when the output streams can encode it, else ``fallback``.

    A Windows console is frequently GBK or cp1252, and neither can represent
    ``✓`` or ``✗``. :func:`make_stdout_safe` sets ``errors="backslashreplace"``
    so that cannot crash the command, but the user would then read a literal
    ``\\u2713`` -- which is strictly worse than a plain mark, since the mark's
    only job is to be legible.

    Both streams are checked because a glyph's destination depends on the call
    site (results go to stdout, progress to stderr); showing the pretty form on
    one and the fallback on the other would be worse than being uniformly plain.
    """
    encodings = (_stream_encoding(sys.stdout), _stream_encoding(sys.stderr))
    if all(_can_encode(preferred, encoding) for encoding in encodings):
        return preferred
    return fallback


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
        label = _PHASE_LABELS.get(event.phase.value, event.phase.value)
        info(f"{symbol('→', '>')} {label}")
    elif isinstance(event, PhaseCompleted):
        label = _PHASE_LABELS.get(event.phase.value, event.phase.value)
        info(f"  {symbol('✓', 'ok')} {label} done")
    elif isinstance(event, StepCompleted):
        info(f"    {symbol('·', '-')} {event.name}")
    elif isinstance(event, ProgressUpdated):
        message = f" {event.message}" if event.message else ""
        info(f"    {event.percent:5.1f}%{message}")
    elif isinstance(event, ArtifactReady):
        info(f"    + {event.kind.value}: {event.path}")
    elif isinstance(event, PhaseFailed):
        info(f"  {symbol('✗', 'x')} {event.phase.value} failed: {event.error.message}")
    elif isinstance(event, LogRecord) and event.level in ("warning", "error"):
        warn(event.message)


#: ``(preferred, fallback)`` per state. The fallback is a single ASCII glyph so
#: the ``porter jobs`` table keeps its column width on a non-UTF-8 console.
_JOB_STATE_GLYPHS: dict[JobState, tuple[str, str]] = {
    JobState.PENDING: ("…", "."),
    JobState.RUNNING: ("▶", ">"),
    JobState.DONE: ("✓", "+"),
    JobState.FAILED: ("✗", "x"),
    JobState.CANCELLED: ("⊘", "-"),
}


def render_job_state(state: JobState) -> str:
    """Colour-free single-glyph rendering of a job state."""
    preferred, fallback = _JOB_STATE_GLYPHS[state]
    return symbol(preferred, fallback)
