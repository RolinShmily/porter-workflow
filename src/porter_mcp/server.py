"""``porter-mcp`` — the Model Context Protocol frontend.

A thin adapter over the ``porter`` engine. It owns three things the engine
deliberately does not:

1. **Protocol framing** — stdio transport, tool schemas, progress notifications.
2. **Stdout hygiene** — see :mod:`porter_mcp.stdout_guard`. In an MCP stdio
   server stdout is the JSON-RPC channel, so *nothing* may print. All engine
   logging goes to stderr.
3. **Job orchestration** — long work is exposed as start/status/result/cancel
   rather than one blocking call, because MCP clients time out long before a
   1080p encode finishes.

Every `@server.tool` body must be decorated with
:func:`~porter_mcp.stdout_guard.protect`.
"""

from __future__ import annotations

import sys
from typing import Any

from porter.logging import configure as configure_logging

__all__ = ["create_server", "main"]

SERVER_NAME = "porter"


def create_server() -> Any:
    """Build the FastMCP server with every implemented tool registered.

    Imported lazily so that ``import porter_mcp`` (which the package's own
    ``__init__`` allows without the ``[mcp]`` extra) stays cheap.
    """
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ImportError(
            "porter-mcp requires the MCP extra: "
            "pip install 'porter-workflow[mcp]' "
            "(or run it with: uvx --from 'porter-workflow[mcp]' porter-mcp)"
        ) from exc

    from porter_mcp.tools import register_all

    server = FastMCP(
        name=SERVER_NAME,
        instructions=(
            "Automated video localization. Typical flow: porter_inspect to check a "
            "link, then porter_job_start to run the pipeline, then poll "
            "porter_job_status until the state is 'done'. Prefer the job tools over "
            "blocking calls — encoding can take tens of minutes."
        ),
    )
    register_all(server)
    return server


def main() -> int:
    """Entry point for the ``porter-mcp`` console script.

    Runs the server over stdio. Logging is redirected to stderr before anything
    else, because a single line on stdout corrupts the protocol.
    """
    configure_logging()

    from porter_mcp.stdout_guard import protect  # noqa: F401  (re-export contract)

    server = create_server()
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
