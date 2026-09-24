"""Tests for ``packaging/launcher.py``.

The launcher is the thing users actually download, and until now nothing tested
it. That gap shipped a broken binary: ``_create_venv`` passed ``sys.executable``
to ``uv venv --python``, which is a Python only when the file runs as a script.
Frozen by PyInstaller it is the launcher binary itself, and ``uv`` inspects a
``--python`` path by *executing* it -- so the probe re-entered the launcher, the
launcher called ``uv`` again, and ``porter.exe --version`` on a clean machine
became 970 nested retries taking 219 seconds before failing.

Three things are therefore pinned here:

* **The bug** -- a frozen launcher must never offer itself as an interpreter.
* **The blast radius** -- re-entry must fail immediately and legibly rather than
  recursing, because that is what turned one wrong argument into a fork bomb.
* **Where packages come from** -- the mirror decision, including the two copies
  of the China heuristic being kept in step.
"""

from __future__ import annotations

import importlib.util
import locale
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from porter import mirrors

LAUNCHER_PATH = Path(__file__).resolve().parents[2] / "packaging" / "launcher.py"


def _load_launcher() -> Any:
    """Import ``packaging/launcher.py`` by path; it is not an installed module.

    Each call *re-executes* the file and hands back a fresh module object, so
    everything that patches the launcher must share one instance: patching one
    and asserting on another compares a patched copy against an unpatched one.
    The ``launcher`` fixture below is that single instance.
    """
    spec = importlib.util.spec_from_file_location("porter_launcher", LAUNCHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def launcher() -> Any:
    """One module instance for the whole file. See ``_load_launcher``."""
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


#: Every variable the index decision reads, so each test starts from nothing set.
_INDEX_KEYS = (
    "PORTER_MIRROR",
    "UV_DEFAULT_INDEX",
    "UV_INDEX_URL",
    "PIP_INDEX_URL",
    "TZ",
)

USTC = "https://mirrors.ustc.edu.cn/pypi/simple"
TSINGHUA = "https://pypi.tuna.tsinghua.edu.cn/simple"
ALIYUN = "https://mirrors.aliyun.com/pypi/simple/"


class TestIndexMirror:
    """Where the launcher sends package downloads.

    Every assertion reads a child environment built by ``_child_env``, never a
    hand-made dict: ``_apply_index_mirror`` looks at the environment it is
    *given*, so passing it ``{}`` cannot see the user's own variables. A test
    written that way passes while the real path does the wrong thing -- which is
    how the first version of this class was written, and why it is spelled out.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in _INDEX_KEYS:
            monkeypatch.delenv(key, raising=False)

    @staticmethod
    def _child(launcher: Any) -> tuple[str, str]:
        """``(uv index, pip index)`` exactly as a spawned child would see them."""
        child = launcher._child_env()
        return child.get("UV_DEFAULT_INDEX", ""), child.get("PIP_INDEX_URL", "")

    def test_a_machine_elsewhere_is_left_on_pypi(
        self, monkeypatch: pytest.MonkeyPatch, launcher: Any
    ) -> None:
        monkeypatch.setenv("TZ", "America/New_York")
        assert self._child(launcher) == ("", "")

    def test_a_chinese_machine_gets_ustc(
        self, monkeypatch: pytest.MonkeyPatch, launcher: Any
    ) -> None:
        monkeypatch.setenv("PORTER_MIRROR", "cn")
        # pip as well as uv, because which of the two runs depends only on
        # whether uv happens to be on PATH.
        assert self._child(launcher) == (USTC, USTC)

    def test_a_user_pip_index_is_passed_to_uv(
        self, monkeypatch: pytest.MonkeyPatch, launcher: Any
    ) -> None:
        # The regression this block exists for: uv ignores PIP_INDEX_URL, so a
        # user who configured a mirror the way every Chinese guide tells them to
        # got no mirror, no error, and no explanation.
        monkeypatch.setenv("PIP_INDEX_URL", TSINGHUA)
        monkeypatch.setenv("PORTER_MIRROR", "cn")

        uv, pip = self._child(launcher)
        assert uv == TSINGHUA, "uv must be told too, or the mirror has no effect"
        assert pip == TSINGHUA
        assert USTC not in (uv, pip), "the user's own index must win"

    @pytest.mark.parametrize("key", ["UV_DEFAULT_INDEX", "UV_INDEX_URL"])
    def test_a_user_uv_index_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, launcher: Any, key: str
    ) -> None:
        monkeypatch.setenv(key, ALIYUN)
        monkeypatch.setenv("PORTER_MIRROR", "cn")

        uv, pip = self._child(launcher)
        assert uv == (ALIYUN if key == "UV_DEFAULT_INDEX" else "")
        assert pip == "", "no index the user did not ask for"

    def test_off_wins_over_a_chinese_machine(
        self, monkeypatch: pytest.MonkeyPatch, launcher: Any
    ) -> None:
        monkeypatch.setenv("PORTER_MIRROR", "off")
        assert self._child(launcher) == ("", "")

    def test_the_marker_still_rides_along(
        self, monkeypatch: pytest.MonkeyPatch, launcher: Any
    ) -> None:
        # _apply_index_mirror is only useful because _child_env, which every
        # spawned process goes through, calls it -- and it must not have lost the
        # re-entry marker on the way.
        monkeypatch.setenv("PORTER_MIRROR", "cn")
        child = launcher._child_env()
        assert child["PORTER_LAUNCHER_IN_PROGRESS"] == "1"
        assert child["UV_DEFAULT_INDEX"] == USTC


class TestTheTwoDetectionCopiesAgree:
    """``packaging/launcher.py`` cannot import ``porter.mirrors``, so it copies it.

    The launcher has to run *before* porter exists -- installing it is the
    launcher's whole job -- so an import is not available to it. Duplication is
    only safe if it is checked, hence this class: both copies are driven through
    the identical environment and clock, and any disagreement fails. Without it
    the two heuristics would drift, and the launcher would send a machine to a
    mirror that the engine then declines to use.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in _INDEX_KEYS:
            monkeypatch.delenv(key, raising=False)

    @staticmethod
    def _ask_both(
        monkeypatch: pytest.MonkeyPatch,
        launcher: Any,
        *,
        tzname: str,
        offset: int,
        language: str | None,
    ) -> tuple[bool, bool]:
        """``(launcher's answer, porter.mirrors' answer)`` for one machine."""
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone(timedelta(hours=offset), tzname))
        monkeypatch.setattr(launcher, "_local_now", lambda: now)
        monkeypatch.setattr(mirrors, "_local_now", lambda: now)
        # One patch covers both copies: they reference the same module object.
        monkeypatch.setattr(locale, "getlocale", lambda: (language, "UTF-8"))
        return launcher._use_china_mirrors(), mirrors.use_china_mirrors()

    @pytest.mark.parametrize(
        ("tz", "tzname", "offset", "language"),
        [
            (None, "UTC", 0, "en_US"),
            (None, "Eastern Standard Time", -5, "en_US"),
            (None, "China Standard Time", 8, "en_US"),
            (None, "\u4e2d\u56fd\u6807\u51c6\u65f6\u95f4", 8, None),
            (None, "UTC", 0, "zh_CN"),
            (None, "UTC", 0, "Chinese (Simplified)_China"),
            (None, "UTC", 0, None),
            ("Asia/Shanghai", "UTC", 0, "en_US"),
            ("America/New_York", "China Standard Time", 8, "zh_CN"),
            ("Europe/Berlin", "UTC", 0, "de_DE"),
            ("PRC", "UTC", 0, None),
        ],
    )
    def test_they_agree(
        self,
        monkeypatch: pytest.MonkeyPatch,
        launcher: Any,
        tz: str | None,
        tzname: str,
        offset: int,
        language: str | None,
    ) -> None:
        if tz is not None:
            monkeypatch.setenv("TZ", tz)

        from_launcher, from_engine = self._ask_both(
            monkeypatch, launcher, tzname=tzname, offset=offset, language=language
        )
        assert from_launcher == from_engine, (
            f"the launcher and porter.mirrors disagree for "
            f"{tz=} {tzname=} {offset=} {language=}"
        )

    @pytest.mark.parametrize("value", ["cn", "off", "chian"])
    def test_they_agree_on_the_override_too(
        self, monkeypatch: pytest.MonkeyPatch, launcher: Any, value: str
    ) -> None:
        monkeypatch.setenv("PORTER_MIRROR", value)
        from_launcher, from_engine = self._ask_both(
            monkeypatch, launcher, tzname="UTC", offset=0, language=None
        )
        assert from_launcher == from_engine

    def test_the_constants_match(self, launcher: Any) -> None:
        assert launcher.USTC_PYPI_INDEX == mirrors.USTC_PYPI_INDEX
        assert launcher.MIRROR_ENV == mirrors.ENV_OVERRIDE
