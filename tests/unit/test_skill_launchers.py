"""Offline contract tests for the skill's shell entry points.

Fake uvx avoids installing packages while still exercising real argument and
sandbox environment forwarding. PowerShell's native runtime needs a Windows
runner; the source-level asset check below is not a substitute for that smoke.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "porter-skill" / "scripts"
CACHE_KEYS = (
    "UV_CACHE_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_BIN_DIR", "XDG_CACHE_HOME", "PORTER_CACHE_DIR",
)


@pytest.fixture
def mock_uvx(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "call.json"
    script = bin_dir / "uvx"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "keys = " + repr(CACHE_KEYS) + "\n"
        "with open(os.environ['MOCK_CAPTURE'], 'w', encoding='utf-8') as f:\n"
        "    json.dump({'args': sys.argv[1:], 'env': {k: os.environ.get(k) for k in keys}}, f)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir, capture


def invoke(
    mock_uvx: tuple[Path, Path], tmp_path: Path, name: str, *args: str,
    state: str | None = None, extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
    bin_dir, capture = mock_uvx
    env = {k: v for k, v in os.environ.items() if k not in CACHE_KEYS}
    env.pop("PORTER_SKILL_SPEC", None)
    env.pop("PORTER_SKILL_STATE_DIR", None)
    env.update({"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}", "MOCK_CAPTURE": str(capture)})
    if state is not None:
        env["PORTER_SKILL_STATE_DIR"] = state
    if extra:
        env.update(extra)
    result = subprocess.run(
        ["bash", str(SCRIPTS / name), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    return result, json.loads(capture.read_text(encoding="utf-8")) if capture.exists() else None


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash required")
def test_porter_forwards_args_with_small_default_and_no_overrides(
    mock_uvx: tuple[Path, Path], tmp_path: Path,
) -> None:
    result, call = invoke(mock_uvx, tmp_path, "porter.sh", "plan", "https://example.com/a?x=1", "--json")
    assert result.returncode == 0, result.stderr
    assert call == {
        "args": ["--from", "porter-workflow[llm]", "porter", "plan", "https://example.com/a?x=1", "--json"],
        "env": dict.fromkeys(CACHE_KEYS),
    }


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash required")
def test_inspect_uses_the_same_sandbox_paths_and_custom_spec(
    mock_uvx: tuple[Path, Path], tmp_path: Path,
) -> None:
    state = tmp_path / "allowed space" / ".state"
    result, call = invoke(
        mock_uvx, tmp_path, "inspect.sh", "https://example.com", "--json",
        state=str(state), extra={
            "UV_CACHE_DIR": str(tmp_path / "blocked"),
            "PORTER_SKILL_SPEC": "porter-workflow[llm,asr-local]",
        },
    )
    assert result.returncode == 0, result.stderr
    assert call is not None
    assert call["args"] == ["--from", "porter-workflow[llm,asr-local]", "porter", "inspect", "https://example.com", "--json"]
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


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash required")
def test_host_check_does_not_run_uvx(mock_uvx: tuple[Path, Path], tmp_path: Path) -> None:
    state = tmp_path / "allowed" / ".state"
    result, call = invoke(mock_uvx, tmp_path, "porter.sh", "--host-check", state=str(state))
    assert result.returncode == 0, result.stderr
    assert call is None
    assert (state / "porter-cache").is_dir()


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash required")
def test_reject_relative_or_unwritable_state_before_uvx(
    mock_uvx: tuple[Path, Path], tmp_path: Path,
) -> None:
    result, call = invoke(mock_uvx, tmp_path, "porter.sh", "doctor", state="relative")
    assert result.returncode == 2
    assert "must be absolute" in result.stderr
    assert call is None

    blocked = tmp_path / "file-not-directory"
    blocked.write_text("x", encoding="utf-8")
    result, call = invoke(mock_uvx, tmp_path, "porter.sh", "doctor", state=str(blocked))
    assert result.returncode == 1
    assert str(blocked / "uv-cache") in result.stderr
    assert call is None


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash required")
def test_configured_host_cache_denied_without_opt_in(
    mock_uvx: tuple[Path, Path], tmp_path: Path,
) -> None:
    blocked = tmp_path / "cache-is-a-file"
    blocked.write_text("not a directory", encoding="utf-8")
    result, call = invoke(
        mock_uvx, tmp_path, "porter.sh", "inspect", "https://example.com",
        extra={"UV_CACHE_DIR": str(blocked)},
    )
    assert result.returncode == 1
    assert str(blocked) in result.stderr
    assert "PORTER_SKILL_STATE_DIR" in result.stderr
    assert call is None


def test_windows_launcher_ships_required_contract() -> None:
    script = (SCRIPTS / "porter.ps1").read_text(encoding="utf-8")
    assert "--host-check" in script
    assert "PORTER_SKILL_STATE_DIR" in script
    assert "porter-workflow[llm]" in script
    assert "@args" in script
    for key in CACHE_KEYS:
        assert f"$env:{key} = " in script
