"""``porter.utils.text`` — filename safety and CJK detection.

The first class holds ``v0.1`` assertions verbatim (from
``main:tests/test_extractors.py::test_sanitize_filename``); everything after it is
new coverage for gaps that porting exposed.
"""

from __future__ import annotations

import pytest

from porter.utils.text import is_cjk, sanitize_filename, truncate


class TestSanitizeFilenamePorted:
    """From ``test_extractors.py::test_sanitize_filename``. Assertions unchanged."""

    def test_replaces_every_illegal_character(self) -> None:
        assert sanitize_filename('test / \\ : * ? " < > | name') == "test_name"

    def test_collapses_whitespace(self) -> None:
        assert sanitize_filename("  a   b   c  ") == "a_b_c"

    def test_caps_the_length(self) -> None:
        assert len(sanitize_filename("a" * 200, max_length=50)) <= 50


class TestControlCharacters:
    """v0.1's character class was ``[\\\\/*?:\"<>|\\r\\n\\t]``, which let NUL and ESC
    through. Both come from remote metadata, so both are untrusted input.
    """

    def test_nul_is_removed(self) -> None:
        """A NUL in a path raises ``ValueError`` on Linux and truncates elsewhere."""
        cleaned = sanitize_filename("NUL\x00byte")
        assert "\x00" not in cleaned
        assert cleaned == "NUL_byte"

    def test_escape_is_removed(self) -> None:
        """ESC would let a crafted title repaint the operator's terminal."""
        cleaned = sanitize_filename("plain\x1b[31mred\x1b[0m")
        assert "\x1b" not in cleaned

    def test_the_whole_c0_range_is_removed(self) -> None:
        for code in range(0x20):
            cleaned = sanitize_filename(f"a{chr(code)}b")
            assert chr(code) not in cleaned, f"U+{code:04X} survived"

    def test_del_is_removed(self) -> None:
        assert "\x7f" not in sanitize_filename("a\x7fb")

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../etc/passwd",
            "..\\..\\windows\\system32",
            "/absolute/path",
            "trailing.",
            "...",
            "   ",
            "",
        ],
    )
    def test_cannot_escape_or_degenerate(self, hostile: str) -> None:
        cleaned = sanitize_filename(hostile)
        assert "/" not in cleaned
        assert "\\" not in cleaned
        assert cleaned not in ("", ".", "..")
        assert not cleaned.startswith(".")

    def test_an_all_illegal_name_falls_back(self) -> None:
        assert sanitize_filename("///") == "video"


class TestTruncate:
    def test_short_text_is_untouched(self) -> None:
        assert truncate("hello", 10) == "hello"

    def test_long_text_gets_an_ellipsis(self) -> None:
        result = truncate("abcdefghij", 5)
        assert result.endswith("...")
        assert len(result) <= 5

    def test_exactly_at_the_limit_is_untouched(self) -> None:
        assert truncate("abcde", 5) == "abcde"


class TestIsCjk:
    @pytest.mark.parametrize("text", ["你好", "日本語", "混合 text 中文"])
    def test_detects_cjk(self, text: str) -> None:
        assert is_cjk(text) is True

    @pytest.mark.parametrize("text", ["hello", "12345", "", "café"])
    def test_rejects_non_cjk(self, text: str) -> None:
        assert is_cjk(text) is False
