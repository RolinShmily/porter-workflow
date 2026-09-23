"""Cue-level cleanup: overlap repair and bilingual alignment.

Split out of v0.1 ``formatter.py`` per ``docs/REFACTOR_PLAN.md`` §4.5. Both
functions mutate the items they are given (v0.1 did too, and the pipeline relies
on that: it aligns the same list objects it later writes out), so they are
documented as mutating rather than quietly returning copies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from porter.models.subtitle import SubtitleItem
from porter.utils.text import is_cjk

__all__ = [
    "CHINESE_CONJUNCTIONS",
    "HANGING_CONJUNCTIONS",
    "NON_TERMINAL_WORDS",
    "PROPER_NOUNS",
    "align_bilingual_items",
    "clean_chinese_punctuation",
    "has_chinese_translation",
    "merge_short_fragments",
    "normalize_subtitle_items",
    "restore_english_punctuation_heuristic",
    "split_chinese_text_by_phrase",
    "split_english_text_to_n_parts",
    "starts_new_sentence",
]

#: Minimum cue duration after overlap repair. A cue that is one millisecond long
#: is worse than a slightly late one: libass renders it for a single frame and
#: the text is unreadable.
_MIN_CUE_MS = 100

#: Fallback duration for a cue whose end landed at or before its start.
_FALLBACK_CUE_MS = 1000


def normalize_subtitle_items(items: list[SubtitleItem]) -> list[SubtitleItem]:
    """Sort by time and repair overlapping or degenerate timings.

    Overlaps are normal in YouTube's automatic captions, and two cues covering
    the same instant stack on top of each other in the render. The repair clips
    the *earlier* cue's end to the next cue's start, because shortening the
    outgoing subtitle is the change least visible to a viewer.

    Returns the same items, mutated and sorted. Renumbered 1..n.
    """
    if not items:
        return []

    sorted_items = sorted(items, key=lambda item: (item.start_ms, item.end_ms))

    for index in range(len(sorted_items) - 1):
        current = sorted_items[index]
        following = sorted_items[index + 1]

        if current.end_ms > following.start_ms:
            current.end_ms = max(current.start_ms + _MIN_CUE_MS, following.start_ms)

        if current.end_ms <= current.start_ms:
            current.end_ms = current.start_ms + _FALLBACK_CUE_MS

    # The loop above never examines the final cue, so a degenerate last cue
    # survives it. v0.1 had this gap and relied on the SRT writer emitting an
    # empty duration.
    last = sorted_items[-1]
    if last.end_ms <= last.start_ms:
        last.end_ms = last.start_ms + _FALLBACK_CUE_MS

    for position, item in enumerate(sorted_items, start=1):
        item.index = position

    return sorted_items


def align_bilingual_items(
    source_items: list[SubtitleItem],
    zh_items: list[SubtitleItem],
) -> list[SubtitleItem]:
    """Attach Chinese text from ``zh_items`` onto ``source_items``.

    Two strategies, in order:

    1. **Equal counts**: attach positionally. This is exact, and it is the case
       that matters — a platform's own zh track usually segments identically to
       its source track.
    2. **Overlap**: otherwise, concatenate every Chinese cue that overlaps the
       source cue's interval.

    The fallback is deliberately generous. Chinese cues are typically *shorter*
    than their English counterparts (fewer characters, same reading time), so a
    strict "must be fully contained" rule discards most of the text. Requiring
    only positive overlap keeps it, at the cost of occasional duplication between
    neighbours, which is the better error: a duplicated line reads as a subtitle
    timing quirk, a missing line reads as a broken translation.

    Mutates and returns ``source_items``.
    """
    if not source_items:
        return []
    if not zh_items:
        return source_items

    if len(source_items) == len(zh_items):
        # strict=True documents the invariant the branch just tested. Without it
        # a future edit that changes the condition would silently misalign text.
        for source, chinese in zip(source_items, zh_items, strict=True):
            source.target_text = (chinese.source_text or chinese.target_text).strip()
        return source_items

    for source in source_items:
        collected: list[str] = []
        for chinese in zh_items:
            overlap_start = max(source.start_ms, chinese.start_ms)
            overlap_end = min(source.end_ms, chinese.end_ms)
            if overlap_end > overlap_start:
                text = (chinese.source_text or chinese.target_text).strip()
                if text and text not in collected:
                    collected.append(text)
        source.target_text = "".join(collected)

    return source_items


def has_chinese_translation(
    items: list[SubtitleItem],
) -> bool:
    """Whether any cue actually carries Chinese characters.

    This is the guard against the failure mode that matters: a translation
    backend returning its input unchanged. The job then produces an ASS file
    whose "Chinese" line is English, and the viewer sees a *bilingual* subtitle
    with two identical tracks. Checking for CJK rather than for non-empty text is
    what catches that — ``target_text`` is non-empty in the broken case.
    """
    return any(item.target_text and is_cjk(item.target_text) for item in items)


#: Sentence-final periods only. Matches the Chinese full stop and the ASCII one,
#: because ASR and both translators emit either.
_TRAILING_PERIOD = re.compile(r"[\u3002\.]+$")


def clean_chinese_punctuation(text: str) -> str:
    """Drop a trailing sentence-final period from a Chinese cue.

    Chinese burned-in subtitles omit the final period as house style. Internal
    commas, pauses, question marks, exclamations, colons and dashes are all
    preserved, because those carry meaning a viewer needs.

    Lives here rather than in :mod:`porter.subtitles.ass` even though only the ASS
    writers call it today: it is *typography for Chinese text*, and
    :mod:`~porter.subtitles.phrasing` is where the other CJK-aware rules live.
    Keeping it next to ``has_chinese_translation`` means a future SRT-side need
    reaches for this function instead of writing a second copy of the regex —
    which is how v0.1 ended up with three copies of its VTT converter.

    Ported from v0.1 ``formatter.py::clean_chinese_subtitle_punctuation``; the
    name drops "subtitle" because it is unambiguous inside this package.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    return _TRAILING_PERIOD.sub("", stripped).strip()

# ======================================================================
# Sentence reconstruction helpers
# ======================================================================
#
# Ported from v0.1 ``formatter.py``. These are the pieces that let translation
# happen on whole sentences instead of on fragments, which is the difference
# between idiomatic Chinese and word-for-word Chinese.
#
# ``restore_english_punctuation_heuristic`` is a deliberately large pile of
# heuristics. It is not elegant and it is not meant to be: ASR output has no
# punctuation at all, and without clause boundaries neither sentence splitting
# nor clause-aware line breaking has anything to work with. It is ported
# behaviour-for-behaviour because its output is user-visible in the bilingual
# track.

#: Terms that keep their capitalisation when they appear mid-sentence. Without
#: this, "we use Python" becomes "we use python" in the transcript.
PROPER_NOUNS = {
    "AI", "API", "ASR", "CPU", "GPU", "HTML", "HTTP", "HTTPS", "JSON", "LLM",
    "NASA", "NLP", "OS", "RAM", "SDK", "SQL", "SSD", "STT", "TTS", "TUI", "UI",
    "UK", "URL", "USA", "USB", "VAD", "XML",
    "YouTube", "Google", "OpenAI", "DeepSeek", "Microsoft", "Apple", "GitHub",
    "Bilibili", "Claude", "ChatGPT", "Windows", "Linux", "macOS", "Android",
    "iOS", "Python", "JavaScript", "TypeScript", "Rust", "Java", "Docker",
    "FFmpeg", "Obsidian", "Notion",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
}

PROPER_NOUNS_LOWER = {word.lower(): word for word in PROPER_NOUNS}

#: A capitalised word from this set can begin a new sentence even mid-run.
SENTENCE_STARTERS = {
    "it", "it's", "this", "that", "these", "those", "there", "there's",
    "they", "they're", "we", "we're", "he", "he's", "she", "she's",
    "however", "therefore", "moreover", "furthermore", "meanwhile", "instead",
    "otherwise", "nevertheless", "suddenly", "eventually", "actually",
    "basically", "finally", "first", "second", "third", "next",
}

SUBORDINATE_CONJUNCTIONS = {
    "but", "so", "because", "although", "though", "while", "whereas",
    "which", "since", "unless", "yet",
}

COORDINATING_CONJUNCTIONS = {"and", "or", "nor"}

PRONOUN_STARTERS = {
    "i", "you", "he", "she", "it", "we", "they", "this", "that", "there",
    "what", "how",
}

#: Words that cannot end a sentence. A clause break before one of these produces
#: "the output of. the encoder" -- the single most visible failure of naive
#: sentence splitting.
NON_TERMINAL_WORDS = {
    "of", "in", "to", "for", "with", "on", "at", "by", "from", "into", "onto",
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "can", "could", "will", "would",
    "shall", "should", "may", "might", "must", "my", "your", "his", "her",
    "its", "our", "their", "and", "or", "but", "nor", "so", "than", "as", "that",
}

#: Conjunctions that read badly at the *start* of a line, so a break is nudged
#: to land just before one rather than just after.
HANGING_CONJUNCTIONS = {
    "and", "but", "or", "so", "yet", "for", "nor", "because", "although",
    "though", "while", "whereas", "which", "since", "unless", "if", "that",
    "when", "where", "as",
}

#: Chinese connectives used as secondary split points when a phrase is too long
#: for one line. Order matters: the first match in the string wins.
CHINESE_CONJUNCTIONS = [
    "但是", "所以", "因为", "而且",
    "如果", "虽然", "并且", "以及",
    "然后", "同时", "为了", "另外",
    "不过", "由于", "从而", "通过",
    "比如", "例如", "即便", "只要",
    "除非",
]


@dataclass
class _TokenPart:
    """A word split into leading punctuation, core, trailing punctuation."""

    raw: str
    prefix: str
    core: str
    suffix: str


def restore_english_punctuation_heuristic(text: str) -> str:
    """Restore punctuation and capitalisation lost to ASR.

    ASR emits run-on text with no sentence boundaries. Both downstream steps need
    them: :func:`reconstruct_sentences_from_fragments` splits on terminal
    punctuation, and :func:`split_english_text_to_n_parts` scores clause
    boundaries by it. So this runs first, and its output is what the bilingual
    track shows.

    Deliberately conservative about *inserting* boundaries — it requires at least
    four words in the current sentence and refuses to break before a
    :data:`NON_TERMINAL_WORDS` member — because a wrong period in the middle of a
    clause is more visible than a missing one.
    """
    text = text.strip()
    if not text:
        return ""

    tokens = text.split()
    if not tokens:
        return ""

    cleaned_tokens: list[_TokenPart] = []
    for token in tokens:
        # v0.1's regex verbatim. Both quote characters must be backslash-escaped
        # even though this is a raw string: an unescaped `"` terminates it, and
        # the retained backslashes are harmless inside a character class.
        match = re.match(r"^([\"'\\(]*)(.*?)([\.\,\!\?\;\:\)\'\"]*)$", token)
        if match:
            prefix = match.group(1) or ""
            core = match.group(2) or ""
            suffix = match.group(3) or ""
        else:
            prefix, core, suffix = "", token, ""

        if match:
            prefix = match.group(1) or ""
            core = match.group(2) or ""
            suffix = match.group(3) or ""
        else:
            prefix, core, suffix = "", token, ""
        cleaned_tokens.append(_TokenPart(raw=token, prefix=prefix, core=core, suffix=suffix))

    result_words: list[str] = []
    num_tokens = len(cleaned_tokens)

    for i in range(num_tokens):
        curr = cleaned_tokens[i]
        core = curr.core
        lower_core = core.lower()

        # 1. Capitalisation and proper-noun normalisation.
        if lower_core in PROPER_NOUNS_LOWER:
            norm_core = PROPER_NOUNS_LOWER[lower_core]
        elif lower_core in ("i", "i'm", "i'll", "i've", "i'd"):
            norm_core = "I" + lower_core[1:]
        elif i == 0 or (result_words and result_words[-1].endswith((".", "!", "?"))):
            norm_core = core.capitalize()
        elif core.istitle() and not core.isupper() and lower_core not in SENTENCE_STARTERS:
            # ASR capitalised it mid-sentence; undo unless it is a starter.
            norm_core = core.lower()
        else:
            norm_core = core

        # 2. Decide whether the previous word needs a boundary before this one.
        if i > 0 and not result_words[-1].endswith((".", "!", "?", ",", ";", ":", "--", "—")):
            prev_token = cleaned_tokens[i - 1]
            prev_lower = prev_token.core.lower()

            words_in_current_sentence = 0
            for word in reversed(result_words):
                words_in_current_sentence += 1
                if word.endswith((".", "!", "?")):
                    break

            is_sentence_break = (
                bool(curr.raw)
                and curr.raw[0].isupper()
                and lower_core in SENTENCE_STARTERS
                and prev_lower not in NON_TERMINAL_WORDS
                and words_in_current_sentence >= 4
            )

            next_token = cleaned_tokens[i + 1] if i + 1 < num_tokens else None
            next_lower = next_token.core.lower() if next_token else ""

            is_coordinating_clause = (
                lower_core in COORDINATING_CONJUNCTIONS
                and next_lower in PRONOUN_STARTERS
                and words_in_current_sentence >= 4
                and prev_lower not in NON_TERMINAL_WORDS
            )

            is_subordinate_clause = (
                lower_core in SUBORDINATE_CONJUNCTIONS
                and prev_lower not in NON_TERMINAL_WORDS
                and words_in_current_sentence >= 4
            )

            if is_sentence_break:
                result_words[-1] += "."
                norm_core = norm_core.capitalize()
            elif is_subordinate_clause or is_coordinating_clause:
                result_words[-1] += ","
                norm_core = norm_core if lower_core in PROPER_NOUNS_LOWER else norm_core.lower()

        result_words.append(curr.prefix + norm_core + curr.suffix)

    final_text = " ".join(result_words).strip()

    # 3. Guarantee terminal punctuation, choosing '?' for a leading question word.
    if final_text and not final_text.endswith((".", "!", "?", "...", "—")):
        first_word = final_text.split()[0].lower().rstrip(",.!?")
        interrogatives = (
            "how", "why", "what", "where", "when", "who", "which",
            "do", "does", "did", "is", "are", "can", "could", "would", "should",
        )
        looks_like_question = first_word in interrogatives and not final_text.startswith(
            ("What I", "How to", "Why we")
        )
        final_text += "?" if looks_like_question else "."

    return final_text


def split_chinese_text_by_phrase(zh_text: str, max_len: int = 28) -> list[str]:
    """Break Chinese into line-sized phrases without cutting words apart.

    Three passes, in order of preference: split on punctuation (keeping the mark
    with the phrase it closes), then on a connective, then near the middle. The
    final merge pass rejoins adjacent short pieces, which is what stops a run of
    commas from producing a stack of two-character lines.
    """
    zh_text = zh_text.strip()
    if not zh_text or len(zh_text) <= max_len:
        return [clean_chinese_punctuation(zh_text)] if zh_text else []

    raw_pieces = re.findall(r"[^\uff0c\u3001\uff1b\u3002\uff01\uff1f,;!?]+"
        r"[\uff0c\u3001\uff1b\u3002\uff01\uff1f,;!?]?", zh_text)
    if not raw_pieces:
        raw_pieces = [zh_text]

    refined_pieces: list[str] = []
    for piece in raw_pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) <= max_len:
            refined_pieces.append(piece)
            continue

        split_pos = -1
        for conjunction in CHINESE_CONJUNCTIONS:
            idx = piece.find(conjunction)
            if 4 <= idx <= max_len:
                split_pos = idx
                break

        if split_pos != -1:
            head = piece[:split_pos].strip()
            tail = piece[split_pos:].strip()
        else:
            mid = len(piece) // 2
            head = piece[:mid].strip()
            tail = piece[mid:].strip()

        if head:
            refined_pieces.append(head)
        if tail:
            refined_pieces.append(tail)

    if not refined_pieces:
        return [clean_chinese_punctuation(zh_text)]

    merged: list[str] = []
    current = refined_pieces[0]
    for next_piece in refined_pieces[1:]:
        if len(current) + len(next_piece) <= max_len:
            current += next_piece
        else:
            merged.append(clean_chinese_punctuation(current))
            current = next_piece
    merged.append(clean_chinese_punctuation(current))

    return [piece for piece in merged if piece]


def split_english_text_to_n_parts(
    en_text: str, n_parts: int, zh_lengths: list[int]
) -> list[str]:
    """Split English into ``n_parts`` so each part matches a Chinese phrase.

    ``zh_lengths`` drives the target split positions: Chinese line length is the
    constraint a viewer experiences, so the English is cut where the Chinese was
    rather than at even word counts. Among nearby candidates, a break after
    terminal punctuation scores best, then after a semicolon, then a comma; a
    break that would strand a conjunction at a line end is penalised heavily, and
    one that would strand a preposition almost as heavily.
    """
    en_text = restore_english_punctuation_heuristic(en_text.strip())
    if n_parts <= 1 or not en_text:
        return [en_text]

    words = en_text.split()
    total_words = len(words)
    if total_words <= n_parts:
        return [en_text] + [""] * (n_parts - 1)

    total_zh_len = sum(zh_lengths) if zh_lengths else n_parts
    target_proportions = [length / total_zh_len for length in zh_lengths]

    target_cuts: list[float] = []
    cumulative = 0.0
    for proportion in target_proportions[:-1]:
        cumulative += proportion
        target_cuts.append(cumulative * total_words)

    def score_breakpoint(k: int, target_pos: float) -> float:
        prev_word = words[k]
        next_word = words[k + 1] if k + 1 < total_words else ""

        prev_clean = prev_word.rstrip(".,!?;:\"')—").lower()
        next_clean = next_word.lstrip("\"'( ").lower()

        score = -abs((k + 1) - target_pos) * 2.0

        if prev_word.endswith((".", "!", "?")):
            score += 15.0
        elif prev_word.endswith((";", ":", "—", "--")):
            score += 12.0
        elif prev_word.endswith(","):
            score += 10.0

        if next_clean in HANGING_CONJUNCTIONS:
            score += 8.0

        if prev_clean in HANGING_CONJUNCTIONS and not prev_word.endswith((".", "!", "?")):
            score -= 25.0

        if prev_clean in NON_TERMINAL_WORDS and not prev_word.endswith((".", "!", "?")):
            score -= 15.0

        return score

    chosen_cuts: list[int] = []
    min_cut = 0

    for i, target_pos in enumerate(target_cuts):
        remaining_parts = (n_parts - 1) - i
        max_cut = total_words - 1 - remaining_parts

        best_k = min_cut
        best_score = float("-inf")
        for k in range(min_cut, max_cut + 1):
            candidate = score_breakpoint(k, target_pos)
            if candidate > best_score:
                best_score = candidate
                best_k = k

        chosen_cuts.append(best_k)
        min_cut = best_k + 1

    en_parts: list[str] = []
    start_idx = 0
    for cut in chosen_cuts:
        en_parts.append(" ".join(words[start_idx : cut + 1]).strip())
        start_idx = cut + 1
    en_parts.append(" ".join(words[start_idx:]).strip())

    # Re-capitalise a part that follows a finished sentence, unless the word is a
    # proper noun whose casing we must not touch.
    for i in range(1, len(en_parts)):
        if not en_parts[i - 1].endswith((".", "!", "?")):
            continue
        part = en_parts[i]
        if not part or not part[0].islower():
            continue
        first_word, *rest = part.split(maxsplit=1)
        if first_word.lower() not in PROPER_NOUNS_LOWER:
            en_parts[i] = first_word.capitalize() + (" " + rest[0] if rest else "")

    return en_parts


#: Shortest text that may be treated as a complete sentence by the
#: sentence-starter rule. Two-word fragments ("he said", "and then") are
#: almost always continuations, and the rule is not confident enough to
#: overrule that.
MIN_WORDS_FOR_STARTER_SPLIT = 3

#: Words that leave a clause open when they *end* it. Separate from
#: ``NON_TERMINAL_WORDS`` because that set is shared with the punctuation
#: heuristic and changing it would alter v0.1-faithful behaviour; this adds only
#: the interrogative and relative words the split rule needs.
DANGLING_TAIL = frozenset({
    "what", "how", "when", "where", "why", "who", "which", "whose",
    "if", "whether", "because", "since", "while", "although", "though",
    # Prepositions and subordinators that are NOT in NON_TERMINAL_WORDS.
    # Without them "we were talking about" + "you and me" -- one sentence --
    # would be cut in two.
    "about", "after", "before", "between", "during", "through", "against",
    "without", "within", "along", "across", "behind", "beyond", "toward",
    "towards", "upon", "over", "under", "above", "below", "near", "until",
    "till", "like", "whereas", "unless", "yet", "plus", "per",
})

#: Verbs that introduce a reported clause, so text ending on one is incomplete.
REPORTING_VERBS = frozenset({
    "said", "says", "told", "tells", "asked", "asks", "wondered", "thought",
    "thinks", "believed", "believes", "knew", "knows", "heard", "noticed",
    "realised", "realized", "added", "replied", "wrote", "suggested",
})

#: Pronouns that can be the object of a reporting verb ("told **me**").
OBJECT_PRONOUNS = frozenset({"me", "him", "her", "us", "them", "you", "it"})

#: Characters that may wrap a word without being part of it.
EDGE_CHARS = "\"'()[]"


def first_word(text: str) -> str:
    """The first word, lowercased and stripped of quoting and punctuation."""
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.split(" ", 1)[0].strip(EDGE_CHARS).lower().rstrip(",.!?;:")


def last_word(text: str) -> str:
    """The last word, lowercased and stripped of quoting and punctuation."""
    parts = text.strip().split(" ")
    if not parts:
        return ""
    return parts[-1].strip(EDGE_CHARS).lower().rstrip(",.!?;:")


def starts_new_sentence(combined: str, next_text: str) -> bool:
    """Whether ``next_text`` clearly opens a new sentence after ``combined``.

    Added because ASR output has no punctuation at all, so the punctuation-based
    conditions never fire and two short sentences merge into one. The merged text
    is then mistranslated *and* mis-punctuated -- the punctuation heuristic sees
    a leading "What" and marks a statement as a question.

    Four things must all hold, each of which rejects a specific real failure:

    1. the next fragment starts with a subject pronoun or question word
       (``PRONOUN_STARTERS``), the only cheap positive evidence of a new clause;
    2. the text so far does not end on a word that cannot end a sentence --
       "the output of" + "the encoder" must stay one sentence;
    3. it does not end on a dangling interrogative or relative word --
       "I know what" + "you mean" is one sentence;
    4. it is not mid-reported-speech -- "and then he said" + "I should go" and
       "she told me" + "we could leave" are one sentence each.

    A minimum length is required too, because the rule is weaker evidence than an
    explicit full stop and should not overrule it.

    This lives here rather than in ``transcript`` because **two** stages need it:
    :func:`merge_short_fragments` must not join across a sentence boundary, and
    :func:`~porter.subtitles.transcript.reconstruct_sentences_from_fragments` must
    not group across one. Putting it in ``transcript`` would make ``phrasing``
    import from ``transcript``, which already imports from ``phrasing``.
    """
    if len(combined.split()) < MIN_WORDS_FOR_STARTER_SPLIT:
        return False
    if first_word(next_text) not in PRONOUN_STARTERS:
        return False

    tail = last_word(combined)
    if tail in NON_TERMINAL_WORDS or tail in DANGLING_TAIL:
        return False
    if tail in REPORTING_VERBS:
        return False

    parts = combined.strip().split(" ")
    second_last = last_word(parts[-2]) if len(parts) >= 2 else ""
    return not (tail in OBJECT_PRONOUNS and second_last in REPORTING_VERBS)


def merge_short_fragments(
    items: list[SubtitleItem],
    max_gap_ms: int = 800,
    max_duration_ms: int = 7000,
    max_len: int = 90,
) -> list[SubtitleItem]:
    """Join consecutive fragments too short to read as one line.

    YouTube's automatic captions arrive as rolling two- or three-word shards.
    Left alone, each becomes its own subtitle line and the result is unreadable.

    Merging stops at terminal punctuation *unless* the result would still be
    under three seconds -- a short sentence is common in dialogue and reads better
    joined than as a fragment.

    Merging also stops when the next fragment clearly opens a new sentence
    (:func:`starts_new_sentence`). **This is the guard that actually fixes the
    merged-sentence bug**: with an 800ms window this function runs first and joins
    the two fragments before sentence reconstruction ever sees them, so guarding
    only the reconstruction stage changed nothing in the live pipeline. Measured
    on a real run -- the unit-level fix passed while the output stayed wrong.

    Returns new items; the input is not mutated (v0.1 also built a fresh list).
    Renumbered 1..n.
    """
    if not items:
        return []

    merged: list[SubtitleItem] = []
    current = SubtitleItem(
        index=items[0].index,
        start_ms=items[0].start_ms,
        end_ms=items[0].end_ms,
        source_text=items[0].source_text.strip(),
        target_text=items[0].target_text.strip(),
    )

    for next_item in items[1:]:
        gap = next_item.start_ms - current.end_ms
        combined_duration = next_item.end_ms - current.start_ms
        combined_source = f"{current.source_text} {next_item.source_text.strip()}".strip()

        ends_with_punct = bool(re.search(r"[.!?\u3002\uff01\uff1f]$", current.source_text.strip()))

        should_merge = (
            gap <= max_gap_ms
            and combined_duration <= max_duration_ms
            and len(combined_source) <= max_len
            and (not ends_with_punct or combined_duration < 3000)
        )
        if should_merge:
            should_merge = not starts_new_sentence(current.source_text, next_item.source_text)

        if should_merge:
            current.end_ms = next_item.end_ms
            current.source_text = combined_source
            if current.target_text and next_item.target_text:
                separator = "" if is_cjk(current.target_text) else " "
                current.target_text = (
                    current.target_text + separator + next_item.target_text.strip()
                ).strip()
        else:
            merged.append(current)
            current = SubtitleItem(
                index=next_item.index,
                start_ms=next_item.start_ms,
                end_ms=next_item.end_ms,
                source_text=next_item.source_text.strip(),
                target_text=next_item.target_text.strip(),
            )

    merged.append(current)
    for idx, item in enumerate(merged, 1):
        item.index = idx
    return merged
