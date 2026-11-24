#!/usr/bin/env python3
"""Create a local virtualenv for an offline or source-checkout install.

This is the fallback for hosts where ``uvx`` is unavailable (offline machines,
a packaged EXE, or a user who wants porter in a project virtualenv). Prefer
``scripts/porter.py``: it installs nothing and needs no activation.

Usage, from a checkout of the repository:

    python3 skills/porter-skill/scripts/bootstrap.py
    python3 skills/porter-skill/scripts/bootstrap.py --extras asr-local
    python3 skills/porter-skill/scripts/bootstrap.py --extras asr-local,llm

Then invoke the console script it prints. The base install covers yt-dlp,
platform subtitles, key-free translation and rendering. ``asr-local`` adds free
offline speech recognition; ``llm`` adds the paid Whisper/LLM capabilities.
Installing large extras needs the user's consent.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a virtualenv and install porter-workflow in editable mode."
    )
    parser.add_argument(
        "--extras",
        default="",
        metavar="LIST",
        help=(
            "Comma-separated extras, for example 'asr-local' or "
            "'asr-local,llm'. Empty installs the free-first base package."
        ),
    )
    parser.add_argument(
        "--venv",
        default=".venv",
        metavar="DIR",
        help="Virtualenv directory, relative to the repository root by default.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[3]
    if not (repo_root / "pyproject.toml").is_file():
        _err(f"bootstrap.py must run from a porter-workflow checkout; looked in {repo_root}")
        return 1

    venv = Path(args.venv)
    if not venv.is_absolute():
        venv = repo_root / venv
    extras = ",".join(part.strip() for part in args.extras.split(",") if part.strip())
    spec = f".[{extras}]" if extras else "."

    try:
        if shutil.which("uv"):
            _out(f"==> Creating {venv} with uv")
            _run(["uv", "venv", str(venv)], cwd=repo_root)
            python = _venv_python(venv)
            _run(
                ["uv", "pip", "install", "--python", str(python), "-e", spec],
                cwd=repo_root,
            )
        else:
            _out(f"==> Creating {venv} with python -m venv")
            _run([sys.executable, "-m", "venv", str(venv)], cwd=repo_root)
            python = _venv_python(venv)
            _run([str(python), "-m", "pip", "install", "-e", spec], cwd=repo_root)
    except (OSError, subprocess.CalledProcessError) as exc:
        _err(f"bootstrap.py: installation failed: {exc}")
        return 1

    console = _venv_console(venv)
    _out("==> Installed. Check the host with:")
    _out(f"    {console} doctor")
    return 0


def _run(command: list[str], *, cwd: Path) -> None:
    _out("    " + " ".join(command))
    # The command is assembled from fixed argv lists above; no shell is involved.
    subprocess.run(command, cwd=cwd, check=True)  # noqa: S603



def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_console(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "porter.exe"
    return venv / "bin" / "porter"


def _out(message: str) -> None:
    """Progress goes to stdout; this is a standalone installer, not the CLI."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _err(message: str) -> None:
    sys.stderr.write(message + "\n")


if __name__ == "__main__":
    sys.exit(main())
