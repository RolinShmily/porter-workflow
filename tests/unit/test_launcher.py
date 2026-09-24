"""Tests for ``packaging/launcher.py``.

The launcher is the thing users actually download, and until now nothing tested
it. That gap shipped a broken binary: ``_create_venv`` passed ``sys.executable``
to ``uv venv --python``, which is a Python only when the file runs as a script.
Frozen by PyInstaller it is the launcher binary itself, and ``uv`` inspects a
``--python`` path by *executing* it -- so the probe re-entered the launcher, the
launcher called ``uv`` again, and ``porter.exe --version`` on a clean machine
became 970 nested retries taking 219 seconds before failing.

Two things are therefore pinned here:

* **The bug** -- a frozen launcher must never offer itself as an interpreter.
* **The blast radius** -- re-entry must fail immediately and legibly rather than
  recursing, because that is what turned one wrong argument into a fork bomb.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

LAUNCHER_PATH = Path(__file__).resolve().parents[2] / "packaging" / "launcher.py"


def _load_launcher() -> Any:
    """Import ``packaging/launcher.py`` by path; it is not an installed module."""
    spec = importlib.util.spec_from_file_location("porter_launcher", LAUNCHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher() -> Any:
    return _load_launcher()


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend PyInstaller built us: ``sys.executable`` is the launcher binary."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\fake\porter.exe")


class TestTheInterpreterChoice:
    def test_run_as_a_script_it_offers_its_own_interpreter(self, launcher: Any) -> None:
        assert launcher._interpreter_for_venv() == sys.executable

    def test_frozen_it_does_not_offer_itself(self, launcher: Any, frozen: None) -> None:
        # The regression. `sys.executable` is the launcher, not an interpreter.
        assert launcher._interpreter_for_venv() is None


class TestCreateVenv:
    def _argv(self, launcher: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        seen: list[list[str]] = []
        monkeypatch.setattr(launcher, "_run", seen.append)
        return seen  # type: ignore[return-value]

    def test_with_uv_it_pins_the_running_interpreter(
        self, launcher: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen = self._argv(launcher, monkeypatch)
        launcher._create_venv(tmp_path / "venv", "uv")
        assert seen == [["uv", "venv", "--python", sys.executable, str(tmp_path / "venv")]]

    def test_frozen_with_uv_it_lets_uv_choose(
        self, launcher: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, frozen: None
    ) -> None:
        seen = self._argv(launcher, monkeypatch)
        launcher._create_venv(tmp_path / "venv", "uv")

        assert len(seen) == 1
        assert seen[0] == ["uv", "venv", str(tmp_path / "venv")], (
            "the launcher binary must not be offered as an interpreter"
        )
        assert "porter.exe" not in " ".join(seen[0])

    def test_frozen_without_uv_falls_back_to_a_python_on_path(
        self, launcher: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, frozen: None
    ) -> None:
        seen = self._argv(launcher, monkeypatch)
        monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")

        launcher._create_venv(tmp_path / "venv", None)

        assert seen == [["/usr/bin/python3", "-m", "venv", str(tmp_path / "venv")]]

    def test_frozen_without_uv_and_without_python_says_so(
        self, launcher: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, frozen: None
    ) -> None:
        monkeypatch.setattr(launcher.shutil, "which", lambda _name: None)

        with pytest.raises(RuntimeError, match="no Python interpreter available"):
            launcher._create_venv(tmp_path / "venv", None)


class TestReEntry:
    def test_children_are_marked(self, launcher: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(launcher.REENTRY_MARKER, raising=False)
        assert launcher._child_env()[launcher.REENTRY_MARKER] == "1"
        # And it is a copy: the parent's own environment must not gain it, or the
        # marker would leak into the shell that invoked us.
        assert launcher.REENTRY_MARKER not in os.environ

    def test_a_marked_process_refuses_instead_of_recursing(
        self, launcher: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(launcher.REENTRY_MARKER, "1")

        def _explode(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("a re-entrant launcher must not run a setup command")

        monkeypatch.setattr(launcher, "_create_venv", _explode)
        monkeypatch.setattr(launcher, "_install_into_venv", _explode)
        monkeypatch.setattr(launcher, "_hand_over", _explode)

        assert launcher.main() == 1

    def test_the_marker_is_named_as_the_documented_contract(self, launcher: Any) -> None:
        # Deliberately pinned: the marker is set in child environments, so
        # changing the name silently would un-guard every nested invocation.
        assert launcher.REENTRY_MARKER == "PORTER_LAUNCHER_IN_PROGRESS"
