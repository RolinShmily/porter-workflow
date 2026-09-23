"""Regression tests ported from v0.1, asserting identical behaviour.

Source: ``git show main:tests/test_extractors.py`` (lines 43-101) and
``git show main:tests/test_bilibili_extractor.py``.

Only the import paths and the call form changed:

===============================  ================================
v0.1                             v0.2
===============================  ================================
``YouTubeExtractor()._select_source_subtitle_lang(subs, is_auto=True)``
                                 ``select_source_lang(subs, is_auto=True)``
``YouTubeExtractor()._select_chinese_subtitle_lang(subs)``
                                 ``select_chinese_lang(subs)``
``from porter_skill.extractors.youtube import _convert_vtt_to_srt``
                                 ``from porter.subtitles.srt import vtt_to_srt``
===============================  ================================

**No assertion was edited.** If one of these fails, behaviour changed.
"""

from __future__ import annotations

from porter.models.subtitle import SubtitleItem
from porter.subtitles.phrasing import (
    align_bilingual_items,
    has_chinese_translation,
    normalize_subtitle_items,
)
from porter.subtitles.srt import (
    bilibili_json_to_srt,
    generate_bilingual_srt,
    generate_zh_srt,
    parse_srt,
    select_chinese_lang,
    select_source_lang,
    vtt_to_srt,
)


class TestSourceSubtitleSelection:
    """Ported from ``test_youtube_extractor_subtitles_selection``."""

    def test_prioritises_original_track_in_auto_captions(self) -> None:
        auto_subs = {
            "en-orig": [{"ext": "vtt"}],
            "zh-Hans": [{"ext": "vtt"}],
            "ja": [{"ext": "vtt"}],
        }
        assert select_source_lang(auto_subs, is_auto=True) == "en-orig"

    def test_standard_source_language_selection(self) -> None:
        subs = {"fr": [{"ext": "vtt"}], "en": [{"ext": "vtt"}]}
        assert select_source_lang(subs, is_auto=False) == "en"

    def test_empty_returns_none(self) -> None:
        assert select_source_lang({}) is None

    def test_original_suffix_variants(self) -> None:
        assert select_source_lang({"de-original": [{}], "en": [{}]}) == "de-original"

    def test_a_dubbed_video_is_disambiguated_by_its_declared_language(self) -> None:
        """YouTube auto-dubbing makes ``*-orig`` non-unique: one per language.

        Real case: an English video (``language: en-US``) whose caption map
        lists ``ar-orig`` at index 6 and ``en-orig`` at index 160. Returning the
        first in document order burned Arabic subtitles over an English video,
        and because a Chinese track existed the plan then reported "translation
        not needed" -- so no English appeared anywhere in the output.
        """
        auto_subs = {
            "ar-orig": [{"ext": "vtt"}],
            "ar": [{"ext": "vtt"}],
            "en-orig": [{"ext": "vtt"}],
            "zh-Hans": [{"ext": "vtt"}],
        }
        assert select_source_lang(auto_subs, is_auto=True, declared_lang="en-US") == "en-orig"

    def test_the_source_preference_decides_when_no_language_is_declared(self) -> None:
        """Document order is random, so the priority list is used before it."""
        auto_subs = {"ar-orig": [{}], "en-orig": [{}]}
        assert select_source_lang(auto_subs, is_auto=True) == "en-orig"

    def test_a_declared_non_english_language_beats_the_priority_list(self) -> None:
        """The one case the priority list gets wrong, so the tiebreak matters.

        An Arabic video with auto-dubbing carries both ``ar-orig`` and
        ``en-orig``. ``SOURCE_LANG_PRIORITY`` leads with ``en``, so without the
        declared language this returns English subtitles for an Arabic video.
        """
        auto_subs = {"ar-orig": [{}], "en-orig": [{}]}
        assert select_source_lang(auto_subs, is_auto=True, declared_lang="ar") == "ar-orig"

    def test_a_declared_language_matching_no_track_falls_through(self) -> None:
        auto_subs = {"ar-orig": [{}], "en-orig": [{}]}
        assert select_source_lang(auto_subs, is_auto=True, declared_lang="fr-FR") == "en-orig"

    def test_document_order_remains_the_final_fallback(self) -> None:
        """Neither ``ar`` nor ``sw`` is preferred, so the first one is taken."""
        auto_subs = {"ar-orig": [{}], "sw-orig": [{}]}
        assert select_source_lang(auto_subs, is_auto=True) == "ar-orig"

    def test_auto_captions_do_not_fall_back_to_arbitrary_track(self) -> None:
        """A non-priority auto track is a poor source, so it is rejected."""
        assert select_source_lang({"sw": [{"ext": "vtt"}]}, is_auto=True) is None

    def test_human_track_falls_back_to_document_order(self) -> None:
        assert select_source_lang({"sw": [{"ext": "vtt"}]}, is_auto=False) == "sw"

    def test_live_pseudo_tracks_are_skipped(self) -> None:
        subs = {"live_chat": [{"ext": "json"}], "sw": [{"ext": "vtt"}]}
        assert select_source_lang(subs, is_auto=False) == "sw"

    def test_empty_format_list_is_not_a_valid_track(self) -> None:
        assert select_source_lang({"en": []}, is_auto=False) is None


class TestChineseSubtitleSelection:
    """Ported from ``test_youtube_extractor_chinese_subtitles_selection``."""

    def test_prioritises_simplified(self) -> None:
        subs = {
            "en": [{"ext": "vtt"}],
            "zh-Hans": [{"ext": "vtt"}],
            "zh-Hant": [{"ext": "vtt"}],
        }
        assert select_chinese_lang(subs) == "zh-Hans"

    def test_falls_back_to_traditional(self) -> None:
        subs = {"zh-Hant": [{"ext": "vtt"}], "ja": [{"ext": "vtt"}]}
        assert select_chinese_lang(subs) == "zh-Hant"

    def test_empty_returns_none(self) -> None:
        assert select_chinese_lang({}) is None

    def test_non_chinese_only_returns_none(self) -> None:
        assert select_chinese_lang({"en": [{"ext": "vtt"}]}) is None


class TestVttConversion:
    """Ported from ``test_convert_vtt_to_srt``."""

    def test_strips_markup_and_joins_lines(self) -> None:
        vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.500 --> 00:00:04.000
<c>Hello</c> <c.yellow>world!</c>

00:00:04.200 --> 00:00:08.500
This is a test subtitle.
Second line.
"""
        srt = vtt_to_srt(vtt)
        assert "1\n00:00:01,500 --> 00:00:04,000\nHello world!" in srt
        assert "2\n00:00:04,200 --> 00:00:08,500\nThis is a test subtitle. Second line." in srt

    def test_hourless_timestamps_get_an_hour_field(self) -> None:
        """VTT permits ``MM:SS.mmm``; SRT requires ``HH:MM:SS,mmm``."""
        srt = vtt_to_srt("WEBVTT\n\n01:02.500 --> 01:04.000\nhi\n")
        assert "00:01:02,500 --> 00:01:04,000" in srt

    def test_empty_input_returns_empty_string(self) -> None:
        assert vtt_to_srt("WEBVTT\n\n") == ""

    def test_crlf_is_normalised(self) -> None:
        srt = vtt_to_srt("WEBVTT\r\n\r\n00:00:01.000 --> 00:00:02.000\r\nhi\r\n")
        assert "00:00:01,000 --> 00:00:02,000" in srt
        assert "\r" not in srt

    def test_output_is_stable_across_repeated_calls(self) -> None:
        """Guards the module-level compiled regexes against state leakage."""
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c>a</c>\n"
        assert vtt_to_srt(vtt) == vtt_to_srt(vtt)


class TestBilibiliJsonConversion:
    """Ported from ``test_bilibili_extractor.py``."""

    def test_seconds_become_srt_timestamps(self) -> None:
        payload = '{"body": [{"from": 1.5, "to": 4.0, "content": "你好"}]}'
        srt = bilibili_json_to_srt(payload)
        assert "1\n00:00:01,500 --> 00:00:04,000\n你好" in srt

    def test_zero_length_cue_is_widened_to_100ms(self) -> None:
        payload = '{"body": [{"from": 2.0, "to": 2.0, "content": "x"}]}'
        assert "00:00:02,000 --> 00:00:02,100" in bilibili_json_to_srt(payload)

    def test_negative_start_is_clamped(self) -> None:
        payload = '{"body": [{"from": -1.0, "to": 1.0, "content": "x"}]}'
        assert "00:00:00,000 --> 00:00:01,000" in bilibili_json_to_srt(payload)

    def test_empty_body_returns_empty_string(self) -> None:
        assert bilibili_json_to_srt('{"body": []}') == ""

    def test_malformed_json_returns_empty_string(self) -> None:
        """Must degrade to 'no CC track' so the ASR chain can take over."""
        assert bilibili_json_to_srt("{ not json") == ""

    def test_non_object_payload_returns_empty_string(self) -> None:
        assert bilibili_json_to_srt("[1, 2]") == ""

    def test_non_list_body_returns_empty_string(self) -> None:
        assert bilibili_json_to_srt('{"body": "nope"}') == ""

    def test_non_dict_entries_are_skipped(self) -> None:
        payload = '{"body": ["junk", {"from": 1.0, "to": 2.0, "content": "ok"}]}'
        srt = bilibili_json_to_srt(payload)
        assert srt.count("-->") == 1
        assert "ok" in srt

    def test_entries_missing_fields_are_skipped(self) -> None:
        payload = '{"body": [{"from": 1.0, "content": "no end"}, {"from": 1.0, "to": 2.0, "content": ""}]}'
        assert bilibili_json_to_srt(payload) == ""


# ----------------------------------------------------------------------
# Ported from tests/test_subtitle.py (main branch)
# ----------------------------------------------------------------------
#
# These were the SRT-layer assertions of v0.1's ``test_time_conversions``,
# ``test_parse_and_generate_srt``, ``test_parse_srt_monolingual_multiline`` and
# ``test_align_bilingual_items``. They live here rather than in a new file
# because they assert the same layer this module already covers: the pure
# subtitle format functions.
#
# The functions themselves were ported in P3, having been missed by P2.1 —
# ``docs/REFACTOR_PLAN.md`` §4.5 had assigned them to ``subtitles/srt.py`` and
# ``subtitles/phrasing.py``, but only the VTT/Bilibili converters were moved.


class TestTimeConversions:
    """From ``test_subtitle.py::test_time_conversions``. Assertions verbatim."""

    def test_round_trips_through_srt_and_ass(self) -> None:
        from porter.utils.time import ms_to_ass_time, ms_to_srt_time, srt_time_to_ms

        ms = srt_time_to_ms("01:23:45,678")
        assert ms == 5025678
        assert ms_to_srt_time(ms) == "01:23:45,678"
        assert ms_to_ass_time(ms) == "1:23:45.67"

    def test_srt_accepts_a_dot_as_the_millisecond_separator(self) -> None:
        """WebVTT and some tools write a period where SRT writes a comma."""
        from porter.utils.time import srt_time_to_ms

        assert srt_time_to_ms("00:00:01.500") == 1500

    def test_a_malformed_timestamp_is_zero_not_an_exception(self) -> None:
        """This parses remote output; one bad cue must not discard the rest."""
        from porter.utils.time import srt_time_to_ms

        assert srt_time_to_ms("garbage") == 0


class TestParseAndGenerateSrt:
    """From ``test_subtitle.py::test_parse_and_generate_srt``."""

    SAMPLE = """1
00:00:01,000 --> 00:00:04,000
你好世界
Hello World

2
00:00:05,500 --> 00:00:08,200
这是第二行
This is line 2
"""

    def test_cjk_first_line_becomes_target_text(self) -> None:
        items = parse_srt(self.SAMPLE)
        assert len(items) == 2
        assert items[0].start_ms == 1000
        assert items[0].end_ms == 4000
        assert items[0].target_text == "你好世界"
        assert items[0].source_text == "Hello World"

    def test_bilingual_generation_keeps_both_lines(self) -> None:
        bilingual = generate_bilingual_srt(parse_srt(self.SAMPLE))
        assert "你好世界\nHello World" in bilingual

    def test_chinese_generation_drops_the_source_line(self) -> None:
        zh = generate_zh_srt(parse_srt(self.SAMPLE))
        assert "你好世界" in zh
        assert "Hello World" not in zh

    def test_english_first_line_is_also_handled(self) -> None:
        """Both orders occur; the parser decides by looking for CJK, not by order."""
        srt = "1\n00:00:01,000 --> 00:00:02,000\nHello World\n你好世界\n"
        items = parse_srt(srt)
        assert items[0].source_text == "Hello World"
        assert items[0].target_text == "你好世界"


class TestParseMonolingualMultiline:
    """From ``test_subtitle.py::test_parse_srt_monolingual_multiline``."""

    SAMPLE = """1
00:00:01,000 --> 00:00:04,000
This is a long sentence
that was wrapped across two lines.
"""

    def test_wrapped_lines_join_with_a_space(self) -> None:
        items = parse_srt(self.SAMPLE)
        assert len(items) == 1
        assert items[0].source_text == (
            "This is a long sentence that was wrapped across two lines."
        )
        assert items[0].target_text == ""

    def test_wrapped_chinese_lines_join_without_a_space(self) -> None:
        """A space between Chinese characters is visible in the rendered video."""
        srt = "1\n00:00:01,000 --> 00:00:02,000\n这是第一句\n这是第二句\n"
        assert parse_srt(srt)[0].source_text == "这是第一句这是第二句"


class TestAlignBilingualItems:
    """From ``test_subtitle.py::test_align_bilingual_items``."""

    def test_equal_counts_align_positionally(self) -> None:
        en_items = [
            SubtitleItem(1, 0, 4000, "Hello world", ""),
            SubtitleItem(2, 4000, 8000, "Goodbye world", ""),
        ]
        zh_items = [
            SubtitleItem(1, 0, 4000, "你好世界", ""),
            SubtitleItem(2, 4000, 8000, "再见世界", ""),
        ]
        aligned = align_bilingual_items(en_items, zh_items)

        assert len(aligned) == 2
        assert aligned[0].target_text == "你好世界"
        assert aligned[1].target_text == "再见世界"

    def test_unequal_counts_fall_back_to_overlap(self) -> None:
        """Chinese cues are usually shorter, so equal counts is not the norm."""
        en_items = [SubtitleItem(1, 0, 4000, "Hello world", "")]
        zh_items = [
            SubtitleItem(1, 0, 2000, "你好", ""),
            SubtitleItem(2, 2000, 4000, "世界", ""),
        ]
        aligned = align_bilingual_items(en_items, zh_items)
        assert aligned[0].target_text == "你好世界"

    def test_no_chinese_leaves_the_source_untouched(self) -> None:
        en_items = [SubtitleItem(1, 0, 4000, "Hello world", "")]
        assert align_bilingual_items(en_items, [])[0].target_text == ""


class TestOverlapRepair:
    """``normalize_subtitle_items``, which both parsers and the pipeline call."""

    def test_an_overlapping_cue_is_clipped_not_dropped(self) -> None:
        items = [
            SubtitleItem(1, 1000, 3000, "first", ""),
            SubtitleItem(2, 2500, 5000, "second", ""),
        ]
        fixed = normalize_subtitle_items(items)

        assert [(item.start_ms, item.end_ms) for item in fixed] == [(1000, 2500), (2500, 5000)]
        assert len(fixed) == 2, "no cue may be discarded to resolve an overlap"

    def test_a_zero_length_last_cue_is_widened(self) -> None:
        """v0.1's loop never examined the final cue, so this survived it."""
        items = [
            SubtitleItem(1, 1000, 2000, "first", ""),
            SubtitleItem(2, 5000, 5000, "last", ""),
        ]
        fixed = normalize_subtitle_items(items)
        assert fixed[-1].end_ms > fixed[-1].start_ms

    def test_items_are_sorted_by_time(self) -> None:
        items = [
            SubtitleItem(1, 5000, 6000, "later", ""),
            SubtitleItem(2, 1000, 2000, "earlier", ""),
        ]
        assert [item.source_text for item in normalize_subtitle_items(items)] == [
            "earlier",
            "later",
        ]

    def test_indices_are_renumbered_contiguously(self) -> None:
        """A gapped or duplicated index makes some players reject the file."""
        items = [
            SubtitleItem(7, 3000, 4000, "b", ""),
            SubtitleItem(7, 1000, 2000, "a", ""),
        ]
        assert [item.index for item in normalize_subtitle_items(items)] == [1, 2]

    def test_empty_input_returns_empty(self) -> None:
        assert normalize_subtitle_items([]) == []


class TestHasChineseTranslation:
    """From ``test_subtitle.py::test_has_chinese_translation``.

    This is the guard against a translator returning its input unchanged, which
    produces a "bilingual" subtitle with two identical English tracks.
    """

    def test_english_target_text_is_not_a_translation(self) -> None:
        items = [SubtitleItem(1, 0, 1000, "Hello", "Hello world")]
        assert has_chinese_translation(items) is False

    def test_chinese_target_text_counts(self) -> None:
        items = [SubtitleItem(1, 0, 1000, "Hello", "你好")]
        assert has_chinese_translation(items) is True

    def test_empty_target_text_does_not_count(self) -> None:
        assert has_chinese_translation([SubtitleItem(1, 0, 1000, "Hello", "")]) is False

    def test_one_translated_cue_among_many_is_enough(self) -> None:
        items = [
            SubtitleItem(1, 0, 1000, "a", ""),
            SubtitleItem(2, 1000, 2000, "b", "部分翻译"),
            SubtitleItem(3, 2000, 3000, "c", ""),
        ]
        assert has_chinese_translation(items) is True
