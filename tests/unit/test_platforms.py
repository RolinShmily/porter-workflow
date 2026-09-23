"""URL identification, registry semantics, and title derivation.

Identification is the one part of the platform layer that is pure, offline and
cheap, so it is tested exhaustively. The URLs below are the real shapes the
platforms emit, including the share and shortlink variants that a
pattern-only matcher would miss.
"""

from __future__ import annotations

import pytest

from porter.errors import UnsupportedPlatformError
from porter.platforms import identify_platform, registry
from porter.platforms.base import YtDlpExtractor
from porter.platforms.bilibili import SPEC as BILIBILI
from porter.platforms.instagram import SPEC as INSTAGRAM
from porter.platforms.registry import PlatformRegistry
from porter.platforms.tiktok import SPEC as TIKTOK
from porter.platforms.titles import bilibili_title, caption_to_title, tweet_to_title
from porter.platforms.x import SPEC as X
from porter.platforms.youtube import SPEC as YOUTUBE

BUILTIN_NAMES = ("bilibili", "x", "instagram", "tiktok", "youtube")


class TestIdentification:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            # YouTube
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
            ("https://youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
            ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
            ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube"),
            ("https://www.youtube.com/embed/dQw4w9WgXcQ", "youtube"),
            ("https://www.youtube.com/v/dQw4w9WgXcQ", "youtube"),
            # X / Twitter
            ("https://x.com/user/status/1234567890", "x"),
            ("https://twitter.com/user/status/1234567890", "x"),
            ("https://mobile.twitter.com/user/status/1234567890", "x"),
            ("https://twitter.com/i/status/1234567890", "x"),
            ("https://t.co/abcdefg", "x"),
            # Instagram
            ("https://www.instagram.com/reel/ABC123/", "instagram"),
            ("https://instagram.com/reels/ABC123", "instagram"),
            ("https://www.instagram.com/p/ABC123/", "instagram"),
            ("https://www.instagram.com/tv/ABC123/", "instagram"),
            ("https://instagr.am/p/ABC123/", "instagram"),
            ("https://www.instagram.com/someuser/reel/ABC123/", "instagram"),
            ("https://ig.me/ABC123", "instagram"),
            # TikTok
            ("https://www.tiktok.com/@user/video/1234567890", "tiktok"),
            ("https://www.tiktok.com/@user/photo/1234567890", "tiktok"),
            ("https://vm.tiktok.com/ZS123abc/", "tiktok"),
            ("https://vt.tiktok.com/ZS123abc/", "tiktok"),
            ("https://www.tiktok.com/t/ZS123abc/", "tiktok"),
            ("https://www.tiktok.com/embed/v2/1234567890", "tiktok"),
            # Bilibili
            ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
            ("https://bilibili.com/video/av12345", "bilibili"),
            ("https://www.bilibili.com/bangumi/play/ep12345", "bilibili"),
            ("https://www.bilibili.com/cheese/play/ss123", "bilibili"),
            ("https://b23.tv/abc123", "bilibili"),
            ("https://www.bilibili.tv/en/play/12345", "bilibili"),
            ("https://t.bilibili.com/12345", "bilibili"),
        ],
    )
    def test_known_urls(self, url: str, expected: str) -> None:
        assert identify_platform(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://vimeo.com/12345678",
            "https://example.com/video",
            "not a url at all",
            "",
        ],
    )
    def test_unsupported_urls(self, url: str) -> None:
        assert identify_platform(url) is None

    def test_unsupported_lookup_raises_with_the_supported_list(self) -> None:
        """The error must say what *would* have worked, not only what failed."""
        with pytest.raises(UnsupportedPlatformError) as excinfo:
            registry().find("https://vimeo.com/12345678")

        assert excinfo.value.url == "https://vimeo.com/12345678"
        assert set(excinfo.value.supported) == set(BUILTIN_NAMES)
        assert "youtube" in str(excinfo.value)

    def test_host_fallback_catches_unusual_deep_links(self) -> None:
        """Patterns cannot enumerate every URL shape a platform emits."""
        assert identify_platform("https://www.bilibili.com/some/new/route") == "bilibili"
        assert identify_platform("https://www.tiktok.com/@u/live") == "tiktok"


class TestVideoId:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://x.com/u/status/1234567890", "1234567890"),
            ("https://www.instagram.com/reel/ABC123/", "ABC123"),
            ("https://www.tiktok.com/@u/video/1234567890", "1234567890"),
            ("https://www.bilibili.com/video/BV1xx411c7mD", "BV1xx411c7mD"),
        ],
    )
    def test_extracted_from_url(self, url: str, expected: str) -> None:
        assert registry().find(url).video_id(url) == expected  # type: ignore[attr-defined]

    def test_unmatched_url_yields_none(self) -> None:
        assert YOUTUBE.video_id("https://youtube.com/feed/subscriptions") is None


class TestRegistry:
    """v0.1's decorator registry double-registered; this one must not."""

    def test_builtins_are_registered(self) -> None:
        assert registry().names() == BUILTIN_NAMES

    def test_lookup_order_is_the_declared_order(self) -> None:
        """Order is a literal, not an accident of import order."""
        assert registry().names() == ("bilibili", "x", "instagram", "tiktok", "youtube")

    def test_registering_the_same_name_replaces_not_appends(self) -> None:
        reg = PlatformRegistry()
        first = YtDlpExtractor(YOUTUBE)
        reg.register(first)
        reg.register(YtDlpExtractor(YOUTUBE))
        assert len(reg) == 1
        assert reg.names() == ("youtube",)

    def test_register_is_idempotent_across_repeated_calls(self) -> None:
        from porter.platforms.registry import register_builtins

        before = registry().names()
        register_builtins()
        register_builtins()
        assert registry().names() == before

    def test_fresh_registry_is_empty(self) -> None:
        assert len(PlatformRegistry()) == 0

    def test_clear_and_unregister(self) -> None:
        reg = PlatformRegistry()
        reg.register(YtDlpExtractor(YOUTUBE))
        reg.register(YtDlpExtractor(X))
        reg.unregister("youtube")
        assert reg.names() == ("x",)
        reg.clear()
        assert len(reg) == 0

    def test_contains(self) -> None:
        assert "youtube" in registry()
        assert "vimeo" not in registry()


class TestCaptionTitles:
    """Ported from the three v0.1 cleaners; only the parameters differ."""

    def test_strips_urls(self) -> None:
        title = caption_to_title(
            "Look at this https://example.com/very/long/link",
            "u",
            fallback_prefix="TikTok_by_",
            max_length=50,
        )
        assert title == "Look at this"

    def test_strips_leading_mentions(self) -> None:
        title = caption_to_title(
            "@alice @bob the real text",
            "u",
            fallback_prefix="TikTok_by_",
            max_length=50,
        )
        assert title == "the real text"

    def test_strips_trailing_hashtags(self) -> None:
        title = caption_to_title(
            "real text #fyp #viral",
            "u",
            fallback_prefix="TikTok_by_",
            max_length=50,
        )
        assert title == "real text"

    def test_falls_back_when_the_first_line_is_only_hashtags(self) -> None:
        title = caption_to_title(
            "#fyp #viral\nactual content here",
            "u",
            fallback_prefix="TikTok_by_",
            max_length=50,
        )
        assert title == "actual content here"

    def test_fallback_uses_uploader(self) -> None:
        title = caption_to_title(
            "https://example.com",
            "someuser",
            fallback_prefix="TikTok_by_",
            max_length=50,
        )
        assert title == "TikTok_by_someuser"

    def test_fallback_without_uploader(self) -> None:
        title = caption_to_title(
            None, None, fallback_prefix="Instagram_by_", max_length=50
        )
        assert title == "Instagram_by_unknown"

    def test_truncates_to_max_length(self) -> None:
        title = caption_to_title(
            "x" * 200, "u", fallback_prefix="TikTok_by_", max_length=50
        )
        assert len(title) == 50

    def test_never_returns_empty(self) -> None:
        for caption in ("", "   ", "#tags", "https://a.b", "\n\n"):
            assert caption_to_title(
                caption, "u", fallback_prefix="TikTok_by_", max_length=50
            )

    def test_tweet_keeps_sixty_characters(self) -> None:
        title = tweet_to_title("y" * 200, "u", "123")
        assert len(title) == 60

    def test_tweet_fallback_uses_uploader_then_id(self) -> None:
        assert tweet_to_title(None, "someone", "123") == "Tweet_by_someone"
        assert tweet_to_title(None, None, "123") == "Tweet_by_123"


class TestBilibiliTitle:
    def test_strips_the_chinese_suffix(self) -> None:
        assert bilibili_title("My Video_哔哩哔哩_bilibili", "BV1") == "My Video"

    def test_strips_the_dash_variant(self) -> None:
        assert bilibili_title("My Video - 哔哩哔哩", "BV1") == "My Video"

    def test_suffix_match_is_case_insensitive(self) -> None:
        assert bilibili_title("My Video_BILIBILI", "BV1") == "My Video"

    def test_falls_back_to_the_bvid(self) -> None:
        assert bilibili_title(None, "BV1xx") == "bilibili_BV1xx"
        assert bilibili_title("_哔哩哔哩_bilibili", "BV1xx") == "bilibili_BV1xx"

    def test_plain_title_is_unchanged(self) -> None:
        assert bilibili_title("Just A Title", "BV1") == "Just A Title"


class TestSpecDeclarations:
    """The specs are data; assert the data is coherent."""

    @pytest.mark.parametrize(
        "spec", [YOUTUBE, X, INSTAGRAM, TIKTOK, BILIBILI], ids=lambda s: s.name
    )
    def test_spec_is_well_formed(self, spec) -> None:
        assert spec.name and spec.display_name
        assert spec.url_patterns, f"{spec.name} has no URL patterns"
        assert spec.url_hosts, f"{spec.name} has no host fallback"
        assert spec.format_selector
        for pattern in spec.url_patterns:
            assert pattern.pattern.startswith("^"), (
                f"{spec.name}: unanchored pattern {pattern.pattern!r} would match "
                "substrings of unrelated URLs"
            )

    def test_video_id_group_exists_in_every_pattern(self) -> None:
        for spec in (YOUTUBE, X, INSTAGRAM, TIKTOK, BILIBILI):
            for pattern in spec.url_patterns:
                assert spec.video_id_group in pattern.groupindex, (
                    f"{spec.name}: {pattern.pattern!r} has no "
                    f"{spec.video_id_group!r} group"
                )

    def test_tiktok_is_vertical_by_default(self) -> None:
        assert TIKTOK.default_vertical is True

    def test_other_platforms_are_not(self) -> None:
        for spec in (YOUTUBE, X, INSTAGRAM, BILIBILI):
            assert spec.default_vertical is False

    def test_only_youtube_declares_player_clients(self) -> None:
        assert YOUTUBE.player_clients
        for spec in (X, INSTAGRAM, TIKTOK, BILIBILI):
            assert spec.player_clients is None

    def test_bilibili_requests_its_subtitle_track(self) -> None:
        """``remote=False`` means "never fetch a subtitle", not "fetch it another way".

        Bilibili was marked ``False`` on the theory that its CC track was fetched
        over HTTP. Nothing did that, so its own exact Chinese track was discarded
        and every job paid for ASR instead. yt-dlp returns no bilibili subtitle
        without cookies, so requesting one is a no-op today -- and the only way it
        can ever be used when cookies make it visible.
        """
        assert BILIBILI.subtitles.remote is True

    def test_platforms_without_subtitle_tracks_do_not_claim_them(self) -> None:
        for spec in (X, INSTAGRAM):
            assert spec.subtitles.remote is False

    def test_rate_limit_retry_only_where_needed(self) -> None:
        assert TIKTOK.retries_on_rate_limit is True
        assert BILIBILI.retries_on_rate_limit is True
        assert YOUTUBE.retries_on_rate_limit is False
