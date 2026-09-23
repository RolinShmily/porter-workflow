"""Stdout protection for the MCP stdio transport.

**Why this module exists.** The MCP stdio transport uses stdout for JSON-RPC
framing. A single stray ``print()`` — in porter, in a dependency, or in some
transitively imported library — writes non-protocol bytes into the stream and
the client drops the connection with a parse error. The failure is far from the
cause, which makes it expensive to debug.

The engine already forbids ``print`` (ruff rule ``T20``), but that only covers
our own source. This guard catches everything else at the boundary:

* ``strict=False`` (production) — the write is redirected to stderr and recorded,
  and the tool still succeeds. The server survives; the bug stays visible.
* ``strict=True`` (tests) — the write raises immediately, so CI fails at the
  exact line rather than letting the defect ship.

Only tool bodies are guarded, never the server loop: FastMCP must keep direct
access to the real stdout in order to write protocol frames.
"""

from __future__ import annotations

import io
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from functools import wraps
from typing import Any, TextIO, TypeVar

__all__ = ["StdoutViolation", "guard_stdout", "protect", "violations"]

_F = TypeVar("_F", bound=Callable[..., Any])

_local = threading.local()


class StdoutViolation(RuntimeError):
    """Raised in strict mode when code writes to stdout inside a guarded region."""

    def __init__(self, payload: str) -> None:
        super().__init__(
            "porter-mcp: something wrote to stdout inside a tool call. "
            "stdout carries the JSON-RPC protocol and must stay clean; use "
            f"logging (stderr) instead. Offending output: {payload[:200]!r}"
        )
        self.payload = payload


class _GuardedStdout(io.TextIOBase):
    """A stdout replacement that diverts writes to **stderr** and records them.

    The divert target must be stderr, never the stream being replaced: writing
    back to stdout would defeat the guard entirely and corrupt the JSON-RPC
    stream in production.
    """

    def __init__(self, divert_to: TextIO, sink: list[str], *, strict: bool) -> None:
        self._divert_to = divert_to
        self._sink = sink
        self._strict = strict

    def write(self, payload: str) -> int:
        if self._strict:
            raise StdoutViolation(payload)
        self._sink.append(payload)
        # Keep the diagnostic visible without corrupting the protocol stream.
        written: int = self._divert_to.write(payload)
        return written

    def flush(self) -> None:
        self._divert_to.flush()

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._divert_to, "encoding", "utf-8")


def _sink() -> list[str]:
    """Per-thread record of everything diverted during guarded regions."""
    if not hasattr(_local, "sink"):
        _local.sink = []
    return _local.sink  # type: ignore[no-any-return]


def violations() -> list[str]:
    """Return (and clear) the payloads diverted during the last guarded region.

    The MCP frontend calls this after each tool invocation and logs a warning if
    anything was diverted, so a leaking dependency shows up in the server log
    instead of silently corrupting a response.
    """
    captured = list(_sink())
    _sink().clear()
    return captured


@contextmanager
def guard_stdout(*, strict: bool = False) -> Generator[list[str], None, None]:
    """Divert writes to ``sys.stdout`` onto stderr for the duration.

    The per-thread record is cleared on entry, so the log always describes
    *the most recent* guarded region. That matters for a long-lived MCP server:
    a tool that leaks must not inherit the previous tool's record and report a
    false positive. Nested guards are not supported (the inner one wins).

    Args:
        strict: Raise :class:`StdoutViolation` on the first write instead of
            diverting. Use in tests.

    Yields:
        The list that collects the diverted payloads.
    """
    sink = _sink()
    sink.clear()
    previous_stdout = sys.stdout
    sys.stdout = _GuardedStdout(sys.stderr, sink, strict=strict)
    try:
        yield sink
    finally:
        sys.stdout = previous_stdout


def protect(func: _F) -> _F:
    """Decorator: run ``func`` inside :func:`guard_stdout`.

    Applied to every MCP tool body. Works for both sync and async callables.
    """

    if _is_async(func):

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with guard_stdout():
                return await func(*args, **kwargs)

        return async_wrapper  # type: ignore[return-value]

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with guard_stdout():
            return func(*args, **kwargs)

    return sync_wrapper  # type: ignore[return-value]


def _is_async(func: Callable[..., Any]) -> bool:
    import inspect

    return inspect.iscoroutinefunction(func)
