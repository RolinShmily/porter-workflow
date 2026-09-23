"""Logging for the porter engine.

Design rules (see ``docs/REFACTOR_PLAN.md`` §2.4 and §8.4):

1. **The engine never writes to stdout.** In an MCP stdio server stdout *is* the
   JSON-RPC channel; a single stray ``print()`` corrupts the protocol. Every
   engine log record therefore goes to **stderr**, and ``ruff`` rule ``T20``
   fails the build if a ``print()`` call appears under ``src/porter``.
2. **No import-time side effects.** :func:`get_logger` never installs handlers;
   it only attaches a :class:`logging.NullHandler` to the ``porter`` root so
   that records stay silent until an embedding application opts in.
3. **Library-friendly.** Frontends call :func:`configure`; libraries should not.
   This mirrors the convention used by requests/urllib3.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import IO

#: Root logger name for the whole engine.
ROOT_LOGGER_NAME = "porter"

#: Environment variable honoured by :func:`configure_default`.
LOG_LEVEL_ENV = "PORTER_LOG_LEVEL"

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
SIMPLE_FORMAT = "%(levelname)-8s %(message)s"

_configured = False


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a namespaced engine logger.

    ``get_logger("asr.bcut")`` resolves to ``porter.asr.bcut``. Passing ``None``
    or ``"porter"`` returns the engine root logger.

    Propagation contract
    --------------------
    The **engine root** (``porter``) is the boundary: it has
    ``propagate = False``, so engine records never reach the host application's
    global ``root`` logger and cannot be duplicated onto stdout by its logging
    configuration. A NullHandler is attached when no handler is installed yet,
    which keeps unconfigured library use silent instead of dumping records
    through Python's ``lastResort`` handler.

    **Child loggers must propagate.** An earlier version of this function set
    ``propagate = False`` on *every* logger, which cut children off from the
    engine root's handler: ``configure()`` appeared to work, but no record ever
    reached it and ``--log-level`` had no effect. See
    ``tests/unit/test_logging.py::test_child_logger_records_reach_the_configured_handler``.
    """
    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.propagate = False
    if not root.handlers:
        root.addHandler(logging.NullHandler())

    if not name or name == ROOT_LOGGER_NAME:
        return root

    full = name if name.startswith(f"{ROOT_LOGGER_NAME}.") else f"{ROOT_LOGGER_NAME}.{name}"
    logger = logging.getLogger(full)

    # Explicitly restore propagation: a third party (or an older porter build)
    # may have disabled it on a logger we reuse by name.
    logger.propagate = True
    # Only configure() may own a handler. A NullHandler left on a child would
    # short-circuit propagation via logging's "found a handler" check.
    for handler in list(logger.handlers):
        if isinstance(handler, logging.NullHandler):
            logger.removeHandler(handler)

    return logger


def resolve_level(level: str | int | None = None) -> int:
    """Resolve a level name/int, falling back to ``PORTER_LOG_LEVEL`` then INFO."""
    if level is None:
        level = os.environ.get(LOG_LEVEL_ENV)
    if level is None:
        return logging.INFO
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(level.strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def configure(
    level: str | int | None = None,
    *,
    stream: IO[str] | None = None,
    fmt: str = DEFAULT_FORMAT,
    force: bool = False,
) -> logging.Logger:
    """Install the engine's stderr handler. Safe to call more than once.

    Args:
        level: Level name (``"debug"``) or numeric level. Defaults to
            ``PORTER_LOG_LEVEL`` or ``INFO``.
        stream: Destination stream. Defaults to ``sys.stderr``. The default is
            deliberate — pass a different stream only in tests.
        fmt: Log format string.
        force: Replace an existing handler instead of leaving it in place.

    Returns:
        The ``porter`` root logger.
    """
    global _configured

    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.propagate = False

    if _configured and not force:
        root.setLevel(resolve_level(level))
        return root

    for handler in list(root.handlers):
        root.removeHandler(handler)
        if not isinstance(handler, logging.NullHandler):
            handler.close()

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(resolve_level(level))

    _configured = True
    return root


def reset() -> None:
    """Remove all handlers, returning the engine loggers to a silent state.

    Intended for tests that need to assert on captured records.
    """
    global _configured
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if not isinstance(handler, logging.NullHandler):
            handler.close()
    _configured = False


__all__ = [
    "DEFAULT_FORMAT",
    "LOG_LEVEL_ENV",
    "ROOT_LOGGER_NAME",
    "SIMPLE_FORMAT",
    "configure",
    "get_logger",
    "reset",
    "resolve_level",
]
