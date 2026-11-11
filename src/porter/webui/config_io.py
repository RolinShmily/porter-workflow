"""Configuration file access for the WebUI settings tab."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from porter.core.config import (
    config_file,
    default_config,
    find_project_config,
    load_config_file,
)
from porter.core.errors import ConfigError
from porter.core.logging import get_logger

_logger = get_logger("web.config")

__all__ = ["get_config_target", "read_config_dict", "save_config_dict"]


def get_config_target() -> Path:
    """Resolve the JSON file the WebUI may edit.

    A project-level TOML file is deliberately skipped: the settings tab writes
    JSON, and writing JSON into ``porter.toml`` would make the next resolution
    fail. Editing TOML stays a manual, text-editor operation.
    """
    found = find_project_config()
    if found:
        if found.suffix.lower() == ".json":
            return found
        _logger.warning(
            "project config %s is not JSON; the WebUI edits JSON, falling back to "
            "the user config file",
            found,
        )
    return config_file()


def read_config_dict() -> tuple[dict[str, Any], Path]:
    """Read existing config file as dict, or return default mapping."""
    dest = get_config_target()
    if dest.is_file():
        try:
            return load_config_file(dest), dest
        except (ConfigError, OSError) as exc:
            _logger.warning("Failed to parse config %s: %s", dest, exc)
    return default_config().model_dump(mode="json"), dest


def save_config_dict(data: dict[str, Any], dest: Path) -> None:
    """Save config dict to target JSON file with secure permissions."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    dest.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        dest.chmod(0o600)
