"""``porter_mcp`` — the Model Context Protocol frontend.

Exposes the ``porter`` engine as MCP tools so AI agents can drive the
localization pipeline directly.

Design notes that shape this package:

* **The engine is reused, never reimplemented.** Every tool body is a thin
  adapter; all logic lives in ``porter``.
* **Nothing may print.** In an MCP stdio server stdout carries the JSON-RPC
  protocol, so the whole package logs to stderr and every tool body runs inside
  :func:`porter_mcp.stdout_guard.protect`.
* **Long work is job-based.** Encoding a 1080p video takes tens of minutes,
  which exceeds MCP tool-call timeouts; the server therefore exposes
  start/status/result/cancel rather than one blocking call.

This module deliberately imports nothing heavy, so ``import porter_mcp`` works
even when the ``[mcp]`` extra is absent. ``fastmcp`` is imported lazily by
:func:`porter_mcp.server.create_server`.
"""

__all__: list[str] = []
