"""Subtitle parsing, phrasing and rendering.

Contents. In v0.1 all of this lived in one
1184-line ``formatter.py``; the split is **purely physical** — the algorithms do
not change, and ``tests/regression/`` asserts the v0.1 behaviour verbatim.

All four modules are ported. ``ass.py`` carries the aspect-ratio-aware styling
math that v0.1 had left in the phase orchestrator, and ``transcript.py`` carries
the script-book writers.

``srt.py``
    ``parse_srt``, ``srt_time_to_ms``, ``ms_to_srt_time``, ``generate_*_srt``,
    plus the VTT/Bilibili-JSON → SRT converters that v0.1 duplicated three
    times across the extractors.
``phrasing.py``
    Sentence reconstruction from ASR fragments, Chinese phrase splitting,
    English re-segmentation, bilingual alignment, CJK validation.
``ass.py``
    ASS time formatting, style computation (aspect-ratio aware), and the
    bilingual / Chinese-only ASS writers.
``transcript.py``
    Structured transcript ("script book") persistence as JSON and TXT.
"""

from porter.subtitles.ass import (
    compute_adaptive_subtitle_style,
    generate_bilingual_ass,
    generate_zh_ass,
)
from porter.subtitles.phrasing import (
    align_bilingual_items,
    clean_chinese_punctuation,
    has_chinese_translation,
    normalize_subtitle_items,
)
from porter.subtitles.srt import (
    CHINESE_LANG_PRIORITY,
    SOURCE_LANG_PRIORITY,
    bilibili_json_to_srt,
    generate_bilingual_srt,
    generate_zh_srt,
    parse_srt,
    select_chinese_lang,
    select_source_lang,
    vtt_to_srt,
)
from porter.subtitles.transcript import (
    save_transcript_json,
    save_transcript_txt,
)

__all__ = [
    "CHINESE_LANG_PRIORITY",
    "SOURCE_LANG_PRIORITY",
    "align_bilingual_items",
    "bilibili_json_to_srt",
    "clean_chinese_punctuation",
    "compute_adaptive_subtitle_style",
    "generate_bilingual_ass",
    "generate_bilingual_srt",
    "generate_zh_ass",
    "generate_zh_srt",
    "has_chinese_translation",
    "normalize_subtitle_items",
    "parse_srt",
    "save_transcript_json",
    "save_transcript_txt",
    "select_chinese_lang",
    "select_source_lang",
    "vtt_to_srt",
]
