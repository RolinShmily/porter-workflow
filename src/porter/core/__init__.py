"""The porter engine core.

This package contains the implementation. The stable, public API is the
``porter`` package root: ``from porter import Pipeline``. Nothing under
``porter.core`` may import ``porter.cli``, ``porter.mcp`` or ``porter.webui``,
and nothing here may write to stdout — in an MCP stdio server stdout is the
JSON-RPC channel.

Kept deliberately empty (no eager imports) so that importing ``porter``, which
eagerly imports ``porter.core.errors`` and ``porter.core.logging``, cannot
create an import cycle through this module.
"""

from __future__ import annotations

__all__: list[str] = []
