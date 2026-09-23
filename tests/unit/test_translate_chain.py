"""The translation chain: fallback order, the CJK self-check, and file writing.

All backends are fakes. What is under test is the chain's judgement about whether
a backend's output is a *translation* at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from porter.config import PorterConfig
from porter.context import RunContext
from porter.errors import JobCancelled, PorterError
from porter.events import (
    ArtifactKind,
    ArtifactReady,
    Event,
    ProgressUpdated,
    collect,
)
from porter.models.request import JobOptions
from porter.models.subtitle import SubtitleItem, SubtitleSet
from porter.translate.base import (
    TranslationBackendError,
    TranslationOutcome,
)
from porter.translate.chain import TranslationChain

# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class FakeBackend:
    """A backend that answers from a script.

    ``behaviour`` is a list of strings, an exception, or a callable taking the
    call count and returning either.
    """

    def __init__(
        self,
        name: str,
        behaviour=None,
        *,
        available: bool = True,
        probe_raises: bool = False,
        translates: bool = True,
    ) -> None:
        self.name = name
        if behaviour is None:
            behaviour = (lambda texts: [f"[{name}]{text}" for text in texts]) if translates else []
        self.behaviour = behaviour
        self.is_available = available
        self.probe_raises = probe_raises
        self.calls: list[list[str]] = []

    def available(self, ctx: RunContext) -> bool:
        if self.probe_raises:
            raise RuntimeError(f"{self.name} probe is broken")
        return self.is_available

    def translate_texts(
        self, texts: list[str], target_lang: str, ctx: RunContext
    ) -> TranslationOutcome:
        self.calls.append(list(texts))
        outcome = self.behaviour
        if callable(outcome):
            outcome = outcome(texts)
        if isinstance(outcome, Exception):
            raise outcome
        return TranslationOutcome(texts=list(outcome), origin=self.name)


def chinese(texts: list[str]) -> list[str]:
    """A backend that returns real Chinese, positionally aligned."""
    return [f"中文{index}" for index, _ in enumerate(texts, start=1)]


def passthrough(texts: list[str]) -> list[str]:
    """The failure mode this chain exists to catch: HTTP 200 echoing the input."""
    return list(texts)


@pytest.fixture
def config(tmp_path) -> PorterConfig:
    return PorterConfig(output_dir=tmp_path / "out")


@pytest.fixture
def ctx(config) -> RunContext:
    return RunContext(
        job_id="test", options=JobOptions(output_dir=config.output_dir), config=config
    )


def _set(tmp_path: Path, *, count: int = 3, width: int | None = 1920, height: int | None = 1080):
    cooked = tmp_path / "out" / "vid1_A_Video" / "cooked"
    cooked.mkdir(parents=True, exist_ok=True)
    # Cues are spaced 1000ms apart and end with a full stop, so each is its own
    # sentence: translation is now sentence-level (see porter.translate.chain),
    # and the default fixture must therefore yield one sentence per cue for the
    # "N inputs -> N outputs" tests to be about the chain rather than about
    # merging. The merging and splitting behaviour has its own tests below.
    items = [
        SubtitleItem(
            index=index,
            start_ms=index * 2000,
            end_ms=index * 2000 + 1000,
            source_text=f"English cue {index}.",
            target_text="",
        )
        for index in range(1, count + 1)
    ]
    return SubtitleSet(
        subtitle_bilingual_srt=cooked / "subtitle_bilingual.srt",
        subtitle_bilingual_ass=cooked / "subtitle_bilingual.ass",
        subtitle_zh_srt=cooked / "subtitle_zh.srt",
        subtitle_zh_ass=cooked / "subtitle_zh.ass",
        transcript_json_path=cooked / "transcript.json",
        transcript_txt_path=cooked / "transcript.txt",
        items=items,
        video_width=width,
        video_height=height,
    )


# ----------------------------------------------------------------------
# Availability
# ----------------------------------------------------------------------


class TestAvailability:
    def test_no_backends_means_unavailable(self, ctx) -> None:
        assert TranslationChain().available(ctx) is False

    def test_one_available_backend_is_enough(self, ctx) -> None:
        chain = TranslationChain([FakeBackend("a", available=False), FakeBackend("b")])
        assert chain.available(ctx) is True

    def test_a_broken_probe_does_not_abort(self, ctx, tmp_path) -> None:
        chain = TranslationChain(
            [FakeBackend("broken", probe_raises=True), FakeBackend("ok", chinese)]
        )
        assert chain.available(ctx) is True
        assert chain.translate(_set(tmp_path), "zh-Hans", ctx).items[0].target_text == "中文1"


# ----------------------------------------------------------------------
# The CJK self-check
# ----------------------------------------------------------------------


class TestCjkSelfCheck:
    """The reason this is a chain and not a for-loop.

    A key-free backend answering HTTP 200 with the input echoed back produces
    non-empty target text that is still English. Nothing downstream notices: the
    BURN phase succeeds and the video has two identical English tracks.
    """

    def test_a_pass_through_is_rejected_and_the_next_backend_runs(self, ctx, tmp_path) -> None:
        echoing = FakeBackend("echoing", passthrough)
        working = FakeBackend("working", chinese)
        chain = TranslationChain([echoing, working])

        result = chain.translate(_set(tmp_path), "zh-Hans", ctx)

        assert echoing.calls, "the echoing backend should have been tried"
        assert result.items[0].target_text.startswith("中文")

    def test_a_pass_through_alone_raises_rather_than_shipping_english(self, ctx, tmp_path) -> None:
        """Better to fail than to produce a bilingual file with one language."""
        chain = TranslationChain([FakeBackend("echoing", passthrough)])

        with pytest.raises(PorterError) as excinfo:
            chain.translate(_set(tmp_path), "zh-Hans", ctx)

        assert "no Chinese characters" in str(excinfo.value)

    def test_english_target_text_is_rejected(self, ctx, tmp_path) -> None:
        chain = TranslationChain([FakeBackend("english", lambda texts: ["hello"] * len(texts))])
        with pytest.raises(PorterError):
            chain.translate(_set(tmp_path), "zh-Hans", ctx)

    def test_cues_that_already_carry_chinese_skip_every_backend(self, ctx, tmp_path) -> None:
        """The platform Chinese track case: free and exact, so do not re-translate."""
        backend = FakeBackend("unused")
        subtitles = _set(tmp_path)
        for item in subtitles.items:
            item.target_text = "已经是中文"

        result = TranslationChain([backend]).translate(subtitles, "zh-Hans", ctx)

        assert backend.calls == []
        assert result.items[0].target_text == "已经是中文"

    def test_files_are_still_written_when_translation_is_skipped(self, ctx, tmp_path) -> None:
        """TRANSLATE's contract is to produce the files, regardless of source."""
        subtitles = _set(tmp_path)
        for item in subtitles.items:
            item.target_text = "已经是中文"

        TranslationChain([FakeBackend("unused")]).translate(subtitles, "zh-Hans", ctx)

        assert subtitles.subtitle_bilingual_srt.is_file()
        assert subtitles.subtitle_zh_ass.is_file()

    def test_a_partial_translation_is_still_accepted(self, ctx, tmp_path) -> None:
        """One CJK cue is enough: a backend that missed a proper noun is fine."""
        chain = TranslationChain(
            [FakeBackend("partial", lambda texts: ["中文", *texts[1:]])]
        )
        result = chain.translate(_set(tmp_path), "zh-Hans", ctx)
        assert result.items[0].target_text == "中文"


class TestAlignmentCheck:
    """A dropped element shifts every later cue onto the wrong moment."""

    def test_a_short_result_is_rejected(self, ctx, tmp_path) -> None:
        short = FakeBackend("short", lambda texts: ["中文"] * (len(texts) - 1))
        chain = TranslationChain([short])

        with pytest.raises(PorterError) as excinfo:
            chain.translate(_set(tmp_path), "zh-Hans", ctx)

        assert "misalign" in str(excinfo.value)

    def test_a_long_result_is_rejected(self, ctx, tmp_path) -> None:
        long_backend = FakeBackend("long", lambda texts: ["中文"] * (len(texts) + 1))
        with pytest.raises(PorterError):
            TranslationChain([long_backend]).translate(_set(tmp_path), "zh-Hans", ctx)

    def test_a_misaligned_backend_advances_to_the_next(self, ctx, tmp_path) -> None:
        short = FakeBackend("short", lambda texts: ["中文"] * (len(texts) - 1))
        working = FakeBackend("working", chinese)
        result = TranslationChain([short, working]).translate(_set(tmp_path), "zh-Hans", ctx)

        assert result.items[0].target_text.startswith("中文")


# ----------------------------------------------------------------------
# Fallback order
# ----------------------------------------------------------------------


class TestFallback:
    def test_the_first_working_backend_wins(self, ctx, tmp_path) -> None:
        first, second = FakeBackend("first", chinese), FakeBackend("second", chinese)
        result = TranslationChain([first, second]).translate(_set(tmp_path), "zh-Hans", ctx)

        assert first.calls and not second.calls
        assert result.items[0].target_text == "中文1"

    def test_a_backend_error_advances(self, ctx, tmp_path) -> None:
        chain = TranslationChain(
            [
                FakeBackend("failing", TranslationBackendError("failing", "429")),
                FakeBackend("working", chinese),
            ]
        )
        assert chain.translate(_set(tmp_path), "zh-Hans", ctx).items

    def test_an_unavailable_backend_is_never_called(self, ctx, tmp_path) -> None:
        skipped = FakeBackend("skipped", available=False)
        chain = TranslationChain([skipped, FakeBackend("working", chinese)])

        chain.translate(_set(tmp_path), "zh-Hans", ctx)
        assert skipped.calls == []

    def test_every_reason_is_reported_when_all_fail(self, ctx, tmp_path) -> None:
        chain = TranslationChain(
            [
                FakeBackend("a", TranslationBackendError("a", "quota exhausted")),
                FakeBackend("b", passthrough),
            ]
        )

        with pytest.raises(PorterError) as excinfo:
            chain.translate(_set(tmp_path), "zh-Hans", ctx)

        message = str(excinfo.value)
        assert "quota exhausted" in message
        assert "no Chinese characters" in message

    def test_an_unexpected_exception_propagates(self, ctx, tmp_path) -> None:
        chain = TranslationChain(
            [FakeBackend("broken", TypeError("our bug")), FakeBackend("working", chinese)]
        )

        with pytest.raises(TypeError):
            chain.translate(_set(tmp_path), "zh-Hans", ctx)

    def test_no_cues_raises_before_calling_any_backend(self, ctx, tmp_path) -> None:
        backend = FakeBackend("unused")
        with pytest.raises(PorterError):
            TranslationChain([backend]).translate(_set(tmp_path, count=0), "zh-Hans", ctx)
        assert backend.calls == []


class TestCancellation:
    def test_cancellation_is_not_treated_as_a_backend_failure(self, ctx, tmp_path) -> None:
        chain = TranslationChain(
            [
                FakeBackend("cancelling", JobCancelled("stopped")),
                FakeBackend("working", chinese),
            ]
        )

        with pytest.raises(JobCancelled):
            chain.translate(_set(tmp_path), "zh-Hans", ctx)

    def test_a_cancelled_job_does_not_try_the_next_backend(self, ctx, tmp_path) -> None:
        after = FakeBackend("after", chinese)
        chain = TranslationChain([FakeBackend("cancelling", JobCancelled("stopped")), after])

        with pytest.raises(JobCancelled):
            chain.translate(_set(tmp_path), "zh-Hans", ctx)
        assert after.calls == []

    def test_a_cancelled_context_stops_immediately(self, ctx, tmp_path) -> None:
        backend = FakeBackend("a", chinese)
        ctx.request_cancel()

        with pytest.raises(JobCancelled):
            TranslationChain([backend]).translate(_set(tmp_path), "zh-Hans", ctx)
        assert backend.calls == []


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------


class TestOutput:
    def test_all_four_files_are_written(self, ctx, tmp_path) -> None:
        subtitles = TranslationChain([FakeBackend("ok", chinese)]).translate(
            _set(tmp_path), "zh-Hans", ctx
        )

        for path in (
            subtitles.subtitle_bilingual_srt,
            subtitles.subtitle_zh_srt,
            subtitles.subtitle_bilingual_ass,
            subtitles.subtitle_zh_ass,
        ):
            assert path.is_file(), f"{path.name} was not written"

    def test_the_bilingual_srt_has_both_languages(self, ctx, tmp_path) -> None:
        subtitles = TranslationChain([FakeBackend("ok", chinese)]).translate(
            _set(tmp_path), "zh-Hans", ctx
        )
        text = subtitles.subtitle_bilingual_srt.read_text(encoding="utf-8")

        assert "中文1" in text
        assert "English cue 1" in text

    def test_the_chinese_srt_has_only_chinese(self, ctx, tmp_path) -> None:
        subtitles = TranslationChain([FakeBackend("ok", chinese)]).translate(
            _set(tmp_path), "zh-Hans", ctx
        )
        text = subtitles.subtitle_zh_srt.read_text(encoding="utf-8")

        assert "中文1" in text
        assert "English cue 1" not in text

    def test_the_ass_files_are_valid_ass(self, ctx, tmp_path) -> None:
        subtitles = TranslationChain([FakeBackend("ok", chinese)]).translate(
            _set(tmp_path), "zh-Hans", ctx
        )
        text = subtitles.subtitle_bilingual_ass.read_text(encoding="utf-8")

        assert "[Script Info]" in text
        assert "[V4+ Styles]" in text
        assert "Dialogue:" in text

    def test_the_measured_resolution_reaches_the_ass_header(self, ctx, tmp_path) -> None:
        """A vertical video must get a vertical play resolution, not 16:9."""
        vertical = _set(tmp_path, width=1080, height=1920)
        TranslationChain([FakeBackend("ok", chinese)]).translate(vertical, "zh-Hans", ctx)

        text = vertical.subtitle_bilingual_ass.read_text(encoding="utf-8")
        assert "PlayResX: 1080" in text
        assert "PlayResY: 1920" in text

    def test_unmeasured_geometry_falls_back_to_16_9(self, ctx, tmp_path) -> None:
        unknown = _set(tmp_path, width=None, height=None)
        TranslationChain([FakeBackend("ok", chinese)]).translate(unknown, "zh-Hans", ctx)

        text = unknown.subtitle_bilingual_ass.read_text(encoding="utf-8")
        assert "PlayResX: 1920" in text
        assert "PlayResY: 1080" in text

    def test_a_degenerate_resolution_is_not_used(self, ctx, tmp_path) -> None:
        """Zero is a measurement failure, not a resolution."""
        broken = _set(tmp_path, width=0, height=0)
        TranslationChain([FakeBackend("ok", chinese)]).translate(broken, "zh-Hans", ctx)

        text = broken.subtitle_bilingual_ass.read_text(encoding="utf-8")
        assert "PlayResX: 1920" in text

    def test_target_lang_is_passed_through(self, ctx, tmp_path) -> None:
        seen: list[str] = []

        class Recorder(FakeBackend):
            def translate_texts(self, texts, target_lang, ctx):
                seen.append(target_lang)
                return super().translate_texts(texts, target_lang, ctx)

        TranslationChain([Recorder("ok", chinese)]).translate(
            _set(tmp_path), "zh-Hant", ctx
        )
        assert seen == ["zh-Hant"]


class TestProgressAndEvents:
    """Anything the CLI or MCP renders must arrive as an event, not a print."""

    @pytest.fixture
    def collecting_ctx(self, config, tmp_path):
        events: list[Event] = []
        ctx = RunContext(
            job_id="test",
            options=JobOptions(output_dir=tmp_path / "out"),
            config=config,
            events=collect(events),
        )
        return ctx, events

    def test_progress_is_reported(self, collecting_ctx, tmp_path) -> None:
        ctx, events = collecting_ctx
        TranslationChain([FakeBackend("ok", chinese)]).translate(_set(tmp_path), "zh-Hans", ctx)

        assert any(isinstance(event, ProgressUpdated) for event in events)

    def test_the_srt_and_ass_artifacts_are_announced(self, collecting_ctx, tmp_path) -> None:
        ctx, events = collecting_ctx
        TranslationChain([FakeBackend("ok", chinese)]).translate(_set(tmp_path), "zh-Hans", ctx)

        announced = {event.kind for event in events if isinstance(event, ArtifactReady)}
        assert announced >= {
            ArtifactKind.SUBTITLE_BILINGUAL_SRT,
            ArtifactKind.SUBTITLE_ZH_SRT,
            ArtifactKind.SUBTITLE_BILINGUAL_ASS,
            ArtifactKind.SUBTITLE_ZH_ASS,
        }

    def test_announced_artifacts_exist_on_disk(self, collecting_ctx, tmp_path) -> None:
        """An announced path that was never written is worse than no announcement."""
        ctx, events = collecting_ctx
        TranslationChain([FakeBackend("ok", chinese)]).translate(_set(tmp_path), "zh-Hans", ctx)

        for event in events:
            if isinstance(event, ArtifactReady):
                assert event.path.is_file(), f"{event.path} was announced but not written"


# ----------------------------------------------------------------------
# Sentence-level translation
# ----------------------------------------------------------------------


class TestTranslationHappensOnSentences:
    """The chain translates sentences, not cues, and the cue list is replaced.

    This is the whole point of the port: a translator that sees "the output of"
    cannot know it continues into "the encoder is wrong", so fragment-by-fragment
    translation produces Chinese with inverted word order. These tests pin the
    data-shape change, which is more invasive than a content change: the number of
    output cues can differ from the number of input cues.
    """

    def test_rolling_shards_are_merged_before_translating(self, ctx, tmp_path) -> None:
        """YouTube's automatic captions arrive as two- and three-word shards.

        Sending those to a translator wastes the one thing sentence-level
        translation buys, so merging happens first and the backend sees one
        sentence where the input had three cues.
        """
        backend = FakeBackend("only", chinese)
        subtitles = _set(tmp_path, count=3)
        for index, item in enumerate(subtitles.items, 1):
            item.start_ms = index * 700
            item.end_ms = index * 700 + 400
            item.source_text = f"shard {index}"

        TranslationChain([backend]).translate(subtitles, "zh-Hans", ctx)

        assert len(backend.calls) == 1
        assert len(backend.calls[0]) == 1, "three shards should become one sentence"

    def test_a_long_sentence_is_split_back_into_several_cues(self, ctx, tmp_path) -> None:
        """Sentence-level input must not mean one enormous subtitle line.

        The Chinese is split by phrasing and the English cut to match, so a long
        sentence produces several timed cues rather than one line that runs off
        the screen.
        """
        long_zh = "这是一个非常长的句子\uff0c它包含了很多很多的内容\uff0c而且还有连接词比如例如通过以及同时\uff0c需要被拆分成多行显示"
        backend = FakeBackend("only", lambda texts: [long_zh for _ in texts])
        subtitles = _set(tmp_path, count=1)
        subtitles.items[0].start_ms = 0
        subtitles.items[0].end_ms = 8000
        subtitles.items[0].source_text = (
            "This is a very long sentence that contains a great deal of content."
        )

        result = TranslationChain([backend]).translate(subtitles, "zh-Hans", ctx)

        assert len(result.items) > 1, "a long sentence should become several cues"
        assert [item.index for item in result.items] == list(
            range(1, len(result.items) + 1)
        ), "cue indices must stay sequential across sentences"

    def test_cue_timings_stay_inside_the_original_sentence(self, ctx, tmp_path) -> None:
        """Splitting must not push a cue outside the span it came from."""
        long_zh = "这是一个非常长的句子\uff0c它包含了很多很多的内容\uff0c而且还有连接词比如例如通过以及同时\uff0c需要被拆分成多行显示"
        backend = FakeBackend("only", lambda texts: [long_zh for _ in texts])
        subtitles = _set(tmp_path, count=1)
        subtitles.items[0].start_ms = 1000
        subtitles.items[0].end_ms = 9000
        subtitles.items[0].source_text = (
            "This is a very long sentence that contains a great deal of content."
        )

        result = TranslationChain([backend]).translate(subtitles, "zh-Hans", ctx)

        assert min(item.start_ms for item in result.items) >= 1000
        assert max(item.end_ms for item in result.items) <= 9000

    def test_the_transcript_records_what_the_translator_was_given(
        self, ctx, tmp_path
    ) -> None:
        """raw/transcript.json is the first thing to read when output reads wrong."""
        backend = FakeBackend("only", chinese)
        subtitles = _set(tmp_path, count=2)

        TranslationChain([backend]).translate(subtitles, "zh-Hans", ctx)

        assert subtitles.transcript_json_path.is_file()
        assert subtitles.transcript_txt_path.is_file()
        payload = json.loads(subtitles.transcript_json_path.read_text(encoding="utf-8"))
        assert len(payload) == 2
        assert payload[0]["en_text"].startswith("English cue 1")

    def test_corrected_english_replaces_the_source(self, ctx, tmp_path) -> None:
        """An LLM also fixes ASR errors; the bilingual track must show the fix.

        Printing the uncorrected English next to a translation made from the
        corrected English would be visibly inconsistent.
        """
        subtitles = _set(tmp_path, count=1)
        subtitles.items[0].source_text = "hello world"

        class RefiningBackend(FakeBackend):
            def translate_texts(self, texts, target_lang, ctx):
                self.calls.append(list(texts))
                return TranslationOutcome(
                    texts=["你好世界。" for _ in texts],
                    origin="llm",
                    sources=["Hello, world." for _ in texts],
                )

        refining = RefiningBackend("llm", chinese)
        result = TranslationChain([refining]).translate(subtitles, "zh-Hans", ctx)

        assert result.items[0].source_text == "Hello, world."

    def test_a_wrong_length_source_list_is_ignored_not_used(self, ctx, tmp_path) -> None:
        """A mispaired list would attach one sentence's English to another's Chinese."""

        class BadSources(FakeBackend):
            def translate_texts(self, texts, target_lang, ctx):
                self.calls.append(list(texts))
                return TranslationOutcome(
                    texts=["中文" for _ in texts],
                    origin="llm",
                    sources=["only one"] * (len(texts) + 3),
                )

        subtitles = _set(tmp_path, count=2)
        result = TranslationChain([BadSources("llm", chinese)]).translate(
            subtitles, "zh-Hans", ctx
        )

        assert result.items[0].source_text.startswith("English cue 1")

    def test_a_backend_without_refinements_keeps_the_original_english(
        self, ctx, tmp_path
    ) -> None:
        backend = FakeBackend("bing", chinese)
        subtitles = _set(tmp_path, count=1)
        subtitles.items[0].source_text = "english cue 1."

        result = TranslationChain([backend]).translate(subtitles, "zh-Hans", ctx)

        # Not the input verbatim: reconstruct_sentences_from_fragments runs the
        # punctuation/capitalisation heuristic, so the leading letter is raised.
        # Asserting the *absence* of refinement is what this test is about.
        assert result.items[0].source_text == "English cue 1."

    def test_merged_shards_produce_a_single_readable_cue(self, ctx, tmp_path) -> None:
        """The user-visible payoff: three flickering shards become one line."""
        backend = FakeBackend("only", lambda texts: ["中文句子。" for _ in texts])
        subtitles = _set(tmp_path, count=3)
        for index, item in enumerate(subtitles.items, 1):
            item.start_ms = index * 700
            item.end_ms = index * 700 + 400
            item.source_text = f"shard {index}"

        result = TranslationChain([backend]).translate(subtitles, "zh-Hans", ctx)

        assert len(result.items) == 1
        assert result.items[0].start_ms == 700
        assert result.items[0].end_ms == 3 * 700 + 400
