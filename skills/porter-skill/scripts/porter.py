#!/usr/bin/env python3
"""Run porter without installing it.

This is the skill's only wrapper. It exists so an agent can invoke the CLI
without creating a virtualenv, activating it, or knowing where porter lives:
``uvx`` resolves the distribution in an ephemeral environment and this script
forwards every argument to it verbatim.

Usage (the interpreter spelling depends on the host):

    python3 <SKILL_DIR>/scripts/porter.py doctor --json
    py -3 <SKILL_DIR>\\scripts\\porter.py doctor --json

    python3 <SKILL_DIR>/scripts/porter.py capabilities --json
    python3 <SKILL_DIR>/scripts/porter.py probe "https://youtu.be/..." --json
    python3 <SKILL_DIR>/scripts/porter.py plan "https://youtu.be/..." --json > plan.json
    python3 <SKILL_DIR>/scripts/porter.py run --plan plan.json --json
    python3 <SKILL_DIR>/scripts/porter.py jobs status <job_id>
    python3 <SKILL_DIR>/scripts/porter.py --host-check

Why Python and not two shell wrappers
-------------------------------------
The state-directory rules, writability checks and uvx invocation are identical
on every platform. Maintaining a Bash copy and a PowerShell copy meant fixing
every bug twice and keeping two subtly different ``--host-check``
implementations in agreement. This file needs only the standard library, and it
is the same file on Linux, macOS and Windows.

Python itself is a prerequisite, not something this script can install. The SOP
checks ``python3 --version`` / ``py -3 --version`` before the first porter call;
if no interpreter is present, the agent guides the user through installing one.
``uvx`` can then provision a Python 3.11+ interpreter for porter even when the
host's default Python is older, so this wrapper deliberately runs on 3.8+.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

__all__ = ["main"]

#: Environment variables that must stay inside the host's writable workspace.
#: The order is only for readable diagnostics.
_STATE_DIRS: tuple[tuple[str, str], ...] = (
    ("UV_CACHE_DIR", "uv-cache"),
    ("UV_TOOL_DIR", "uv-tools"),
    ("UV_TOOL_BIN_DIR", "uv-tools/bin"),
    ("UV_PYTHON_INSTALL_DIR", "uv-python"),
    ("UV_PYTHON_BIN_DIR", "uv-python/bin"),
    ("XDG_CACHE_HOME", "cache"),
    ("PORTER_CACHE_DIR", "porter-cache"),
)

#: The wrapper uses no syntax newer than this. porter itself requires 3.11, but
#: uvx can provision that; blocking here would only make the error less clear.
_MIN_PYTHON = (3, 8)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if sys.version_info < _MIN_PYTHON:
        _err(
            "porter-skill: Python "
            f"{_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+ is needed to run this wrapper "
            f"(found {sys.version.split()[0]})."
        )
        return 2

    env = os.environ.copy()
    state = env.get("PORTER_SKILL_STATE_DIR", "").strip()
    if state:
        if not os.path.isabs(state):
            _err(f"porter-skill: PORTER_SKILL_STATE_DIR must be absolute: {state}")
            return 2
        _apply_state_dir(env, Path(state))

    if args[:1] == ["--host-check"]:
        return _host_check(env, state_configured=bool(state))

    if not _check_configured_dirs(env):
        return 1

    spec = env.get("PORTER_SKILL_SPEC") or "porter-workflow"
    command = _uvx_command(spec, args)

    try:
        # The command is a fixed argument list built above; no shell is involved.
        code = subprocess.call(command, env=env)  # noqa: S603
    except FileNotFoundError:
        _err(
            "porter-skill: uvx not found on PATH; install uv "
            "(https://docs.astral.sh/uv/) or use a pre-installed porter CLI."
        )
        return 127
    except OSError as exc:
        _err(f"porter-skill: could not start uvx: {exc}")
        return 127
    except KeyboardInterrupt:
        return 130

    # A child killed by a signal reports a negative code; shells use 128+N.
    return 128 - code if code < 0 else code


def _uvx_command(spec: str, args: list[str]) -> list[str]:
    """Build the uvx invocation, including Windows shim handling.

    ``uv`` normally installs ``uvx.exe``; a package-manager shim may instead be
    ``uvx.cmd``, which CreateProcess cannot start directly. Resolve through
    ``PATH`` so both cases work without asking the user which one they have.
    """
    resolved = shutil.which("uvx")
    if resolved is None:
        return ["uvx", "--from", spec, "porter", *args]
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/c", resolved, "--from", spec, "porter", *args]
    return [resolved, "--from", spec, "porter", *args]


# ----------------------------------------------------------------------
# Workspace state directory
# ----------------------------------------------------------------------


def _apply_state_dir(env: dict[str, str], state: Path) -> None:
    """Point uv, yt-dlp and the job registry at ``state``.

    Overrides pre-existing variables on purpose: on a restricted host a
    machine-level cache variable can point outside the sandbox, and honouring it
    would defeat the setting.
    """
    for name, relative in _STATE_DIRS:
        env[name] = str(state / relative)


def _host_check(env: dict[str, str], *, state_configured: bool) -> int:
    if not state_configured:
        _err(
            "porter-skill: no workspace state directory configured; host "
            "defaults cannot be certified."
        )
        _err(
            "porter-skill: in a restricted host set PORTER_SKILL_STATE_DIR to "
            "an absolute writable workspace path."
        )

    if not _check_configured_dirs(env):
        return 1

    _err(
        "porter-skill: configured writable directories OK "
        "(external commands and network not tested)."
    )
    return 0


def _check_configured_dirs(env: dict[str, str]) -> bool:
    for name, _relative in _STATE_DIRS:
        raw = env.get(name)
        if not raw:
            continue
        if not _is_writable(Path(raw).expanduser()):
            _err(
                f"porter-skill: cannot write {raw}; select a writable workspace "
                "with PORTER_SKILL_STATE_DIR"
            )
            return False
    return True


def _is_writable(directory: Path) -> bool:
    """Create ``directory`` and prove it accepts a write.

    Checked by writing, not by inspecting permissions: mode bits do not account
    for read-only mounts, ACLs, a full disk, or WSL2's ``/mnt/c`` translation.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".porter-skill-write-{uuid.uuid4().hex}"
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        probe.unlink()
    except OSError:
        return False
    return True


def _err(message: str) -> None:
    """stderr, not stdout. The MCP transport owns stdout elsewhere; keep the
    habit here too so a caller can pipe the CLI's own output safely."""
    sys.stderr.write(message + "\n")


if __name__ == "__main__":
    sys.exit(main())
