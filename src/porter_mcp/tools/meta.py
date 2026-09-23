"""Tool: implementation metadata.

Always available, and deliberately trivial: it gives MCP clients (and the
protocol inspector) something to call that proves the server is wired up
correctly, without touching the network or the filesystem.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from porter import __version__
from porter_mcp.stdout_guard import protect

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]


def register(server: FastMCP) -> None:
    """Attach the metadata tool to ``server``."""

    @server.tool(
        name="porter_version",
        description=(
            "Report the porter-workflow engine version and runtime details. "
            "Use to confirm which implementation is serving this session."
        ),
    )
    @protect
    def porter_version() -> dict[str, Any]:
        """Return engine version and Python runtime information."""
        return {
            "name": "porter-workflow",
            "version": __version__,
            "python": sys.version.split()[0],
            "implementation": sys.implementation.name,
        }
