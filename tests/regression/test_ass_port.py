"""Regression tests for the ASS layer and transcript writers.

Source: ``git show main:tests/test_subtitle.py`` — the assertions of
``test_save_transcript_files`` (line 188), ``test_generate_ass_styling`` (221),
``test_generate_asynchronous_bilingual_ass`` (249) and
``test_compute_adaptive_subtitle_style`` (699).

Only the import paths and the call form changed:

==================================  ===================================
v0.1                                v0.2
==================================  ===================================
``from porter_skill.subtitle.formatter import X``
                                    ``from porter.subtitles.ass import X``
``from porter_skill.subtitle.controller import compute_adaptive_subtitle_style``
                                    ``from porter.subtitles.ass import compute_adaptive_subtitle_style``
``SubtitleItem`` / ``TranscriptSentence`` (re-exported by formatter)
                                    ``from porter.models.subtitle import ...``
==================================  ===================================

**No assertion was edited.** If one of these fails, behaviour changed.

The final class holds boundary tests written for v0.2 (zero/None dimensions,
empty cue lists); these have no v0.1 counterpart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from porter.models.subtitle import SubtitleItem, TranscriptSentence
from porter.subtitles.ass import (
    compute_adaptive_subtitle_style,
    generate_bilingual_ass,
    generate_zh_ass,
)
from porter.subtitles.transcript import save_transcript_json, save_transcript_txt


class TestSaveTranscriptFiles:
    """From ``test_subtitle.py::test_save_transcript_files``. Assertions verbatim."""

    def test_writes_structured_json_and_readable_txt(self, tmp_path: Path) -> None:
        json_path = tmp_path / "transcript.json"
        txt_path = tmp_path / "transcript.txt"

        sentences = [
            TranscriptSentence(
                sentence_id=1,
                start_ms=1000,
                end_ms=4000,
                en_text="Hello world.",
                zh_text="你好世界。",
                fragment_indices=[1, 2],
            )
        ]

        save_transcript_json(sentences, json_path)
        save_transcript_txt(sentences, txt_path)

        assert json_path.exists()
        assert txt_path.exists()

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["en_text"] == "Hello world."
        assert data[0]["zh_text"] == "你好世界。"

        txt_content = txt_path.read_text(encoding="utf-8")
        assert "[1] 00:00:01,000 --> 00:00:04,000" in txt_content
        assert "EN: Hello world." in txt_content
        assert "ZH: 你好世界。" in txt_content


class TestGenerateAssStyling:
    """From ``test_subtitle.py::test_generate_ass_styling``. Assertions verbatim."""

    def test_styled_ass_with_fade_and_margin_anchoring(self) -> None:
        items = [
            SubtitleItem(
                index=1,
                start_ms=1000,
                end_ms=4500,
                source_text="Welcome to the video",
                target_text="欢迎观看本期视频",
            )
        ]

        bi_ass = generate_bilingual_ass(items)
        assert "[Script Info]" in bi_ass
        assert "[V4+ Styles]" in bi_ass
        assert "欢迎观看本期视频" in bi_ass
        assert "Welcome to the video" in bi_ass
        assert "0:00:01.00,0:00:04.50" in bi_ass
        assert "\\fad(120,120)" in bi_ass
        assert "SubtitleZh" in bi_ass
        assert "SubtitleEn" in bi_ass

        zh_ass = generate_zh_ass(items)
        assert "欢迎观看本期视频" in zh_ass
        assert "Welcome to the video" not in zh_ass
        assert "\\fad(120,120)" in zh_ass


class TestGenerateAsynchronousBilingualAss:
    """From ``test_subtitle.py::test_generate_asynchronous_bilingual_ass``."""

    def test_dual_track_independent_events(self) -> None:
        zh_items = [
            SubtitleItem(1, 1000, 7000, "", "这是整句中文翻译。"),
        ]
        en_items = [
            SubtitleItem(1, 1000, 3500, "This is part one,", ""),
            SubtitleItem(2, 3600, 7000, "and part two.", ""),
        ]

        bi_ass = generate_bilingual_ass(zh_items=zh_items, en_items=en_items)
        assert "这是整句中文翻译" in bi_ass
        assert "This is part one," in bi_ass
        assert "and part two." in bi_ass
        assert "SubtitleZh" in bi_ass
        assert "SubtitleEn" in bi_ass
        assert "\\fad(120,120)" in bi_ass


class TestComputeAdaptiveSubtitleStyle:
    """From ``test_subtitle.py::test_compute_adaptive_subtitle_style``."""

    def test_adaptive_styling_across_aspect_ratios(self) -> None:
        # 1. 1106x720 (~1.53:1 3:2/4:3-like compact screen from user screenshot)
        b_st, z_st, px, py = compute_adaptive_subtitle_style(1106, 720)
        assert px == 1106
        assert py == 720
        assert b_st.zh_font_size >= 38  # Boosted for compact screen
        assert b_st.en_font_size >= 24
        assert z_st.zh_font_size > b_st.zh_font_size  # Pure ZH is larger
        assert b_st.bilingual_zh_margin_v > b_st.bilingual_en_margin_v

        # 2. 1920x1080 (Standard 16:9 1080p)
        b_1080, z_1080, px_1080, py_1080 = compute_adaptive_subtitle_style(1920, 1080)
        assert px_1080 == 1920
        assert py_1080 == 1080
        assert b_1080.zh_font_size == 52
        assert b_1080.en_font_size == 34
        assert z_1080.zh_font_size == 58

        # 3. 1080x1920 (Vertical 9:16)
        b_vert, z_vert, px_vert, py_vert = compute_adaptive_subtitle_style(1080, 1920)
        assert px_vert == 1080
        assert py_vert == 1920
        assert b_vert.zh_font_size == 56
        assert b_vert.bilingual_zh_margin_v == 220
        assert z_vert.zh_font_size == 64

        # 4. 960x960 (Square 1:1)
        b_sq, z_sq, px_sq, py_sq = compute_adaptive_subtitle_style(960, 960)
        assert px_sq == 960
        assert py_sq == 960
        assert b_sq.zh_font_size >= 55  # Boosted for square screen
        assert z_sq.zh_font_size >= 60


class TestAssBoundaries:
    """v0.2-only boundary coverage: aspect ratios, degenerate sizes, empty cues."""

    def test_vertical_and_horizontal_produce_different_styles(self) -> None:
        """9:16 must not reuse the 16:9 margins: the text would sit mid-frame."""
        b_h, z_h, px_h, py_h = compute_adaptive_subtitle_style(1920, 1080)
        b_v, z_v, px_v, py_v = compute_adaptive_subtitle_style(1080, 1920)

        assert (px_h, py_h) == (1920, 1080)
        assert (px_v, py_v) == (1080, 1920)
        assert b_v.zh_font_size != b_h.zh_font_size
        assert b_v.bilingual_zh_margin_v != b_h.bilingual_zh_margin_v
        assert z_v.margin_v != z_h.margin_v

    def test_zero_dimensions_clamp_to_engine_minimum(self) -> None:
        """A failed probe yields 0x0; it must not divide by zero or emit a 0 canvas."""
        bilingual, zh_style, px, py = compute_adaptive_subtitle_style(0, 0)
        assert (px, py) == (320, 240)
        assert bilingual.zh_font_size > 0
        assert zh_style.zh_font_size > 0

    def test_none_base_style_falls_back_to_defaults(self) -> None:
        """``base_style=None`` is the documented default, not a crash."""
        bilingual, zh_style, px, py = compute_adaptive_subtitle_style(
            1920, 1080, base_style=None
        )
        assert (px, py) == (1920, 1080)
        assert bilingual.zh_font_size == 52
        assert zh_style.zh_font_size == 58

    def test_none_dimensions_are_rejected(self) -> None:
        """``None`` is outside the ``int`` contract; fail loudly rather than guess."""
        with pytest.raises(TypeError):
            compute_adaptive_subtitle_style(None, None)  # type: ignore[arg-type]

    def test_no_items_emits_a_valid_empty_script(self) -> None:
        """``items=None`` / ``[]`` must yield a parseable header, not an exception."""
        bi_ass = generate_bilingual_ass()
        assert "[Script Info]" in bi_ass
        assert "[V4+ Styles]" in bi_ass
        assert "[Events]" in bi_ass
        assert "PlayResX: 1920" in bi_ass
        assert "Dialogue:" not in bi_ass
        assert bi_ass.endswith("\n")

        empty_bi = generate_bilingual_ass([])
        assert empty_bi == bi_ass

        empty_zh = generate_zh_ass([])
        assert "[Script Info]" in empty_zh
        assert "[Events]" in empty_zh
        assert "Dialogue:" not in empty_zh
        assert empty_zh.endswith("\n")
