"""The ASR chain: platform-track preference, fallback order, failure semantics.

Every backend here is a fake, so nothing touches the network or ffmpeg. What is
under test is the *chain*: which backend runs, in what order, and what counts as
success.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from porter.asr.base import AsrBackendError, AsrOutcome, coerce_items
from porter.asr.chain import (
    SOURCE_SRT_NAME,
    AsrChain,
    _best_audio,
)
from porter.config import PorterConfig
from porter.context import RunContext
from porter.errors import JobCancelled, PorterError
from porter.models.materials import RawMaterials, TaskLayout
from porter.models.metadata import VideoMetadata
from porter.models.request import JobOptions
from porter.models.subtitle import SubtitleItem
from porter.subtitles.srt import parse_srt

# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class FakeBackend:
    """A backend that answers from a script.

    ``behaviour`` is either a list of cues to return, an exception to raise, or
    a callable receiving the call count.
    """

    def __init__(
        self,
        name: str,
        behaviour=None,
        *,
        available: bool = True,
        probe_raises: bool = False,
    ) -> None:
        self.name = name
        self.behaviour = behaviour if behaviour is not None else _cues(name)
        self.is_available = available
        self.probe_raises = probe_raises
        self.calls: list[Path] = []

    def available(self, ctx: RunContext) -> bool:
        if self.probe_raises:
            raise RuntimeError(f"{self.name} probe is broken")
        return self.is_available

    def transcribe(self, audio: Path, ctx: RunContext) -> AsrOutcome:
        self.calls.append(audio)
        outcome = self.behaviour
        if callable(outcome):
            outcome = outcome(len(self.calls))
        if isinstance(outcome, Exception):
            raise outcome
        return AsrOutcome(items=list(outcome), origin=self.name)


def _cues(text: str, count: int = 2) -> list[SubtitleItem]:
    block = "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index + 1:02d},000\n{text} {index}"
        for index in range(1, count + 1)
    )
    return parse_srt(block + "\n")


@pytest.fixture
def config(tmp_path) -> PorterConfig:
    return PorterConfig(output_dir=tmp_path / "out")


@pytest.fixture
def ctx(config) -> RunContext:
    return RunContext(job_id="test", options=JobOptions(output_dir=config.output_dir), config=config)


def _raw(
    tmp_path: Path,
    *,
    platform_srt: str | None = None,
    platform_zh: str | None = None,
    enhanced: bool = True,
    with_info: bool = True,
) -> RawMaterials:
    layout = TaskLayout.build(tmp_path / "out", "vid1", "A Video")
    layout.ensure_dirs()
    (layout.raw_dir / "video.mp4").write_bytes(b"x")
    (layout.raw_dir / "audio.wav").write_bytes(b"x")

    audio = layout.raw_dir / "audio.wav"
    audio_enhanced = None
    if enhanced:
        audio_enhanced = layout.raw_dir / "audio_enhanced.wav"
        audio_enhanced.write_bytes(b"x")

    subtitle_src = None
    if platform_srt is not None:
        subtitle_src = layout.raw_dir / "subtitle.srt"
        subtitle_src.write_text(platform_srt, encoding="utf-8")

    subtitle_zh = None
    if platform_zh is not None:
        subtitle_zh = layout.raw_dir / "subtitle_zh.srt"
        subtitle_zh.write_text(platform_zh, encoding="utf-8")

    return RawMaterials(
        layout=layout,
        video=layout.raw_dir / "video.mp4",
        audio=audio,
        audio_enhanced=audio_enhanced,
        subtitle_src=subtitle_src,
        subtitle_zh=subtitle_zh,
        info=VideoMetadata(
            id="vid1",
            title="A Video",
            safe_title="A_Video",
            url="https://example.com/vid1",
            width=1080,
            height=1920,
        )
        if with_info
        else None,
    )


PLATFORM_SRT = "1\n00:00:01,000 --> 00:00:02,000\nAuthor's own caption\n"
PLATFORM_ZH = "1\n00:00:01,000 --> 00:00:02,000\n作者自己的字幕\n"


# ----------------------------------------------------------------------
# Availability
# ----------------------------------------------------------------------


class TestAvailability:
    def test_no_backends_means_unavailable(self, ctx) -> None:
        assert AsrChain().available(ctx) is False

    def test_one_available_backend_is_enough(self, ctx) -> None:
        chain = AsrChain([FakeBackend("a", available=False), FakeBackend("b")])
        assert chain.available(ctx) is True

    def test_a_broken_probe_does_not_abort_the_chain(self, ctx) -> None:
        """The protocol says available() never raises; a violation must be survivable."""
        chain = AsrChain([FakeBackend("broken", probe_raises=True), FakeBackend("good")])
        assert chain.available(ctx) is True

    def test_a_broken_probe_does_not_abort_transcription(self, ctx, tmp_path) -> None:
        chain = AsrChain([FakeBackend("broken", probe_raises=True), FakeBackend("good")])
        result = chain.transcribe(_raw(tmp_path), ctx)
        assert result.items

    def test_add_returns_self_for_assembly(self) -> None:
        chain = AsrChain()
        assert chain.add(FakeBackend("a")) is chain


# ----------------------------------------------------------------------
# Platform track preference
# ----------------------------------------------------------------------


class TestPlatformTrackWins:
    def test_the_platform_track_is_preferred_over_asr(self, ctx, tmp_path) -> None:
        """The author's text beats a transcription of the same audio."""
        backend = FakeBackend("whisper")
        chain = AsrChain([backend])

        result = chain.transcribe(_raw(tmp_path, platform_srt=PLATFORM_SRT), ctx)

        assert backend.calls == [], "no engine should run when a track exists"
        assert result.items[0].source_text == "Author's own caption"

    def test_used_asr_is_false_for_a_platform_track(self, ctx, tmp_path) -> None:
        """Nothing was recognised, so reporting ASR would mislabel the provenance."""
        result = AsrChain([FakeBackend("whisper")]).transcribe(
            _raw(tmp_path, platform_srt=PLATFORM_SRT), ctx
        )
        assert result.used_asr is False

    def test_used_asr_is_true_for_a_backend(self, ctx, tmp_path) -> None:
        result = AsrChain([FakeBackend("whisper")]).transcribe(_raw(tmp_path), ctx)
        assert result.used_asr is True

    def test_an_unparseable_track_falls_back_to_asr(self, ctx, tmp_path) -> None:
        """A truncated download is normal and must not fail the job."""
        backend = FakeBackend("whisper")
        result = AsrChain([backend]).transcribe(
            _raw(tmp_path, platform_srt="not a subtitle file at all"), ctx
        )

        assert backend.calls, "ASR should have run"
        assert result.used_asr is True

    def test_an_empty_track_falls_back_to_asr(self, ctx, tmp_path) -> None:
        result = AsrChain([FakeBackend("whisper")]).transcribe(
            _raw(tmp_path, platform_srt="   \n"), ctx
        )
        assert result.used_asr is True

    def test_a_missing_track_falls_back_to_asr(self, ctx, tmp_path) -> None:
        assert AsrChain([FakeBackend("whisper")]).transcribe(_raw(tmp_path), ctx).used_asr


class TestPlatformChineseTrack:
    """A platform zh track is aligned in TRANSCRIBE, so TRANSLATE can skip entirely."""

    def test_aligned_cues_carry_target_text(self, ctx, tmp_path) -> None:
        result = AsrChain([FakeBackend("whisper")]).transcribe(
            _raw(tmp_path, platform_srt=PLATFORM_SRT, platform_zh=PLATFORM_ZH), ctx
        )
        assert "作者自己的字幕" in result.items[0].target_text

    def test_the_source_track_is_still_what_is_transcribed(self, ctx, tmp_path) -> None:
        result = AsrChain([FakeBackend("whisper")]).transcribe(
            _raw(tmp_path, platform_srt=PLATFORM_SRT, platform_zh=PLATFORM_ZH), ctx
        )
        assert result.items[0].source_text == "Author's own caption"

    def test_a_zh_track_without_a_source_track_is_ignored(self, ctx, tmp_path) -> None:
        """There is nothing to align onto, so this must not be treated as a source."""
        result = AsrChain([FakeBackend("whisper")]).transcribe(
            _raw(tmp_path, platform_zh=PLATFORM_ZH), ctx
        )
        assert result.used_asr is True


# ----------------------------------------------------------------------
# Fallback order
# ----------------------------------------------------------------------


class TestFallback:
    def test_the_first_working_backend_wins(self, ctx, tmp_path) -> None:
        first, second = FakeBackend("first"), FakeBackend("second")
        chain = AsrChain([first, second])

        result = chain.transcribe(_raw(tmp_path), ctx)

        assert first.calls and not second.calls
        assert result.items[0].source_text.startswith("first")

    def test_a_backend_error_advances_to_the_next(self, ctx, tmp_path) -> None:
        failing = FakeBackend("failing", AsrBackendError("failing", "429"))
        working = FakeBackend("working")
        chain = AsrChain([failing, working])

        assert chain.transcribe(_raw(tmp_path), ctx).items

    def test_a_porter_error_advances_to_the_next(self, ctx, tmp_path) -> None:
        chain = AsrChain(
            [FakeBackend("failing", PorterError("nope")), FakeBackend("working")]
        )
        assert chain.transcribe(_raw(tmp_path), ctx).items

    def test_an_unavailable_backend_is_never_called(self, ctx, tmp_path) -> None:
        skipped = FakeBackend("skipped", available=False)
        chain = AsrChain([skipped, FakeBackend("working")])

        chain.transcribe(_raw(tmp_path), ctx)
        assert skipped.calls == []

    def test_empty_output_from_a_backend_advances_to_the_next(self, ctx, tmp_path) -> None:
        """HTTP 200 with no utterances is the usual shape of a quota error.

        Treating it as success is how a job reports DONE with an empty subtitle
        file, so an empty list is a *failure* here, not a result.
        """
        empty = FakeBackend("empty", [])
        working = FakeBackend("working")
        chain = AsrChain([empty, working])

        result = chain.transcribe(_raw(tmp_path), ctx)

        assert empty.calls, "the empty backend should have been tried"
        assert result.items, "and the chain should have moved on"
        assert result.items[0].source_text.startswith("working")

    def test_every_backend_failing_raises_with_the_reasons(self, ctx, tmp_path) -> None:
        """The operator needs to know *why* each engine failed, not just that none did."""
        chain = AsrChain(
            [
                FakeBackend("a", AsrBackendError("a", "quota exhausted")),
                FakeBackend("b", AsrBackendError("b", "endpoint is dead")),
            ]
        )

        with pytest.raises(PorterError) as excinfo:
            chain.transcribe(_raw(tmp_path), ctx)

        message = str(excinfo.value)
        assert "quota exhausted" in message
        assert "endpoint is dead" in message

    def test_an_unexpected_exception_propagates(self, ctx, tmp_path) -> None:
        """A bug in our own code must not be silently swallowed as a fallback."""
        broken = FakeBackend("broken", TypeError("our bug"))
        chain = AsrChain([broken, FakeBackend("working")])

        with pytest.raises(TypeError):
            chain.transcribe(_raw(tmp_path), ctx)

    def test_all_backends_empty_raises_rather_than_returning_nothing(self, ctx, tmp_path) -> None:
        chain = AsrChain([FakeBackend("a", []), FakeBackend("b", [])])

        with pytest.raises(PorterError) as excinfo:
            chain.transcribe(_raw(tmp_path), ctx)
        assert "returned no cues" in str(excinfo.value)


class TestCancellation:
    """The bug the P2 suite caught in the platform fetcher: JobCancelled swallowed."""

    def test_cancellation_is_not_treated_as_a_backend_failure(self, ctx, tmp_path) -> None:
        cancelling = FakeBackend("cancelling", JobCancelled("stopped"))
        chain = AsrChain([cancelling, FakeBackend("working")])

        with pytest.raises(JobCancelled):
            chain.transcribe(_raw(tmp_path), ctx)

    def test_a_cancelled_job_does_not_try_the_next_backend(self, ctx, tmp_path) -> None:
        """Otherwise Ctrl-C costs the user four more network round-trips."""
        cancelling = FakeBackend("cancelling", JobCancelled("stopped"))
        after = FakeBackend("after")
        chain = AsrChain([cancelling, after])

        with pytest.raises(JobCancelled):
            chain.transcribe(_raw(tmp_path), ctx)

        assert after.calls == []

    def test_a_cancelled_context_stops_before_any_backend(self, ctx, tmp_path) -> None:
        backend = FakeBackend("a")
        chain = AsrChain([backend])
        ctx.request_cancel()

        with pytest.raises(JobCancelled):
            chain.transcribe(_raw(tmp_path), ctx)
        assert backend.calls == []


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------


class TestOutput:
    def test_the_source_srt_is_written_into_cooked(self, ctx, tmp_path) -> None:
        raw = _raw(tmp_path)
        chain = AsrChain([FakeBackend("whisper")])
        chain.transcribe(raw, ctx)

        written = raw.layout.cooked_dir / SOURCE_SRT_NAME
        assert written.is_file()
        assert "whisper 1" in written.read_text(encoding="utf-8")

    def test_the_ass_paths_are_set_but_not_yet_written(self, ctx, tmp_path) -> None:
        """TRANSLATE owns ASS, but the paths must exist so it can run alone."""
        result = AsrChain([FakeBackend("whisper")]).transcribe(_raw(tmp_path), ctx)

        assert result.subtitle_bilingual_ass.name == "subtitle_bilingual.ass"
        assert not result.subtitle_bilingual_ass.exists()

    def test_the_measured_geometry_is_carried_forward(self, ctx, tmp_path) -> None:
        """ASS PlayResX/Y must match the video or every subtitle scales wrong."""
        result = AsrChain([FakeBackend("whisper")]).transcribe(_raw(tmp_path), ctx)
        assert (result.video_width, result.video_height) == (1080, 1920)

    def test_unmeasured_geometry_stays_none_and_is_not_invented(self, ctx, tmp_path) -> None:
        """v0.1 fabricated (1920, 1080) here, which styled vertical videos wrong."""
        result = AsrChain([FakeBackend("whisper")]).transcribe(
            _raw(tmp_path, with_info=False), ctx
        )
        assert result.video_width is None
        assert result.video_height is None

    def test_cues_are_renumbered_contiguously(self, ctx, tmp_path) -> None:
        result = AsrChain([FakeBackend("whisper")]).transcribe(_raw(tmp_path), ctx)
        assert [item.index for item in result.items] == list(range(1, len(result.items) + 1))

    def test_coerce_drops_cues_with_no_text(self) -> None:
        items = [
            SubtitleItem(1, 0, 1000, "keep", ""),
            SubtitleItem(2, 1000, 2000, "   ", ""),
            SubtitleItem(3, 2000, 3000, "also keep", ""),
        ]
        assert [item.source_text for item in coerce_items(items)] == ["keep", "also keep"]

    def test_coerce_drops_zero_length_cues(self) -> None:
        """A cue with no duration renders a single frame of unreadable text."""
        items = [SubtitleItem(1, 1000, 1000, "instant", "")]
        assert coerce_items(items) == []


class TestEnhancedAudioPreference:
    def test_the_enhanced_wav_is_preferred(self, tmp_path) -> None:
        raw = _raw(tmp_path)
        assert _best_audio(raw) == raw.audio_enhanced

    def test_the_raw_wav_is_used_when_enhancement_is_missing(self, tmp_path) -> None:
        """Enhancement is best-effort, so its absence must not break transcription."""
        raw = _raw(tmp_path, enhanced=False)
        assert _best_audio(raw) == raw.audio

    def test_a_backend_receives_the_enhanced_wav(self, ctx, tmp_path) -> None:
        backend = FakeBackend("whisper")
        raw = _raw(tmp_path)
        AsrChain([backend]).transcribe(raw, ctx)
        assert backend.calls == [raw.audio_enhanced]
