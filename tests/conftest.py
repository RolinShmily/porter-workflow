"""Shared pytest fixtures.

Deliberately minimal: the engine's design goal is that every layer can be
constructed without network, ffmpeg, or a real config file, so almost nothing
needs patching here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from porter.logging import reset as reset_logging


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    """Undo any handler installation so tests cannot leak log state."""
    reset_logging()
    yield
    reset_logging()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real environment out of config resolution tests."""
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_MODEL",
        "WHISPER_API_KEY",
        "WHISPER_API_BASE",
        "WHISPER_MODEL",
        "PORTER_ASR_ENGINE",
        "PORTER_LLM_MODEL",
        "PORTER_OUTPUT_DIR",
        "PORTER_CONFIG",
        "PORTER_LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_job_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test write to the developer's real job registry.

    The registry lives under ``platformdirs.user_cache_dir``, and several code
    paths reach it without any test asking: ``porter run`` registers its job,
    ``porter jobs`` reads and reaps it, and the MCP server builds a store at
    import time. Two tests therefore wrote into ``~/.cache/porter/jobs.json``,
    and one of them reaped records belonging to real runs.

    Redirecting the path seam here makes it structural instead of a rule each
    new test has to remember. Tests that want a specific registry still declare
    one; this only decides where the *default* points.
    """
    from porter.jobs import records as records_module

    monkeypatch.setattr(records_module, "registry_file", lambda: tmp_path / "jobs.json")


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project directory, used as the CWD for config discovery."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("porter.config.config_file", lambda: tmp_path / "nonexistent_user_config.json")
    return tmp_path


@pytest.fixture
def write_config(tmp_path: Path):
    """Factory writing a JSON config file and returning its path."""

    def _write(data: dict, name: str = "porter.json") -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    return _write
