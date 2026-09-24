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

import contextlib
import locale
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

__all__ = ["main"]

#: Set in the environment of every process this launcher spawns.
#:
#: The launcher is a Python program that is *also* an executable, so anything that
#: runs it in order to identify an interpreter re-enters this file. That is not
#: hypothetical: ``uv venv --python <launcher>`` probes the given path by
#: executing it, the probe re-ran the install, the install called ``uv`` again,
#: and a single `porter.exe --version` on a clean machine became 970 nested
#: retries taking 219 seconds before it died. Re-entry is now an immediate,
#: legible failure instead.
REENTRY_MARKER = "PORTER_LAUNCHER_IN_PROGRESS"

# -- Where packages come from ------------------------------------------------
#
# Deliberately a copy of porter.mirrors rather than an import. This file is
# frozen by PyInstaller and has to run *before* porter exists, so importing the
# engine would mean bundling it into the launcher. tests/unit/test_launcher.py
# drives both copies through the same environments and asserts they agree,
# because a silent drift between them would be worse than either being wrong.

MIRROR_ENV = "PORTER_MIRROR"
USTC_PYPI_INDEX = "https://mirrors.ustc.edu.cn/pypi/simple"
_MIRROR_ON = frozenset({"cn", "china", "1", "true", "yes", "on"})
_MIRROR_OFF = frozenset({"off", "0", "false", "no", "none", "intl"})
_TZ_MARKERS = (
    "china",
    "chinese",
    "shanghai",
    "chongqing",
    "harbin",
    "urumqi",
    "beijing",
    "prc",
    "中国",
)

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
    # T201 is the project-wide "never print" rule, and stdout *is* forbidden here
    # for the reason above -- but this writes to stderr, which is the only channel
    # a bootstrap shim has.
    print(f"porter: {message}", file=sys.stderr, flush=True)  # noqa: T201


def _child_env() -> dict[str, str]:
    """The environment for a child process, marked as launcher-spawned.

    It also decides which index packages come from. Order, and why:

    1. A uv index the user set is left alone.
    2. A *pip* index the user set is copied to uv. uv ignores ``PIP_INDEX_URL``
       -- verified: pointed at an unreachable host it still resolved from PyPI --
       so without this, the most common way to configure a mirror in China
       (``pip config set global.index-url ...``) silently does nothing at all.
    3. Otherwise, on a machine that looks Chinese, both point at USTC.
    """
    env = dict(os.environ)
    env[REENTRY_MARKER] = "1"
    _apply_index_mirror(env)
    return env


def _apply_index_mirror(env: dict[str, str]) -> None:
    """Point ``env`` at a mirror, unless the user already chose an index."""
    if any(env.get(name, "").strip() for name in ("UV_DEFAULT_INDEX", "UV_INDEX_URL")):
        return

    pip_index = env.get("PIP_INDEX_URL", "").strip()
    if pip_index:
        # The user has an index. Make uv respect it rather than quietly using PyPI.
        env["UV_DEFAULT_INDEX"] = pip_index
        return

    if _use_china_mirrors():
        env["UV_DEFAULT_INDEX"] = USTC_PYPI_INDEX
        env["PIP_INDEX_URL"] = USTC_PYPI_INDEX


def _local_now() -> datetime:
    """This machine's current time, as a seam for the tests below."""
    return datetime.now().astimezone()


def _mirrors_forced() -> bool | None:
    """``True``/``False`` when ``PORTER_MIRROR`` says so, else ``None``."""
    raw = os.environ.get(MIRROR_ENV, "").strip().lower()
    if raw in _MIRROR_ON:
        return True
    if raw in _MIRROR_OFF:
        return False
    return None


def _use_china_mirrors() -> bool:
    """Whether to prefer the mirrors, honouring ``PORTER_MIRROR`` first.

    Shaped exactly like :func:`porter.mirrors.use_china_mirrors`; the two are
    compared case by case in tests/unit/test_launcher.py.
    """
    explicit = _mirrors_forced()
    if explicit is not None:
        return explicit

    tz = os.environ.get("TZ", "").strip().lower()
    if tz:
        # Believed in both directions, and final: an explicit America/New_York
        # must not be overruled by a Chinese locale.
        return any(marker in tz for marker in _TZ_MARKERS)

    try:
        local = _local_now()
    except (OSError, ValueError, OverflowError):
        return False

    if any(marker in (local.tzname() or "").lower() for marker in _TZ_MARKERS):
        return True
    try:
        # UTC+8: Greater China, Singapore, Perth. Survives Windows localisation
        # and an absent TZ variable, both of which this machine demonstrates.
        if local.utcoffset() == timedelta(hours=8):
            return True
    except (OSError, ValueError, OverflowError):
        return False

    try:
        language, _encoding = locale.getlocale()
    except (locale.Error, ValueError, TypeError):
        return False
    if not language:
        return False
    # ``zh_CN`` on POSIX, ``Chinese (Simplified)_China`` on Windows.
    lowered = language.lower()
    return lowered.startswith("zh") or "china" in lowered


def _run(argv: list[str]) -> None:
    """Run a setup command, surfacing its output on failure."""
    # argv is a fixed command plus paths we derived ourselves: no shell, and no
    # user string is interpreted.
    result = subprocess.run(argv, check=False, env=_child_env())  # noqa: S603
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
                env=_child_env(),
            )
        except FileNotFoundError:
            continue
        if probe.returncode == 0:
            return candidate
    return None


def _interpreter_for_venv() -> str | None:
    """An interpreter to build the venv with, or ``None`` to let ``uv`` choose.

    ``sys.executable`` is a Python only when this file is run as a script. Frozen
    by PyInstaller it is the launcher binary itself, and handing that to
    ``uv venv --python`` does not merely fail -- ``uv`` inspects the path by
    running it, so the launcher re-entered itself (see :data:`REENTRY_MARKER`).

    Returning ``None`` frozen is deliberate rather than guessing from ``PATH``:
    ``uv`` resolves (or fetches) an interpreter that actually satisfies
    ``requires-python``, whereas a stale ``python3`` would turn into a confusing
    pip failure several steps later.
    """
    if getattr(sys, "frozen", False):
        return None
    return sys.executable


def _create_venv(venv: Path, uv: str | None) -> None:
    """Create the venv, preferring uv and falling back to the stdlib."""
    _log(f"creating {venv}")
    venv.parent.mkdir(parents=True, exist_ok=True)
    interpreter = _interpreter_for_venv()

    if uv is not None:
        argv = [uv, "venv"]
        if interpreter is not None:
            argv += ["--python", interpreter]
        _run([*argv, str(venv)])
        return

    # No uv: the venv has to be built by a Python we can find. The binary cannot
    # be that Python, so look on PATH -- and say so plainly if there is nothing.
    fallback = interpreter or shutil.which("python3") or shutil.which("python")
    if fallback is None:
        raise RuntimeError(
            "no Python interpreter available to build the environment with; "
            "install uv (recommended) or Python 3.11+, then run this again"
        )
    # `python -m venv` seeds pip via ensurepip, which the fallback install below
    # relies on. `uv venv` does not install pip at all, which is why the two
    # branches keep their own install command.
    _run([fallback, "-m", "venv", str(venv)])


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
        with contextlib.suppress(OSError):
            _refresh_stamp(venv).touch()


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
        completed = subprocess.run(  # noqa: S603
            [str(script), *argv], check=False, env=_child_env()
        )
        raise SystemExit(completed.returncode)

    # argv[0] is conventionally the program name. execv involves no shell, which
    # is precisely why it is used.
    os.execv(str(script), [str(script), *argv])  # noqa: S606


def main() -> int:
    """Ensure an installation exists, then hand over to it."""
    if os.environ.get(REENTRY_MARKER):
        _log(
            "this executable is the porter launcher, not a Python interpreter, "
            "and it was invoked again while already running"
        )
        return 1

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
