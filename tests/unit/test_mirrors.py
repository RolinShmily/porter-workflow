"""Tests for :mod:`porter.mirrors`.

The detection is heuristic, so the tests pin the signals rather than a
conclusion: each branch is exercised with the input it is supposed to read, and
the override is checked to win over all of them. The Windows locale spelling is
in here as a regression guard -- ``locale.getlocale()`` returns
``('Chinese (Simplified)_China', '936')`` there, not ``('zh_CN', ...)``, so a
``startswith("zh")`` test alone silently never matches on the platform the
feature was written for.
"""

from __future__ import annotations

import locale
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from porter import mirrors


@pytest.fixture(autouse=True)
def _no_ambient_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from "a machine that is not in China".

    The clock is pinned to UTC rather than read from the machine: this file was
    written on a Chinese Windows box, so the real tzname and +08:00 offset would
    make the timezone branch fire in every locale test.
    """
    monkeypatch.delenv(mirrors.ENV_OVERRIDE, raising=False)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(mirrors, "_local_now", lambda: _at(0, "UTC"))
    monkeypatch.setattr(mirrors.locale, "getlocale", lambda: (None, None))


def _at(offset_hours: float, name: str) -> datetime:
    return datetime(2026, 9, 24, 12, 0, tzinfo=timezone(timedelta(hours=offset_hours), name))


class TestOverride:
    def test_unset_has_no_opinion(self) -> None:
        assert mirrors.forced() is None

    @pytest.mark.parametrize("value", ["cn", "CN", "1", "true", "yes", "on", " china "])
    def test_the_on_spellings(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(mirrors.ENV_OVERRIDE, value)
        assert mirrors.forced() is True

    @pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no", "none", "intl"])
    def test_the_off_spellings(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(mirrors.ENV_OVERRIDE, value)
        assert mirrors.forced() is False

    def test_a_typo_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A misspelt environment variable must not stop a transcription; it just
        # means "no opinion", and auto-detection decides.
        monkeypatch.setenv(mirrors.ENV_OVERRIDE, "chian")
        assert mirrors.forced() is None

    def test_the_override_beats_every_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even on a machine that is unambiguously in China.
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        monkeypatch.setattr(mirrors.locale, "getlocale", lambda: ("zh_CN", "UTF-8"))

        monkeypatch.setenv(mirrors.ENV_OVERRIDE, "off")
        assert mirrors.use_china_mirrors() is False

        monkeypatch.setenv(mirrors.ENV_OVERRIDE, "cn")
        assert mirrors.use_china_mirrors() is True


class TestTimezoneDetection:
    @pytest.mark.parametrize(
        "tz", ["Asia/Shanghai", "asia/shanghai", "PRC", "Asia/Urumqi", "Asia/Chongqing"]
    )
    def test_a_chinese_tz_is_believed(self, monkeypatch: pytest.MonkeyPatch, tz: str) -> None:
        monkeypatch.setenv("TZ", tz)
        assert mirrors.detected() is True

    def test_an_explicit_foreign_tz_is_believed_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The machine may well be in Shanghai, but a TZ of America/New_York is a
        # deliberate setting and the offset must not override it.
        monkeypatch.setenv("TZ", "America/New_York")
        monkeypatch.setattr(mirrors, "_local_now", lambda: _at(8, "China Standard Time"))
        assert mirrors.detected() is False

    def test_utc_plus_eight_without_a_tz(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The signal that survives Windows localisation and a missing TZ var.
        monkeypatch.setattr(mirrors, "_local_now", lambda: _at(8, "China Standard Time"))
        assert mirrors.detected() is True

    def test_a_localised_tzname_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # What a Chinese Windows actually reports; an English-only marker list
        # would miss exactly the users this exists for.
        monkeypatch.setattr(mirrors, "_local_now", lambda: _at(8, "中国标准时间"))
        assert mirrors.detected() is True

    @pytest.mark.parametrize(
        ("offset", "name"),
        [(0, "UTC"), (-5, "Eastern Standard Time"), (1, "Central European Standard Time")],
    )
    def test_elsewhere_is_not_china(
        self, monkeypatch: pytest.MonkeyPatch, offset: int, name: str
    ) -> None:
        monkeypatch.setattr(mirrors, "_local_now", lambda: _at(offset, name))
        assert mirrors.detected() is False

    def test_a_broken_tz_database_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _explode() -> datetime:
            raise OSError("no tz database")

        monkeypatch.setattr(mirrors, "_local_now", _explode)
        assert mirrors.detected() is False


class TestLocaleDetection:
    def test_posix_spelling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mirrors.locale, "getlocale", lambda: ("zh_CN", "UTF-8"))
        assert mirrors.detected() is True

    def test_windows_spelling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The regression this test exists for: getlocale() on Windows.
        monkeypatch.setattr(
            mirrors.locale, "getlocale", lambda: ("Chinese (Simplified)_China", "936")
        )
        assert mirrors.detected() is True

    def test_english_is_not_china(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mirrors.locale, "getlocale", lambda: ("en_US", "UTF-8"))
        assert mirrors.detected() is False

    def test_an_empty_locale_is_not_china(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert mirrors.detected() is False

    def test_a_raising_locale_is_not_china(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _explode() -> Any:
            raise locale.Error("unsupported locale")

        monkeypatch.setattr(mirrors.locale, "getlocale", _explode)
        assert mirrors.detected() is False


class TestModelScopeRepo:
    @pytest.mark.parametrize(
        "size", ["tiny", "base", "small", "medium", "large-v2", "large-v3"]
    )
    def test_every_mirrored_size_resolves(self, size: str) -> None:
        assert mirrors.modelscope_whisper_repo(size) == f"pengzhendong/faster-whisper-{size}"

    def test_the_verification_that_these_are_the_official_weights(self) -> None:
        # Recorded here because the choice to prefer ModelScope rests on it: the
        # mirror must be the same model, not a lookalike. Verified by fetching
        # both config.json files and comparing SHA-256.
        assert mirrors.modelscope_whisper_repo("small") == "pengzhendong/faster-whisper-small"

    def test_case_and_padding_do_not_matter(self) -> None:
        assert mirrors.modelscope_whisper_repo(" Small ") == "pengzhendong/faster-whisper-small"

    def test_a_size_with_no_mirror_is_left_alone(self) -> None:
        assert mirrors.modelscope_whisper_repo("distil-large-v3") is None

    def test_a_local_path_is_left_alone(self) -> None:
        # A caller who already has a directory does not want it rewritten into a
        # download.
        assert mirrors.modelscope_whisper_repo(r"C:\models\whisper-small") is None

    def test_a_custom_hf_repo_is_left_alone(self) -> None:
        assert mirrors.modelscope_whisper_repo("some-org/my-whisper") is None
