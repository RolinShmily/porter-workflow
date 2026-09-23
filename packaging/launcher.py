"""The ``porter`` executable's launcher.

This is **not** the engine. It is the small program that PyInstaller freezes
into the released ``porter`` / ``porter.exe``, and its only job is to make sure a
working installation exists and then get out of the way.

Why not freeze the engine with PyInstaller
------------------------------------------

``yt-dlp`` is the reason. It is the component that breaks when a video site
changes, and it ships weekly fixes. Freezing it means the copy inside the
executable is the copy the user is stuck with: the day YouTube changes
something, the released binary is dead and the fix is a 60 MB re-download.
Freezing also fights ``remote_components`` (yt-dlp downloads its JavaScript
challenge solver at runtime), whose dynamic paths are exactly what
PyInstaller's frozen-filesystem assumptions tend to break.

So the released binary contains only this file plus the standard library
(a few hundred KB), and the engine lives in a normal virtualenv under
``~/.porter/venv`` that can be updated in place. The CLI, the MCP server and the
skill's scripts all end up using that same venv, so all three behave
identically.

Layout
------

``$PORTER_HOME`` (default ``~/.porter``)
    ``venv/``                 the installation, created on first run
    ``venv/.ytdlp-refresh``   timestamp of the last yt-dlp update

Environment variables
---------------------

``PORTER_HOME``
    Override the installation root. Useful for containers and for tests.
``PORTER_LAUNCHER_NO_UPDATE``
    Set to ``1`` to skip the periodic yt-dlp refresh entirely. Release-mode
    correctness must not depend on the network, and neither should a user who
    pins versions on purpose.
``PORTER_LAUNCHER_SPEC``
    The requirement installed into the venv. Defaults to
    ``porter-workflow[all]``; a release built from a local wheel can point this
    at a path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

__all__ = ["main"]

#: Release requirement. Overridable so an offline install can point at a wheel.
DEFAULT_SPEC = "porter-workflow[all]"

#: Refresh yt-dlp at most this often. It ships weekly fixes, and a stale copy is
#: the single most common cause of "the download stopped working", but doing
#: this on every launch would add a network round-trip to every command.
REFRESH_AFTER_SECONDS = 7 * 24 * 60 * 60


def _porter_home() -> Path:
    """Installation root, honouring ``$PORTER_HOME``."""
    override = os.environ.get("PORTER_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".porter"


def _venv_python(venv: Path) -> Path:
    """Path to the venv's interpreter (Windows and POSIX layouts differ)."""
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_script(venv: Path, name: str) -> Path:
    """Path to a console script installed inside the venv."""
    if os.name == "nt":
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def _log(message: str) -> None:
    """Write a launcher message to stderr.

    stderr, not stdout, and deliberately so: the process we hand over to is a
    CLI whose stdout may be piped into ``--json`` consumers, and an MCP host
    reads stdout as JSON-RPC frames. Setup chatter on stdout would corrupt both.
    """
    print(f"porter: {message}", file=sys.stderr, flush=True)


def _run(argv: list[str]) -> None:
    """Run a setup command, surfacing its output on failure."""
    # noqa justification: argv is built here from a fixed command plus paths we
    # derived ourselves; no shell is involved and no user string is interpreted.
    result = subprocess.run(argv, check=False)  # noqa: S603
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(argv)}")


def _find_uv() -> str | None:
    """Return the ``uv`` executable name if it is runnable, else ``None``."""
    for candidate in ("uv", "uv.exe"):
        try:
            probe = subprocess.run(  # noqa: S603
                [candidate, "--version"],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            continue
        if probe.returncode == 0:
            return candidate
    return None


def _create_venv(venv: Path, uv: str | None) -> None:
    """Create the venv, preferring uv and falling back to the stdlib."""
    _log(f"creating {venv}")
    venv.parent.mkdir(parents=True, exist_ok=True)
    if uv is not None:
        _run([uv, "venv", "--python", sys.executable, str(venv)])
    else:
        # `python -m venv` seeds pip via ensurepip, which the fallback install
        # below relies on. `uv venv` does not install pip at all, which is why
        # the two branches keep their own install command.
        _run([sys.executable, "-m", "venv", str(venv)])


def _install_into_venv(venv: Path, spec: str, uv: str | None, upgrade: bool) -> None:
    """Install (or refresh) ``spec`` inside ``venv``."""
    python = str(_venv_python(venv))
    if uv is not None:
        argv = [uv, "pip", "install", "--python", python]
    else:
        argv = [python, "-m", "pip", "install", "--disable-pip-version-check"]
    if upgrade:
        argv.append("--upgrade")
    argv.append(spec)
    _run(argv)


def _refresh_stamp(venv: Path) -> Path:
    return venv / ".ytdlp-refresh"


def _ytdlp_refresh_due(venv: Path) -> bool:
    """Has it been long enough that refreshing yt-dlp is worth a network call?"""
    stamp = _refresh_stamp(venv)
    try:
        last = stamp.stat().st_mtime
    except OSError:
        return True
    return (time.time() - last) > REFRESH_AFTER_SECONDS


def _refresh_ytdlp(venv: Path, uv: str | None) -> None:
    """Update yt-dlp in place, and record the attempt.

    The stamp is written even when the update fails: a machine that is offline
    or behind a proxy must not pay a failing network round-trip on *every*
    launch. The next launch a week later tries again.
    """
    if os.environ.get("PORTER_LAUNCHER_NO_UPDATE", "").strip() == "1":
        return
    _log("refreshing yt-dlp")
    try:
        _install_into_venv(venv, "yt-dlp[default]", uv, upgrade=True)
    except RuntimeError as exc:
        _log(f"warning: yt-dlp refresh failed, continuing with the installed copy ({exc})")
    finally:
        try:
            _refresh_stamp(venv).touch()
        except OSError:
            pass


def _hand_over(script: Path, argv: list[str]) -> None:
    """Replace this process with the real CLI.

    POSIX: ``execv`` so the child inherits the terminal, the exit code and any
    signal handling directly -- there is no wrapper process left to confuse an
    agent that is watching for a non-zero exit.

    Windows: ``os.execv`` does not replace the process image, so it would leave
    a launcher parent behind and break ``Ctrl-C`` propagation. Spawn and wait
    instead, then propagate the exit code immediately.
    """
    if os.name == "nt":
        completed = subprocess.run([str(script), *argv], check=False)  # noqa: S603
        raise SystemExit(completed.returncode)

    # argv[0] is conventionally the program name.
    os.execv(str(script), [str(script), *argv])


def main() -> int:
    """Ensure an installation exists, then hand over to it."""
    venv = _porter_home() / "venv"
    script = _venv_script(venv, "porter")
    spec = os.environ.get("PORTER_LAUNCHER_SPEC", "").strip() or DEFAULT_SPEC

    uv = _find_uv()

    if not script.is_file():
        try:
            _create_venv(venv, uv)
            _log(f"installing {spec}")
            _install_into_venv(venv, spec, uv, upgrade=False)
        except RuntimeError as exc:
            _log(str(exc))
            _log("installation failed; see the README for manual setup")
            return 1
        if not script.is_file():
            _log(f"installation finished but {script} is missing")
            return 1
    elif _ytdlp_refresh_due(venv):
        _refresh_ytdlp(venv, uv)

    _hand_over(script, sys.argv[1:])
    return 0  # unreachable on POSIX; satisfies the declared return type


if __name__ == "__main__":
    raise SystemExit(main())
