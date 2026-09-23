"""Structured transcript ("script book") and whole-sentence cue splitting.

Ported from v0.1 ``subtitle/formatter.py`` (lines 574-665 and 833-899). These are
the two halves of the sentence-level translation strategy, and they only make
sense together:

1. :func:`reconstruct_sentences_from_fragments` joins fragmented ASR cues into
   whole grammatical sentences, so a translator sees a sentence rather than a
   three-word shard. Translating shards is what produces Chinese with inverted
   word order -- the translator cannot know that "the output of" continues into
   "the encoder is wrong".
2. :func:`split_chinese_sentence_into_cues` takes a translated sentence and cuts
   it back into readable, timed lines.

``save_transcript_json`` / ``save_transcript_txt`` persist step 1 to ``raw/``,
which is the artifact to inspect when a translation reads wrong: it shows exactly
what the translator was given.

## The splitting is not arbitrary

Chinese and English line lengths are constrained differently, so the Chinese is
split first (by punctuation, then by connectives, then near the middle) and the
English is then cut to *match those positions*. Timestamps are interpolated in
proportion to Chinese character count, because the Chinese line length is what
the viewer experiences as the cue's weight.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from porter.models.subtitle import SubtitleItem, TranscriptSentence
from porter.subtitles.phrasing import (
    clean_chinese_punctuation,
    restore_english_punctuation_heuristic,
    split_chinese_text_by_phrase,
    split_english_text_to_n_parts,
    starts_new_sentence,
)

__all__ = [
    "reconstruct_sentences_from_fragments",
    "save_transcript_json",
    "save_transcript_txt",
    "split_chinese_sentence_into_cues",
]

#: A gap this long is treated as a sentence boundary regardless of punctuation.
#: 600ms is above the typical inter-word gap in continuous speech and below the
#: pause a speaker makes at a full stop.
_DEFAULT_MAX_SILENCE_GAP_MS = 600

#: A sentence may not run longer than this even without punctuation, so one
#: unpunctuated monologue cannot swallow a whole transcript.
_DEFAULT_MAX_DURATION_MS = 7000

#: Word count past which a capitalised next fragment is taken as a new sentence.
_DEFAULT_MAX_WORDS = 20

#: Terminal marks that end a sentence. Fullwidth forms are written as escapes so
#: they cannot be confused with their ASCII counterparts when read.
_TERMINAL_RE = re.compile(r"[.!?\u3002\uff01\uff1f]$")

def reconstruct_sentences_from_fragments(
    items: list[SubtitleItem],
    max_silence_gap_ms: int = _DEFAULT_MAX_SILENCE_GAP_MS,
    max_duration_ms: int = _DEFAULT_MAX_DURATION_MS,
    max_words: int = _DEFAULT_MAX_WORDS,
) -> list[TranscriptSentence]:
    """Rebuild whole sentences from fragmented ASR or caption cues.

    A fragment ends the current sentence when any of six conditions holds:

    1. terminal punctuation closes the text so far,
    2. the text ends on a dash (an interrupted or trailing-off clause),
    3. the silence before the next fragment exceeds ``max_silence_gap_ms``,
    4. the sentence already runs ``max_duration_ms`` or longer,
    5. it is at least ``max_words`` long *and* the next fragment starts with a
       capital letter, or
    6. the next fragment clearly opens a new sentence -- see
       :func:`~porter.subtitles.phrasing.starts_new_sentence`.

    Condition 5 needs both halves: length alone would split mid-clause, and a
    capital alone is unreliable because ASR capitalises inconsistently.

    Condition 6 exists because conditions 1-5 all key off punctuation, pauses or
    length, and **ASR output has no punctuation at all**. Two short sentences
    therefore merged into one, which was then mistranslated and mis-punctuated --
    measured on a real run, "what is going on here" + "I think that's right"
    became the question "What is going on here I think that's right?" and the
    Chinese came out with scrambled word order. Condition 6 is the only one that
    looks at the words themselves.

    ``fragment_indices`` records which cues each sentence was built from, so a
    later step can map a translated sentence back to its original timing.
    ``sentence_id`` is 1-based and sequential.

    The English is passed through
    :func:`~porter.subtitles.phrasing.restore_english_punctuation_heuristic`, as
    v0.1 did, because ``raw/transcript.json`` is read by a human and raw ASR text
    has no punctuation at all. The heuristic is idempotent (verified on a
    differential suite), so its second application inside
    :func:`~porter.subtitles.phrasing.split_english_text_to_n_parts` is harmless.
    """
    if not items:
        return []

    sentences: list[TranscriptSentence] = []
    current_id = 1
    current_start = items[0].start_ms
    current_end = items[0].end_ms
    current_texts = [items[0].source_text.strip()]
    current_zh = [items[0].target_text.strip()] if items[0].target_text.strip() else []
    current_fragments = [items[0].index]

    for next_item in items[1:]:
        next_text = next_item.source_text.strip()
        if not next_text:
            continue

        gap = next_item.start_ms - current_end
        combined_text = " ".join(current_texts)
        words_count = len(combined_text.split())
        current_duration = current_end - current_start

        ends_terminal = bool(_TERMINAL_RE.search(combined_text))
        ends_dash = combined_text.endswith(("\u2014", "--"))
        next_is_capital = bool(next_text) and next_text[0].isupper()

        should_split = (
            ends_terminal
            or ends_dash
            or gap > max_silence_gap_ms
            or current_duration >= max_duration_ms
            or (words_count >= max_words and next_is_capital)
            or starts_new_sentence(combined_text, next_text)
        )

        if should_split:
            sentences.append(
                TranscriptSentence(
                    sentence_id=current_id,
                    start_ms=current_start,
                    end_ms=current_end,
                    en_text=restore_english_punctuation_heuristic(combined_text),
                    zh_text="".join(current_zh),
                    fragment_indices=list(current_fragments),
                )
            )
            current_id += 1
            current_start = next_item.start_ms
            current_end = next_item.end_ms
            current_texts = [next_text]
            current_zh = (
                [next_item.target_text.strip()] if next_item.target_text.strip() else []
            )
            current_fragments = [next_item.index]
        else:
            current_end = next_item.end_ms
            current_texts.append(next_text)
            if next_item.target_text.strip():
                current_zh.append(next_item.target_text.strip())
            current_fragments.append(next_item.index)

    # The trailing sentence.
    if current_texts:
        sentences.append(
            TranscriptSentence(
                sentence_id=current_id,
                start_ms=current_start,
                end_ms=current_end,
                en_text=restore_english_punctuation_heuristic(" ".join(current_texts)),
                zh_text="".join(current_zh),
                fragment_indices=list(current_fragments),
            )
        )

    return sentences


def split_chinese_sentence_into_cues(
    en_text: str,
    zh_text: str,
    start_ms: int,
    end_ms: int,
    start_index: int = 1,
    max_cjk_len: int = 20,
) -> list[SubtitleItem]:
    """Cut a translated whole sentence back into timed, readable cues.

    Short sentences (``max_cjk_len`` or fewer characters) become a single cue
    spanning the whole sentence -- splitting them would create two flickering
    lines where one reads perfectly.

    Longer ones are split by :func:`~porter.subtitles.phrasing.split_chinese_text_by_phrase`
    and the English is cut to match those positions, then each cue's duration is
    allocated in proportion to its Chinese character count. Timestamps are
    interpolated rather than taken from the original fragments because the split
    points are new: the original cue boundaries described where the *English*
    broke, which is not where the Chinese breaks.

    ``end_ms - start_ms`` is floored at 500ms so a degenerate sentence still
    produces cues with visible duration, and every cue is forced to at least
    200ms.
    """
    zh_text = zh_text.strip()
    en_text = en_text.strip()

    if not zh_text or len(zh_text) <= max_cjk_len:
        return [
            SubtitleItem(
                index=start_index,
                start_ms=start_ms,
                end_ms=end_ms,
                source_text=en_text,
                target_text=zh_text,
            )
        ]

    zh_pieces = split_chinese_text_by_phrase(zh_text, max_len=max_cjk_len)
    if len(zh_pieces) <= 1:
        return [
            SubtitleItem(
                index=start_index,
                start_ms=start_ms,
                end_ms=end_ms,
                source_text=en_text,
                target_text=zh_text,
            )
        ]

    zh_lengths = [len(piece) for piece in zh_pieces]
    total_zh_len = sum(zh_lengths)
    en_pieces = split_english_text_to_n_parts(en_text, len(zh_pieces), zh_lengths)

    duration = max(end_ms - start_ms, 500)
    cues: list[SubtitleItem] = []
    current_t = start_ms

    for idx, (zh_piece, en_piece, zh_len) in enumerate(
        zip(zh_pieces, en_pieces, zh_lengths, strict=False)
    ):
        if idx == len(zh_pieces) - 1:
            next_t = end_ms
        else:
            cue_duration = int(duration * (zh_len / total_zh_len))
            # Leave 200ms before the sentence ends so the final cue cannot be
            # squeezed to nothing by rounding in earlier cues.
            next_t = min(current_t + cue_duration, end_ms - 200)

        cues.append(
            SubtitleItem(
                index=start_index + idx,
                start_ms=current_t,
                end_ms=max(next_t, current_t + 200),
                source_text=en_piece,
                target_text=clean_chinese_punctuation(zh_piece),
            )
        )
        current_t = next_t

    return cues


def save_transcript_json(sentences: list[TranscriptSentence], path: Path) -> None:
    """Save structured transcript to JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [s.to_dict() for s in sentences]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_transcript_txt(sentences: list[TranscriptSentence], path: Path) -> None:
    """Save human-readable bilingual transcript to text file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for s in sentences:
        blocks.append(
            f"[{s.sentence_id}] {s.start_srt} --> {s.end_srt}\n"
            f"EN: {s.en_text}\n"
            f"ZH: {s.zh_text or '(Pending Translation)'}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
