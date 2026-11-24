"""``porter-mcp`` — the Model Context Protocol frontend.

A thin adapter over the ``porter`` engine. It owns three things the engine
deliberately does not:

1. **Protocol framing** — stdio transport and tool schemas. Progress
   notifications are *not* sent: jobs run on background threads and are polled
   (see :mod:`porter.mcp.tools.jobs`), and the blocking stage tools are short
   enough that a token would buy nothing.
2. **Stdout hygiene** — see :mod:`porter.mcp.stdout_guard`. In an MCP stdio
   server stdout is the JSON-RPC channel, so *nothing* may print. All engine
   logging goes to stderr.
3. **Job orchestration** — long work is exposed as start/status/result/cancel
   rather than one blocking call, because MCP clients time out long before a
   1080p encode finishes.
4. **Graceful shutdown** — see :mod:`porter.mcp.shutdown`. A signal asks the
   running jobs to stop rather than killing them mid-write.

Every `@server.tool` body must be decorated with
:func:`~porter.mcp.stdout_guard.protect`.
"""

from __future__ import annotations

import sys
from typing import Any

from porter.core.logging import configure as configure_logging

__all__ = ["create_server", "main"]

SERVER_NAME = "porter"


def create_server() -> Any:
    """Build the FastMCP server with every implemented tool registered.

    Imported lazily so that ``import porter.mcp`` (which the package's own
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

    from porter.mcp.tools import register_all

    server = FastMCP(
        name=SERVER_NAME,
        instructions=(
            "Automated video localization with an explicit plan. Typical flow: "
            "porter_doctor for machine health, porter_capabilities for the stage "
            "inventory, porter_probe for source facts, porter_plan for the "
            "executable WorkflowPlan, then confirm with the user and call "
            "porter_job_start(plan=...) with that plan unchanged. Poll "
            "porter_job_status until 'done', then porter_job_result. A plan names "
            "exactly one capability per stage; there is no runtime fallback, so a "
            "failed stage is replanned rather than retried on another engine."
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

    from porter.mcp.stdout_guard import protect  # noqa: F401  (re-export contract)

    server = create_server()

    # Before the transport starts. A signal that arrives while a job is running
    # must ask the job to stop, not kill it: the pipeline notices a flag between
    # steps, and a job that unwinds records its own outcome. See
    # porter.mcp.shutdown for what this can and cannot interrupt.
    from porter.mcp.shutdown import install as install_shutdown_handlers

    install_shutdown_handlers()

    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
