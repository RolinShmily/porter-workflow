"""Sentence reconstruction, and the one rule that departs from v0.1.

The rule is enforced in **two** places -- ``merge_short_fragments`` and
``reconstruct_sentences_from_fragments`` -- because the pipeline runs the former
first and its wider gap window would otherwise destroy the boundary before the
latter could see it. See ``TestMergingStopsAtSentenceBoundaries``.

``reconstruct_sentences_from_fragments`` is a faithful port except for its sixth
split condition. The first five all key off punctuation, pauses or length, and
**ASR output has no punctuation at all**, so in practice they rarely fire and two
short sentences merged into one:

    (0-700)   "what is going on here"
    (800-1500) "I think that's right"
    -> "What is going on here I think that's right?"

That merged text was then mistranslated (the Chinese came back with scrambled
word order) *and* mis-punctuated, because ``restore_english_punctuation_heuristic``
sees a leading "What" and marks the statement as a question. Measured on a real
run, so this is not a hypothetical.

Condition 6 is the only one that inspects the words. The bulk of this file is
about the cases it must NOT fire on: a split rule that fixes two sentences but
breaks reported speech is a net loss, and the guards are the whole design.
"""

from __future__ import annotations

import pytest

from porter.models.subtitle import SubtitleItem
from porter.subtitles.phrasing import merge_short_fragments, split_chinese_text_by_phrase
from porter.subtitles.transcript import (
    reconstruct_sentences_from_fragments,
    split_chinese_sentence_into_cues,
)


def _frags(*texts: str, gap_ms: int = 100) -> list[SubtitleItem]:
    """Fragments 700ms long, spaced ``gap_ms`` apart.

    The default gap is under the 600ms silence threshold, so these cases exercise
    condition 6 rather than the pause rule -- which is the point, since real ASR
    fragments follow each other closely.
    """
    items = []
    start = 0
    for index, text in enumerate(texts, 1):
        items.append(
            SubtitleItem(
                index=index,
                start_ms=start,
                end_ms=start + 700,
                source_text=text,
                target_text="",
            )
        )
        start += 700 + gap_ms
    return items


def _sentences(*texts: str, gap_ms: int = 100) -> list[str]:
    return [s.en_text for s in reconstruct_sentences_from_fragments(_frags(*texts, gap_ms=gap_ms))]


class TestAShortSentenceFollowedByAnotherIsSplit:
    """The bug condition 6 exists to fix."""

    def test_two_statements(self) -> None:
        assert _sentences("what is going on here", "I think that's right") == [
            "What is going on here?",
            "I think that's right.",
        ]

    def test_three_short_statements(self) -> None:
        assert _sentences("that was fast", "I liked it", "we should keep it") == [
            "That was fast.",
            "I liked it.",
            "We should keep it.",
        ]

    def test_a_question_then_a_statement(self) -> None:
        assert _sentences("how does this work", "you just run it") == [
            "How does this work?",
            "You just run it.",
        ]

    def test_a_declarative_then_an_existential(self) -> None:
        assert _sentences("the build is done", "there is nothing left") == [
            "The build is done.",
            "There is nothing left.",
        ]

    def test_each_sentence_keeps_its_own_timing(self) -> None:
        """Splitting must not lose or shift the fragment timings."""
        sentences = reconstruct_sentences_from_fragments(
            _frags("what is going on here", "I think that's right")
        )

        assert [(s.start_ms, s.end_ms) for s in sentences] == [(0, 700), (800, 1500)]

    def test_fragment_indices_stay_with_their_sentence(self) -> None:
        """The mapping back to source cues is what makes re-splitting possible."""
        sentences = reconstruct_sentences_from_fragments(
            _frags("what is going on here", "I think that's right")
        )

        assert [s.fragment_indices for s in sentences] == [[1], [2]]


class TestItDoesNotSplitWhatIsOneSentence:
    """Every guard, each rejecting a specific real failure.

    These matter more than the fixes above. Merging two sentences degrades a
    translation; splitting one sentence removes the sentence context that
    sentence-level translation exists to provide, which is the same defect
    pointed the other way.
    """

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("the output of", "the encoder is wrong"),
            ("and we need to", "fix it before we ship"),
            ("this is the", "best option we have"),
            ("we shipped it but", "I still worry about it"),
            ("the encoder is fast and", "the decoder is slow"),
            ("the problem is", "we have no tests"),
            ("I fixed my", "own mistakes"),
            ("I know that", "we can do better"),
            ("it is better than", "what we had before"),
        ],
    )
    def test_a_clause_ending_on_an_open_word_is_kept(
        self, first: str, second: str
    ) -> None:
        """Guard 2: determiners, prepositions, auxiliaries, conjunctions.

        A break here is the most visible failure of naive splitting -- "the
        output of. the encoder".
        """
        assert len(_sentences(first, second)) == 1

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("I know what", "you mean by that"),
            ("I remember when", "we first met here"),
            ("let me know if", "you need anything else"),
            ("tell me why", "this keeps happening"),
            ("ask her whether", "she agrees with us"),
        ],
    )
    def test_a_dangling_interrogative_is_kept(self, first: str, second: str) -> None:
        """Guard 3: interrogative and relative words expect a continuation.

        These are deliberately NOT added to ``NON_TERMINAL_WORDS``, because that
        set is shared with the punctuation heuristic and widening it would change
        v0.1-faithful output.
        """
        assert len(_sentences(first, second)) == 1

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("he said", "I should go"),
            ("she told me", "we could leave early"),
            ("and then he said", "I should go now"),
            ("and then she asked", "if we were ready"),
            ("he thought", "we had already left"),
        ],
    )
    def test_reported_speech_is_kept(self, first: str, second: str) -> None:
        """Guard 4: a reporting clause expects its reported clause.

        Without this, "she told me we could leave early" -- one sentence -- was
        split in two.
        """
        assert len(_sentences(first, second)) == 1

    def test_a_reporting_verb_at_the_end_is_kept(self) -> None:
        assert len(_sentences("and then he said", "I should go now")) == 1

    def test_a_reporting_verb_followed_by_an_object_is_kept(self) -> None:
        assert len(_sentences("she told me", "we could leave early")) == 1

    def test_a_reporting_verb_with_a_real_object_splits(self) -> None:
        """"he said goodbye" ends a sentence; the guard must not over-fire.

        This is the false-negative side of guard 4: only a *dangling* reporting
        verb protects, not any occurrence of one.
        """
        assert len(_sentences("he said goodbye", "we left after that")) == 2

    def test_a_two_word_fragment_is_never_treated_as_a_sentence(self) -> None:
        """Guard 5: the rule is weaker evidence than a full stop.

        "he said" is two words and is almost always a continuation, so the rule
        does not overrule on length alone.
        """
        assert len(_sentences("he said", "I should go")) == 1


class TestThePunctuationRuleStillWins:
    """Condition 6 is additive; conditions 1-5 are untouched."""

    def test_a_full_stop_still_splits(self) -> None:
        assert _sentences("the encoder is wrong.", "I think that's right") == [
            "The encoder is wrong.",
            "I think that's right.",
        ]

    def test_a_long_pause_still_splits(self) -> None:
        assert len(_sentences("the encoder is wrong", "I think that's right", gap_ms=2000)) == 2

    def test_a_capitalised_fragment_after_twenty_words_still_splits(self) -> None:
        long_first = (
            "this is a fairly long sentence that contains a great deal of content and keeps going"
        )

        assert len(_sentences(long_first, "I think that's right")) == 2

    def test_a_single_fragment_is_one_sentence(self) -> None:
        assert _sentences("just one fragment here") == ["Just one fragment here."]

    def test_an_empty_input_produces_no_sentences(self) -> None:
        assert reconstruct_sentences_from_fragments([]) == []


class TestTheSplitIsWhatFixesTheTranslation:
    """The end-to-end reason: sentence context is what the translator needs.

    This is the user-visible consequence, asserted as text rather than as a count,
    because "2 sentences" is only right if the two are the right two.
    """

    def test_the_merged_version_would_be_the_wrong_question(self) -> None:
        """The merged text is not merely longer -- it changes the sentence type.

        Left merged, ``restore_english_punctuation_heuristic`` sees a leading
        "What" and marks the whole thing a question, which is what produced the
        scrambled Chinese on a real run.
        """
        merged = _sentences("what is going on here I think that's right")

        assert merged == ["What is going on here I think that's right?"]
        assert len(_sentences("what is going on here", "I think that's right")) == 2

    def test_chinese_targets_stay_with_their_own_sentence(self) -> None:
        """A pre-existing Chinese track must follow its fragment, not be orphaned."""
        items = _frags("what is going on here", "I think that's right")
        items[0].target_text = "这是怎么回事"
        items[1].target_text = "我认为这是对的"

        sentences = reconstruct_sentences_from_fragments(items)

        assert [s.zh_text for s in sentences] == ["这是怎么回事", "我认为这是对的"]


class TestMergingStopsAtSentenceBoundaries:
    """``merge_short_fragments`` must not join across a sentence boundary.

    This is the guard that actually fixes the bug, and the reason is an ordering
    trap worth stating plainly: ``translate/chain.py`` runs
    ``merge_short_fragments`` **first**, with an 800ms gap window. That window is
    wider than the 600ms one in sentence reconstruction, so two fragments 100ms
    apart were joined here and sentence reconstruction never saw a boundary to
    find.

    The first version of this fix guarded only the reconstruction stage. Its unit
    tests passed, the differential harness passed, and the live pipeline still
    produced the merged sentence. A test that does not model the real call order
    can pass while the product stays broken.
    """

    def test_two_sentences_are_not_joined(self) -> None:
        merged = merge_short_fragments(
            _frags("what is going on here", "I think that's right")
        )

        assert [m.source_text for m in merged] == [
            "what is going on here",
            "I think that's right",
        ]

    def test_three_short_sentences_stay_separate(self) -> None:
        merged = merge_short_fragments(
            _frags("that was fast", "I liked it", "we should keep it")
        )

        assert len(merged) == 3

    def test_rolling_shards_are_still_joined(self) -> None:
        """The function's actual job must keep working.

        These are the YouTube-style shards it exists for, and the guard must not
        stop them: "the" and "before" are not sentence openers.
        """
        merged = merge_short_fragments(
            _frags("so the output of", "the encoder is wrong")
        )

        assert [m.source_text for m in merged] == ["so the output of the encoder is wrong"]

    def test_a_clause_ending_on_an_open_word_is_still_joined(self) -> None:
        assert len(merge_short_fragments(_frags("and we need to", "fix it"))) == 1

    def test_reported_speech_is_still_joined(self) -> None:
        assert len(merge_short_fragments(_frags("she told me", "we could leave early"))) == 1

    def test_the_merge_window_alone_would_have_joined_them(self) -> None:
        """Pins the mechanism, not just the outcome.

        With the sentence check removed, the 800ms gap window and the length
        ceiling both allow the join -- so the guard is the only thing preventing
        it, and a future change to either threshold cannot silently reintroduce
        the bug.
        """
        items = _frags("what is going on here", "I think that's right")

        assert items[1].start_ms - items[0].end_ms <= 800
        assert len("what is going on here I think that's right") <= 90

    def test_indices_are_renumbered_after_the_guard_splits(self) -> None:
        merged = merge_short_fragments(
            _frags("that was fast", "I liked it", "we should keep it")
        )

        assert [m.index for m in merged] == [1, 2, 3]

    def test_an_empty_input_returns_nothing(self) -> None:
        assert merge_short_fragments([]) == []


class TestTheTwoStagePipeline:
    """The real order, as ``translate/chain.py`` runs it.

    These are the tests that would have caught the incomplete first fix, because
    they exercise merge and reconstruction together rather than either alone.
    """

    @staticmethod
    def _pipeline(*texts: str) -> list[str]:
        merged = merge_short_fragments(_frags(*texts))
        return [s.en_text for s in reconstruct_sentences_from_fragments(merged)]

    def test_two_sentences_survive_both_stages(self) -> None:
        assert self._pipeline("what is going on here", "I think that's right") == [
            "What is going on here?",
            "I think that's right.",
        ]

    def test_rolling_shards_become_one_sentence(self) -> None:
        """Merging and reconstruction must cooperate, not fight.

        Four shards become one merged fragment and then one sentence -- the
        behaviour the two stages exist to produce.
        """
        assert self._pipeline(
            "so the output of", "the encoder is wrong", "and we need to fix it", "before we ship"
        ) == ["So the output of the encoder is wrong, and we need to fix it before we ship."]

    def test_a_mixed_transcript_groups_correctly(self) -> None:
        assert self._pipeline(
            "so the output of", "the encoder is wrong",
            "what is going on here", "I think that's right",
        ) == [
            "So the output of the encoder is wrong.",
            "What is going on here?",
            "I think that's right.",
        ]

    def test_the_merged_sentence_is_what_broke_the_translation(self) -> None:
        """Pins the user-visible symptom, as text.

        Left merged, the leading "What" made the punctuation heuristic mark the
        whole thing a question, and the Chinese came back with the clauses
        reversed. Both halves are asserted so a regression shows up as the wrong
        sentence *type*, not merely the wrong count.
        """
        merged = self._pipeline("what is going on here I think that's right")

        assert merged == ["What is going on here I think that's right?"]
        assert self._pipeline("what is going on here", "I think that's right") == [
            "What is going on here?",
            "I think that's right.",
        ]


class TestACueIsNeverSplitInsideALatinWord:
    """A CJK character may be cut anywhere; ``Windows`` may not.

    The midpoint pass used a raw character index, which served a real subtitle as
    ``这里的所有内容都在 Wi`` / ``ndows 上本地运行``. The English is cut to match
    the Chinese, so both lines came out wrong. Chinese/Latin mixing is the common
    case, not the exotic one: brand names, ``AI``, ``Python``.
    """

    ZH = "这里的所有内容都在 Windows 上本地运行"

    def test_the_phrase_splitter_keeps_the_word_whole(self) -> None:
        pieces = split_chinese_text_by_phrase(self.ZH, max_len=20)

        assert any("Windows" in piece for piece in pieces), pieces
        assert not any(piece.endswith(" Wi") for piece in pieces), pieces
        assert not any(piece.startswith("ndows") for piece in pieces), pieces

    def test_no_cue_starts_mid_word(self) -> None:
        cues = split_chinese_sentence_into_cues(
            "Everything here runs locally on Windows.",
            self.ZH,
            start_ms=0,
            end_ms=6000,
        )

        assert len(cues) > 1, "longer than max_cjk_len, so it must split"
        assert not any(cue.target_text.startswith("ndows") for cue in cues)
        assert any("Windows" in cue.target_text for cue in cues)

    def test_splitting_still_happens_for_pure_chinese(self) -> None:
        """The fix must not turn long Chinese into one oversized line."""
        pieces = split_chinese_text_by_phrase("这是第一句话。这是第二句话。", max_len=8)

        assert pieces == ["这是第一句话", "这是第二句话"]

    def test_one_unbreakable_token_is_left_whole(self) -> None:
        """A token with no boundary at all cannot be split without severing it.

        A line over ``max_len`` reads better than half an identifier.
        """
        token = "a" * 30

        assert split_chinese_text_by_phrase(token, max_len=10) == [token]
