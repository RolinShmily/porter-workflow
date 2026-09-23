"""``porter_cli`` — the ``porter`` command-line frontend.

Contains argument parsing, terminal rendering, and exit-code mapping. It holds
**no business logic**: everything is delegated to the ``porter`` engine so the
MCP frontend can offer identical behaviour.

Import direction is one-way: ``porter_cli`` may import ``porter``; ``porter``
must never import ``porter_cli`` (enforced by ``lint-imports``).
"""

from porter_cli.app import build_parser, dispatch, main

__all__ = ["build_parser", "dispatch", "main"]
