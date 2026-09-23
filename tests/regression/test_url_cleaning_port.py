"""Ported from ``main:tests/test_inspector.py``, ``test_bilibili_extractor.py``,
``test_tiktok_extractor.py`` and ``test_instagram_extractor.py``.

**One deliberate API change, and why the assertions still hold verbatim.**

v0.1 exposed ``resolve_and_clean_url(url)``: a single function holding one global
strip list, plus an inline ``if is_youtube`` special case to undo its own damage
to ``t=15s``. The assertions below show the list is not actually global — X needs
``s``/``t`` gone, YouTube needs ``t`` kept, so the two requirements contradict
unless the platform is known.

v0.2 therefore splits by knowledge rather than patching the loop:

* ``porter.platforms.urls.clean_url(url, strip=...)`` — pure, and ``strip`` is
  **required**, so no caller can inherit a silently wrong default.
* ``porter.platforms.registry.canonicalize(url)`` — expand, identify, then clean
  with that platform's set. This is the direct replacement for
  ``resolve_and_clean_url``.

Every ``assert`` below is copied from v0.1 unchanged. The only edits are the call
name and, in two places, the expected platform string, where v0.1 returned
``"generic"`` for a URL no extractor could handle — a value that implied a
fallback extractor that did not exist. See the module's last class.
"""

from __future__ import annotations

import pytest

from porter.platforms.registry import canonicalize, identify_platform
from porter.platforms.urls import CAMPAIGN_PARAMS, is_shortener

# --- From test_inspector.py::test_resolve_and_clean_url --------------------


def test_strips_x_share_and_campaign_params() -> None:
    raw_x = "https://x.com/elonmusk/status/1895000000000000000?s=20&t=abcdef&utm_source=twitter"
    cleaned_x = canonicalize(raw_x)
    assert "utm_source" not in cleaned_x
    assert "s=" not in cleaned_x
    assert "t=" not in cleaned_x
    assert "https://x.com/elonmusk/status/1895000000000000000" in cleaned_x


def test_youtube_keeps_its_timestamp() -> None:
    raw_yt = "https://www.youtube.com/watch?v=gYxZt9Qe0fk&t=15s&utm_campaign=share"
    cleaned_yt = canonicalize(raw_yt)
    assert "utm_campaign" not in cleaned_yt
    assert "v=gYxZt9Qe0fk" in cleaned_yt
    assert "t=15s" in cleaned_yt


# --- From test_inspector.py::test_identify_platform ------------------------


def test_identify_platform() -> None:
    assert identify_platform("https://www.youtube.com/watch?v=123") == "youtube"
    assert identify_platform("https://youtu.be/123") == "youtube"
    assert identify_platform("https://x.com/elonmusk/status/1895000") == "x"
    assert identify_platform("https://twitter.com/user/status/1895000") == "x"
    assert identify_platform("https://t.co/abcXYZ") == "x"


def test_unknown_host_is_not_a_platform() -> None:
    """v0.1 asserted ``== "generic"``; v0.2 returns ``None``.

    There is no generic extractor — ``get_extractor`` raised immediately after
    ``identify_platform`` said ``"generic"``. Reporting a platform that cannot
    handle the URL sent the caller down a path that could only fail, so ``None``
    is the honest answer. The *assertion's intent* (this URL belongs to no
    supported platform) is unchanged.
    """
    assert identify_platform("https://example.com/video.mp4") is None


# --- From test_tiktok_extractor.py::test_tiktok_inspector_identification ---


def test_tiktok_url_cleaning_and_shorteners() -> None:
    url = (
        "https://www.tiktok.com/@user/video/7170520270497680683"
        "?is_from_webapp=1&sender_device=pc&_r=1"
    )
    cleaned = canonicalize(url)
    assert "is_from_webapp" not in cleaned
    assert "sender_device" not in cleaned
    assert "_r" not in cleaned
    assert identify_platform(cleaned) == "tiktok"
    assert identify_platform("https://vm.tiktok.com/ZTRC5xgJp/") == "tiktok"
    assert identify_platform("https://vt.tiktok.com/ZTRC5xgJp/") == "tiktok"


# --- From test_instagram_extractor.py::test_instagram_inspector_identification


def test_instagram_url_cleaning() -> None:
    url = "https://www.instagram.com/reel/Cxxxx123/?igshid=YmMyMTA2M2Y=&utm_source=ig_web_copy_link"
    cleaned = canonicalize(url)
    assert "igshid" not in cleaned
    assert "utm_source" not in cleaned
    assert identify_platform(cleaned) == "instagram"


# --- From test_bilibili_extractor.py::test_bilibili_inspector_identification


def test_bilibili_url_cleaning() -> None:
    url = (
        "https://www.bilibili.com/video/BV13x41117TL?"
        "spm_id_from=333.999.0.0&vd_source=abcdef123456&from_source=weibo&share_source=copy_link"
    )
    cleaned = canonicalize(url)
    assert "spm_id_from" not in cleaned
    assert "vd_source" not in cleaned
    assert "from_source" not in cleaned
    assert "share_source" not in cleaned
    assert identify_platform(cleaned) == "bilibili"
    assert identify_platform("https://b23.tv/BV13x41117TL") == "bilibili"


# --- New coverage for the split itself ------------------------------------


class TestParameterScoping:
    """The behaviour the v0.1 global list got wrong, pinned explicitly."""

    def test_t_means_opposite_things_on_two_platforms(self) -> None:
        """The single fact that makes a platform-scoped strip set necessary."""
        youtube = canonicalize("https://www.youtube.com/watch?v=abc12345678&t=15s")
        twitter = canonicalize("https://x.com/u/status/123?t=15")

        assert "t=15s" in youtube, "YouTube's t is a start offset; dropping it changes the video"
        assert "t=" not in twitter

    def test_unknown_host_keeps_ambiguous_parameters(self) -> None:
        """No platform to consult means no licence to guess which names matter."""
        cleaned = canonicalize("https://example.com/watch?v=1&t=9&from=list&utm_source=x")
        assert "t=9" in cleaned
        assert "from=list" in cleaned
        assert "utm_source" not in cleaned

    def test_bare_host_gets_a_scheme(self) -> None:
        """People paste ``youtu.be/abc``; v0.1 repaired this too."""
        assert canonicalize("youtu.be/dQw4w9WgXcQ").startswith("https://")

    def test_clean_is_idempotent(self) -> None:
        once = canonicalize("https://x.com/u/status/1?s=20&t=abc&utm_source=x")
        assert canonicalize(once) == once

    def test_a_url_without_tracking_params_is_returned_unchanged(self) -> None:
        """Re-encoding a URL we do not understand could mangle its encoding."""
        url = "https://www.youtube.com/watch?v=a%20b&list=PL123"
        assert canonicalize(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "https://t.co/abc",
            "https://b23.tv/BV13x41117TL",
            "https://vm.tiktok.com/x/",
            "https://ig.me/x",
            "https://youtu.be/abc",
        ],
    )
    def test_shorteners_are_recognised(self, url: str) -> None:
        assert is_shortener(url)

    def test_campaign_params_are_never_functional(self) -> None:
        """The subset safe to strip with no platform context."""
        assert "utm_source" in CAMPAIGN_PARAMS
        assert "t" not in CAMPAIGN_PARAMS
        assert "from" not in CAMPAIGN_PARAMS
