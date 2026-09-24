"""``porter config`` — inspect and mutate configuration.

Secrets are never echoed in plaintext: ``list`` and ``get`` render masked
previews, so the output is safe to paste into a bug report.

Writing a key defaults to the *user-level* config file rather than the nearest
project file, so that running the command from an unexpected directory cannot
silently edit a repository.
"""

from __future__ import annotations

import argparse
from typing import Any

from porter.config import (
    config_file,
    find_project_config,
    mask_secret,
    resolve,
    save_key,
)
from porter_cli import render

__all__ = ["configure", "run"]


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``config`` command and its sub-actions."""
    parser = subparsers.add_parser(
        "config",
        help="Show or edit configuration.",
        description="Show the effective configuration, or set a single key.",
    )
    actions = parser.add_subparsers(dest="config_action", metavar="<action>")

    actions.add_parser("list", help="Print the effective configuration (secrets masked).")

    get_parser = actions.add_parser("get", help="Print one resolved key (secret masked).")
    get_parser.add_argument("key", help="Dotted key, e.g. llm.model")

    set_parser = actions.add_parser("set", help="Persist one key to the user config file.")
    set_parser.add_argument(
        "assignment",
        metavar="KEY=VALUE",
        help="Dotted key and value, e.g. llm.model=deepseek-chat",
    )
    set_parser.add_argument(
        "--file",
        dest="target_file",
        metavar="PATH",
        help="Write to this file instead of the user config file.",
    )

    actions.add_parser("path", help="Print which config file is in effect.")

    actions.add_parser(
        "import-videocaptioner",
        help="Import settings from a VideoCaptioner config.toml (not yet implemented).",
    )

    parser.set_defaults(handler=run)


def _lookup(data: dict[str, Any], key: str) -> Any:
    """Resolve a dotted key against nested dicts. Returns ``KeyError``-free None."""
    cursor: Any = data
    for part in key.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def run(args: argparse.Namespace) -> int:
    """Execute the selected ``config`` action."""
    action = getattr(args, "config_action", None)

    if action is None:
        render.info("usage: porter config {list,get,set,path}")
        return render.EXIT_MISUSE

    if action == "list":
        config = resolve(args.config_path)
        render.value(render.banner("PORTER WORKFLOW CONFIGURATION"))
        render.value(f"Source: {config.source}")
        render.value("")
        render.emit_json(config.masked())
        return render.EXIT_OK

    if action == "get":
        config = resolve(args.config_path)
        found = _lookup(config.masked(), args.key)
        if found is None:
            render.warn(f"unknown configuration key: {args.key}")
            return render.EXIT_ERROR
        render.value(f"{args.key} = {found}")
        return render.EXIT_OK

    if action == "set":
        if "=" not in args.assignment:
            render.warn("expected KEY=VALUE, e.g. porter config set llm.model=deepseek-chat")
            return render.EXIT_MISUSE
        key, _, raw_value = args.assignment.partition("=")
        key = key.strip()
        destination = save_key(
            key,
            raw_value.strip(),
            target=args.target_file,
        )
        render.info(f"saved '{key}' to {destination}")
        render.info(f"  (value stored: {mask_secret(raw_value.strip())})")
        return render.EXIT_OK

    if action == "path":
        explicit = args.config_path
        config = resolve(explicit)
        render.value(config.source)
        if explicit is None:
            project = find_project_config()
            if project is not None:
                render.info(f"  (project-level file found at {project})")
            else:
                render.info(f"  (user-level default: {config_file()})")
        return render.EXIT_OK

    if action == "import-videocaptioner":
        return render.not_implemented("config import-videocaptioner")

    render.warn(f"unknown config action: {action}")
    return render.EXIT_MISUSE
