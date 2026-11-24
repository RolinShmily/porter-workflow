"""Configuration file access for the WebUI settings tab.

Delegates directly to :mod:`porter.core.config` so the WebUI and any external
caller share the exact same config target and encoding rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from porter.core.config import (
    read_config_mapping,
    writable_config_path,
    write_config_mapping,
)

__all__ = ["get_config_target", "read_config_dict", "save_config_dict"]


def get_config_target() -> Path:
    """Resolve the JSON file the WebUI may edit."""
    return writable_config_path()


def read_config_dict() -> tuple[dict[str, Any], Path]:
    """Read existing config file as dict, or return default mapping."""
    return read_config_mapping(get_config_target())


def save_config_dict(data: dict[str, Any], dest: Path) -> None:
    """Save config dict to target JSON file with secure permissions."""
    write_config_mapping(data, dest)
