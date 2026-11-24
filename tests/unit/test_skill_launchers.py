"""Offline contract tests for the skill's Python entry point.

A fake ``uvx`` on PATH avoids installing packages while still exercising real
argument forwarding and sandbox environment handling. The wrapper is one Python
file, so the same tests exercise it on every host; the Windows-specific mock
shim (``uvx.cmd``) exists so that path is covered by the Windows CI job too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "porter-skill" / "scripts"
CACHE_KEYS = (
    "UV_CACHE_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_BIN_DIR", "XDG_CACHE_HOME", "PORTER_CACHE_DIR",
)


def _write_mock_uvx(bin_dir: Path) -> Path:
    """Put a fake ``uvx`` on PATH that records its arguments and environment."""
    capture = bin_dir / "call.json"
    helper = bin_dir / "mock_uvx.py"
    helper.write_text(
        "import json, os, sys\n"
        f"keys = {CACHE_KEYS!r}\n"
        "with open(os.environ['MOCK_CAPTURE'], 'w', encoding='utf-8') as f:\n"
        "    json.dump({'args': sys.argv[1:], "
        "'env': {k: os.environ.get(k) for k in keys}}, f)\n",
        encoding="utf-8",
    )

    if os.name == "nt":
        # CreateProcess cannot start a .cmd directly, which is exactly why the
        # wrapper resolves uvx through PATH and goes via COMSPEC for this case.
        shim = bin_dir / "uvx.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0mock_uvx.py" %*\r\n',
            encoding="utf-8",
        )
    else:
        shim = bin_dir / "uvx"
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/mock_uvx.py" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
    return capture


def _invoke(
    *args: str,
    bin_dir: Path,
    capture: Path,
    state: str | None = None,
    extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
    env = {k: v for k, v in os.environ.items() if k not in CACHE_KEYS}
    env.pop("PORTER_SKILL_SPEC", None)
    env.pop("PORTER_SKILL_STATE_DIR", None)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "MOCK_CAPTURE": str(capture),
        }
    )
    if state is not None:
        env["PORTER_SKILL_STATE_DIR"] = state
    if extra:
        env.update(extra)

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "porter.py"), *args],
        cwd=bin_dir.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    call = None
    if capture.exists():
        call = json.loads(capture.read_text(encoding="utf-8"))
    return result, call


def test_porter_forwards_args_with_the_base_default_and_no_overrides(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = _write_mock_uvx(bin_dir)

    result, call = _invoke(
        "plan", "https://example.com/a?x=1", "--json",
        bin_dir=bin_dir, capture=capture,
    )

    assert result.returncode == 0, result.stderr
    assert call == {
        "args": [
            "--from", "porter-workflow", "porter",
            "plan", "https://example.com/a?x=1", "--json",
        ],
        "env": dict.fromkeys(CACHE_KEYS),
    }


def test_probe_uses_the_same_sandbox_paths_and_custom_spec(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = _write_mock_uvx(bin_dir)
    state = tmp_path / "allowed space" / ".state"

    result, call = _invoke(
        "probe", "https://example.com", "--json",
        bin_dir=bin_dir,
        capture=capture,
        state=str(state),
        extra={
            "UV_CACHE_DIR": str(tmp_path / "blocked"),
            "PORTER_SKILL_SPEC": "porter-workflow[asr-local]",
        },
    )

    assert result.returncode == 0, result.stderr
    assert call is not None
    assert call["args"] == [
        "--from", "porter-workflow[asr-local]", "porter",
        "probe", "https://example.com", "--json",
    ]
    assert call["env"] == {
        "UV_CACHE_DIR": str(state / "uv-cache"),
        "UV_TOOL_DIR": str(state / "uv-tools"),
        "UV_TOOL_BIN_DIR": str(state / "uv-tools" / "bin"),
        "UV_PYTHON_INSTALL_DIR": str(state / "uv-python"),
        "UV_PYTHON_BIN_DIR": str(state / "uv-python" / "bin"),
        "XDG_CACHE_HOME": str(state / "cache"),
        "PORTER_CACHE_DIR": str(state / "porter-cache"),
    }
    assert all(Path(path).is_dir() for path in call["env"].values())


def test_host_check_does_not_run_uvx(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = _write_mock_uvx(bin_dir)
    state = tmp_path / "allowed" / ".state"

    result, call = _invoke(
        "--host-check", bin_dir=bin_dir, capture=capture, state=str(state)
    )

    assert result.returncode == 0, result.stderr
    assert call is None
    assert (state / "porter-cache").is_dir()


def test_host_check_without_a_state_dir_warns_but_passes(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = _write_mock_uvx(bin_dir)

    result, call = _invoke("--host-check", bin_dir=bin_dir, capture=capture)

    assert result.returncode == 0
    assert call is None
    assert "PORTER_SKILL_STATE_DIR" in result.stderr


def test_reject_relative_or_unwritable_state_before_uvx(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = _write_mock_uvx(bin_dir)

    result, call = _invoke(
        "doctor", bin_dir=bin_dir, capture=capture, state="relative"
    )
    assert result.returncode == 2
    assert "must be absolute" in result.stderr
    assert call is None

    blocked = tmp_path / "file-not-directory"
    blocked.write_text("x", encoding="utf-8")
    result, call = _invoke(
        "doctor", bin_dir=bin_dir, capture=capture, state=str(blocked)
    )
    assert result.returncode == 1
    assert str(blocked / "uv-cache") in result.stderr
    assert call is None


def test_configured_host_cache_denied_without_opt_in(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = _write_mock_uvx(bin_dir)
    blocked = tmp_path / "cache-is-a-file"
    blocked.write_text("not a directory", encoding="utf-8")

    result, call = _invoke(
        "probe", "https://example.com",
        bin_dir=bin_dir, capture=capture, extra={"UV_CACHE_DIR": str(blocked)},
    )

    assert result.returncode == 1
    assert str(blocked) in result.stderr
    assert "PORTER_SKILL_STATE_DIR" in result.stderr
    assert call is None
