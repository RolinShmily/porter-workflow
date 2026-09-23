"""Ported from ``main:tests/test_x_extractor.py``, ``test_tiktok_extractor.py``,
``test_instagram_extractor.py`` and ``test_bilibili_extractor.py``.

The title-cleaning assertions are held **verbatim**. Only the call form changed,
because v0.1 had four cleaners and v0.2 has two shared algorithms:

============================  ==========================================
v0.1                          v0.2
============================  ==========================================
``XExtractor._clean_tweet_title(text, uploader=u, status_id=i)``
                              ``tweet_to_title(text, u, i)``
``TikTokExtractor._clean_caption_to_title(text, uploader=u, post_id=i)``
                              ``caption_to_title(text, u, fallback_prefix="TikTok_by_", max_length=50)``
``InstagramExtractor._clean_caption_to_title(...)``
                              ``caption_to_title(..., "Instagram_by_", 50)``
``BilibiliExtractor._clean_title(text, video_id)``
                              ``bilibili_title(text, video_id)``
============================  ==========================================

The prefix and length differing per platform is exactly the finding that
justified collapsing them, so those values appear in the call rather than inside
a subclass. No expected value was edited.
"""

from __future__ import annotations

import pytest

from porter.platforms.titles import bilibili_title, caption_to_title, tweet_to_title

TIKTOK = {"fallback_prefix": "TikTok_by_", "max_length": 50}
INSTAGRAM = {"fallback_prefix": "Instagram_by_", "max_length": 50}


class TestTweetToTitle:
    """From ``test_x_extractor.py::test_x_extractor_clean_tweet_title``."""

    def test_strips_trailing_link_and_extra_lines(self) -> None:
        text = (
            "Amazing breakthrough in robotics!\n"
            "Watch full demonstration here: https://t.co/demo123"
        )
        assert tweet_to_title(text, "TechDaily", "123") == "Amazing breakthrough in robotics!"

    def test_strips_leading_mentions(self) -> None:
        text = "@sama @karpathy Incredible work on model reasoning!"
        assert tweet_to_title(text, "AIResearch", "456") == "Incredible work on model reasoning!"

    def test_empty_text_falls_back_to_uploader(self) -> None:
        assert tweet_to_title("", "ElonMusk", "789") == "Tweet_by_ElonMusk"


class TestCaptionToTitle:
    """From ``test_tiktok_extractor.py`` / ``test_instagram_extractor.py``."""

    def test_tiktok_strips_link_lines_and_tags(self) -> None:
        caption = (
            "How AI transforms modern robotics\n"
            "Full video: https://vm.tiktok.com/xyz #fyp #robotics #tech"
        )
        assert caption_to_title(caption, "TechDaily", **TIKTOK) == "How AI transforms modern robotics"

    def test_tiktok_strips_leading_mentions(self) -> None:
        caption = "@openai @deepseek Check out this amazing coding demonstration! #coding #ai"
        assert (
            caption_to_title(caption, "DevGuru", **TIKTOK)
            == "Check out this amazing coding demonstration!"
        )

    def test_tiktok_hashtags_only_falls_back_to_uploader(self) -> None:
        assert caption_to_title("#fyp #foryou #trending #viral #xyzbca", "creator", **TIKTOK) == (
            "TikTok_by_creator"
        )

    def test_tiktok_empty_falls_back_to_uploader(self) -> None:
        assert caption_to_title("", "creator2", **TIKTOK) == "TikTok_by_creator2"

    def test_instagram_strips_link_lines_and_tags(self) -> None:
        caption = (
            "How Starship achieves rapid turnaround\n"
            "Full interview: https://ig.me/xyz #SpaceX #Starship"
        )
        assert (
            caption_to_title(caption, "elonmusk", **INSTAGRAM)
            == "How Starship achieves rapid turnaround"
        )

    def test_instagram_strips_leading_mentions(self) -> None:
        caption = "@nasa @spacex Watch the hot fire test at Starbase! #rocket"
        assert (
            caption_to_title(caption, "TechDaily", **INSTAGRAM)
            == "Watch the hot fire test at Starbase!"
        )

    def test_instagram_hashtags_only_falls_back_to_uploader(self) -> None:
        assert caption_to_title("#reels #trending #viral #fyp", "creator", **INSTAGRAM) == (
            "Instagram_by_creator"
        )

    def test_instagram_empty_falls_back_to_uploader(self) -> None:
        assert caption_to_title("", "photographer", **INSTAGRAM) == "Instagram_by_photographer"


class TestBilibiliTitle:
    """From ``test_bilibili_extractor.py``."""

    def test_strips_the_bilibili_suffix(self) -> None:
        raw = "深度拆解大模型强化学习与推理优化_哔哩哔哩_bilibili"
        assert bilibili_title(raw, "BV123") == "深度拆解大模型强化学习与推理优化"

    def test_strips_the_dash_bilibili_suffix(self) -> None:
        raw = "2026最新科技趋势演讲 - 哔哩哔哩"
        assert bilibili_title(raw, "BV124") == "2026最新科技趋势演讲"

    def test_empty_falls_back_to_id(self) -> None:
        assert bilibili_title("", "BV125") == "bilibili_BV125"


class TestTitlesArePathSafe:
    """A title becomes a directory name, so this is a security boundary.

    It is a **two-stage** boundary, and the stages are worth keeping distinct:
    ``titles.py`` strips URLs and caps the length, then
    ``utils.text.sanitize_filename`` removes path separators. Asserting on
    ``caption_to_title`` alone would fail — it deliberately does *not* touch
    separators — so the assertion is on the composition that actually reaches the
    filesystem, which is what ``TaskLayout.build`` performs.
    """

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../etc/passwd",
            "a/b/c",
            'name with "quotes" and \\backslash',
            "x" * 500,
            "line\nbreak",
            "....//....//etc",
            "NUL\x00byte",
        ],
    )
    def test_the_layout_path_cannot_escape_its_root(self, hostile: str, tmp_path) -> None:
        from porter.models.materials import TaskLayout
        from porter.utils.text import sanitize_filename

        title = caption_to_title(hostile, "u", **TIKTOK)
        safe = sanitize_filename(title)

        assert "/" not in safe
        assert "\\" not in safe
        assert "\x00" not in safe

        layout = TaskLayout.build(tmp_path, "vid1", hostile)
        assert layout.task_dir.parent == tmp_path, "the task dir escaped its root"
        assert len(layout.safe_title) <= 80
