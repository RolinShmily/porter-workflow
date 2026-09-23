"""Tool: read-only configuration.

§8.5 allows ``porter_config`` exactly two actions, both reads, and requires every
secret to be masked. It is the only tool that exists purely so an agent can *see*
what it is working with: which LLM model, which output directory, whether an API
key is configured at all. Without it an agent diagnosing "why did translation use
the key-free backend" has to guess.

Two things this tool deliberately cannot do:

``set``
    Writing an API key over MCP would put the credential in the conversation
    transcript and in whatever telemetry the client keeps. The CLI is the only
    writer, which is also why it can read keys from the environment.
``unmasked``
    Masking is :meth:`porter.config.PorterConfig.masked`, in the engine, not
    reimplemented here. A second masking routine is a second place to get it
    wrong -- and the one that gets it wrong is the one that leaks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from porter.config import config_file, find_project_config, resolve
from porter.errors import PorterError
from porter_mcp.stdout_guard import protect

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]

#: The sections ``get`` can be narrowed to. Kept explicit so a typo is an error
#: rather than an empty dict that reads like "this section is empty".
SECTIONS = ("llm", "asr", "ffmpeg", "style")


def register(server: FastMCP) -> None:
    """Attach the configuration tool to ``server``."""

    @server.tool(
        name="porter_config",
        description=(
            "Read the resolved porter configuration. action='list' returns the "
            "available sections and which config files were used; action='get' "
            "returns the values. Secrets (API keys) are masked and can never be "
            "read or written through this tool -- configure them with the porter "
            "CLI instead."
        ),
    )
    @protect
    def porter_config(action: str = "list", section: str | None = None) -> dict[str, Any]:
        try:
            config = resolve(None)
        except PorterError as exc:
            return {"ok": False, "error": f"configuration could not be resolved: {exc}"}

        if action == "list":
            return {
                "ok": True,
                "action": "list",
                "sections": list(SECTIONS),
                "source": config.source,
                "user_config_file": str(config_file()),
                "project_config_file": (
                    str(path) if (path := find_project_config()) is not None else None
                ),
                "output_dir": str(config.output_dir),
                "secrets_masked": True,
                "note": (
                    "Use action='get' for the values. Keys are masked here and "
                    "cannot be written through MCP."
                ),
            }

        if action != "get":
            return {
                "ok": False,
                "error": f"unknown action: {action!r} (expected 'get' or 'list')",
            }

        masked = config.masked()
        if section is None:
            return {"ok": True, "action": "get", "config": masked, "secrets_masked": True}

        if section not in SECTIONS:
            return {
                "ok": False,
                "error": f"unknown section: {section!r} (expected one of {', '.join(SECTIONS)})",
            }

        return {
            "ok": True,
            "action": "get",
            "section": section,
            "config": masked[section],
            "secrets_masked": True,
        }
