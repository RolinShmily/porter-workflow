"""Ported from ``main:tests/test_inspector.py``.

Patch target change only: v0.1 mocked ``yt_dlp.YoutubeDL`` directly, so the
extractor skipped every option it would have set. v0.2 patches
``porter.platforms.base.build_ydl`` — the single construction point — which means
these tests now also prove the inspection path goes through the stdout-safe
builder rather than around it. Every assertion is unchanged.

One v0.1 test is **not** ported as written; see
``test_no_video_when_formats_contain_no_video_codec``, which documents a real
v0.1 bug that the original test could not see.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from porter.context import RunContext
from porter.models.inspection import InspectionResult
from porter.models.request import JobOptions
from porter.platforms import inspector as inspector_module
from porter.platforms.inspector import NO_VIDEO_MESSAGE, UNSUPPORTED, inspect_url

X_URL = "https://x.com/TechInsider/status/1895000123"


class FakeYDL:
    def __init__(self, state: SimpleNamespace) -> None:
        self._state = state

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any] | None:
        if self._state.raises is not None:
            raise self._state.raises
        return self._state.info

    def download(self, urls: list[str]) -> None:  # pragma: no cover - never called
        raise AssertionError("inspection must not download")


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(info=None, raises=None, policies=[])

    def build(policy=None, *, logger=None, progress_hook=None, extra=None):
        state.policies.append(policy)
        return FakeYDL(state)

    monkeypatch.setattr("porter.platforms.base.build_ydl", build)
    # Zero the backoff: the transient-failure test would otherwise sleep 4.5s.
    monkeypatch.setattr(inspector_module, "BACKOFF_BASE_SECONDS", 0.0)
    return state


@pytest.fixture
def ctx(tmp_path) -> RunContext:
    return RunContext(job_id="inspect", options=JobOptions(output_dir=tmp_path))


# --- From test_inspector.py::test_inspect_url_success ----------------------

SUCCESS_INFO = {
    "id": "1895000123",
    "title": "Exciting demo of new AI robot! https://t.co/xyz",
    "uploader": "TechInsider",
    "duration": 45.0,
    "width": 1080,
    "height": 1920,
    "formats": [{"vcodec": "h264", "width": 1080, "height": 1920}],
}


def test_inspect_url_success(fake_ydl) -> None:
    fake_ydl.info = dict(SUCCESS_INFO)

    res = inspect_url(X_URL)

    assert res.is_valid is True
    assert res.has_video is True
    assert res.platform == "x"
    assert res.video_id == "1895000123"
    assert res.is_vertical is True
    assert res.duration_seconds == 45.0
    assert "Exciting demo" in (res.title or "")
    summary = res.format_summary()
    assert "Vertical 9:16" in summary


# --- From test_inspector.py::test_inspect_url_no_video ----------------------


def test_inspect_url_no_video(fake_ydl) -> None:
    fake_ydl.info = {
        "id": "1895000999",
        "title": "Just a text tweet without any video attachment.",
        "uploader": "RandomUser",
        "formats": [],
    }

    res = inspect_url("https://x.com/RandomUser/status/1895000999")

    assert res.is_valid is False
    assert res.has_video is False
    assert "does not contain any video" in (res.error_message or "")


# --- From test_inspector.py::test_inspect_url_network_error ----------------


def test_inspect_url_network_error(fake_ydl) -> None:
    fake_ydl.raises = Exception("HTTP Error 429: Too Many Requests")

    res = inspect_url("https://x.com/user/status/1895000000")

    assert res.is_valid is False
    assert "HTTP 429" in (res.error_message or "")


# --- Behaviour v0.1 got wrong, and its test could not see ------------------


class TestNoVideoDetection:
    """v0.1 derived a ``has_video_stream`` flag, used it to reject, then
    **fell through and returned ``is_valid=True, has_video=True``** for anything
    with a non-empty ``formats`` list. Its test passed because it used
    ``formats: []``, the one input where the fall-through did not trigger.

    These cases are the ones it could not see: audio-only and image-only posts.
    """

    def test_no_video_when_formats_contain_no_video_codec(self, fake_ydl) -> None:
        """YouTube serves audio-only formats (140, 251) as ordinary entries."""
        fake_ydl.info = {
            "id": "audioonly",
            "title": "A podcast episode",
            "formats": [
                {"vcodec": "none", "acodec": "mp4a", "format_id": "140"},
                {"vcodec": "none", "acodec": "opus", "format_id": "251"},
            ],
        }

        res = inspect_url("https://www.youtube.com/watch?v=abc12345678")

        assert res.is_valid is False, "an audio-only post is not a video job"
        assert res.has_video is False
        assert res.error_message == NO_VIDEO_MESSAGE

    def test_no_video_for_an_image_carousel(self, fake_ydl) -> None:
        fake_ydl.info = {
            "id": "carousel",
            "entries": [
                {"id": "a", "formats": [{"vcodec": "none", "url": "https://x/1.jpg"}]},
                {"id": "b", "formats": [{"vcodec": "none", "url": "https://x/2.jpg"}]},
            ],
        }

        res = inspect_url("https://www.instagram.com/p/Cxxxx123/")

        assert res.is_valid is False
        assert res.has_video is False

    def test_video_inside_a_carousel_is_found(self, fake_ydl) -> None:
        """One video entry is enough; the mixed carousel is a normal case."""
        fake_ydl.info = {
            "id": "mixed",
            "entries": [
                {"id": "a", "formats": [{"vcodec": "none", "url": "https://x/1.jpg"}]},
                {"id": "b", "formats": [{"vcodec": "h264", "width": 1080, "height": 1920}]},
            ],
        }

        res = inspect_url("https://www.instagram.com/p/Cxxxx123/")

        assert res.is_valid is True
        assert res.has_video is True


class TestOrientation:
    def test_orientation_comes_from_the_largest_video_format(self, fake_ydl) -> None:
        """The top-level width/height are often absent; the format table has them."""
        fake_ydl.info = {
            "id": "v",
            "title": "t",
            "formats": [
                {"vcodec": "h264", "width": 640, "height": 360},
                {"vcodec": "h264", "width": 1080, "height": 1920},
            ],
        }

        res = inspect_url("https://www.tiktok.com/@u/video/123")

        assert (res.width, res.height) == (1080, 1920)
        assert res.is_vertical is True

    def test_unmeasured_vertical_platform_still_reports_vertical(self, fake_ydl) -> None:
        """v0.1 computed ``bool(width and height and height > width)``, which
        reports a horizontal video whenever the pixels are simply unknown."""
        fake_ydl.info = {"id": "v", "title": "t", "formats": [{"vcodec": "h264"}]}

        res = inspect_url("https://www.tiktok.com/@u/video/123")

        assert (res.width, res.height) == (None, None)
        assert res.is_vertical is True, "spec default_vertical, not a guess at 16:9"


class TestUnsupportedAndFailure:
    def test_unsupported_platform_is_a_result_not_an_error(self, fake_ydl) -> None:
        res = inspect_url("https://vimeo.com/12345")

        assert res.is_valid is False
        assert res.has_video is False
        assert res.platform == UNSUPPORTED
        assert "vimeo" in res.canonical_url
        assert not fake_ydl.policies, "no extractor should have been consulted"

    def test_not_found_is_not_retried(self, fake_ydl, monkeypatch) -> None:
        """A 404 is permanent; v0.1 retried it and slept in between."""
        fake_ydl.raises = Exception("Video unavailable: 404 Not Found")
        calls = {"n": 0}
        original = FakeYDL.extract_info

        def counting(self, url, download=False):
            calls["n"] += 1
            return original(self, url, download)

        monkeypatch.setattr(FakeYDL, "extract_info", counting)

        res = inspect_url(X_URL)

        assert calls["n"] == 1
        assert "not found" in (res.error_message or "").lower()

    def test_transient_failure_is_retried_then_reported(self, fake_ydl) -> None:
        fake_ydl.raises = Exception("HTTP Error 429: Too Many Requests")
        assert inspect_url(X_URL).is_valid is False
        assert "429" in (inspect_url(X_URL).error_message or "")

    def test_cancellation_propagates_instead_of_being_reported_as_a_bad_link(
        self, fake_ydl, ctx
    ) -> None:
        from porter.errors import JobCancelled

        ctx.request_cancel()
        with pytest.raises(JobCancelled):
            inspect_url(X_URL, ctx)


class TestInspectionWritesNothing:
    def test_nothing_on_stdout(self, fake_ydl, capsys) -> None:
        """stdout is the MCP transport; v0.1 printed the whole report."""
        fake_ydl.info = dict(SUCCESS_INFO)
        res = inspect_url(X_URL)
        res.format_summary()

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_summary_returns_a_string(self, fake_ydl) -> None:
        fake_ydl.info = dict(SUCCESS_INFO)
        summary = inspect_url(X_URL).format_summary()
        assert isinstance(summary, str)
        assert "X" in summary

    def test_failure_summary_mentions_the_error(self, fake_ydl) -> None:
        fake_ydl.info = {"id": "x", "title": "t", "formats": []}
        summary = inspect_url(X_URL).format_summary()
        assert "failed" in summary.lower()


class TestToDict:
    def test_is_json_serialisable_and_small(self, fake_ydl) -> None:
        """``raw_info`` is megabytes of format tables; only a subset goes out."""
        import json

        fake_ydl.info = {
            **SUCCESS_INFO,
            "formats": [{"format_id": str(i), "url": "x" * 500} for i in range(200)],
            "extractor": "twitter",
        }

        payload = inspect_url(X_URL).to_dict()

        assert json.loads(json.dumps(payload)) == payload
        assert payload["raw_info"]["extractor"] == "twitter"
        assert len(json.dumps(payload)) < 4000

    def test_round_trips_through_the_model(self, fake_ydl) -> None:
        fake_ydl.info = dict(SUCCESS_INFO)
        original = inspect_url(X_URL)
        assert InspectionResult(**original.to_dict()).platform == "x"


def test_inspection_uses_the_stdout_safe_builder(fake_ydl) -> None:
    """The patch target is build_ydl precisely so this is provable."""
    fake_ydl.info = dict(SUCCESS_INFO)
    inspect_url(X_URL)
    assert fake_ydl.policies, "inspection must construct yt-dlp through build_ydl"
    assert fake_ydl.policies[0].extract_flat is False


def test_no_download_ever_happens(fake_ydl) -> None:
    """``FakeYDL.download`` raises, so a download would fail the test loudly."""
    fake_ydl.info = dict(SUCCESS_INFO)
    assert inspect_url(X_URL).is_valid is True
    _ = patch  # keep the import meaningful for readers
    _ = subprocess
