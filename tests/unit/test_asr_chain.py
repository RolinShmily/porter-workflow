"""The ASR chain: platform-track preference, fallback order, failure semantics.

Every backend here is a fake, so nothing touches the network or ffmpeg. What is
under test is the *chain*: which backend runs, in what order, and what counts as
success.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from porter.asr.base import AsrBackendError, AsrOutcome, coerce_items
from porter.asr.chain import (
    PROVENANCE_NAME,
    SOURCE_SRT_NAME,
    AsrChain,
    _best_audio,
)
from porter.config import PorterConfig
from porter.context import RunContext
from porter.errors import JobCancelled, PorterError
from porter.events import Phase
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


# ----------------------------------------------------------------------
# Reuse of the source cues a previous run left on disk
# ----------------------------------------------------------------------


class TestTranscribeReuse:
    """§13.50: re-running a job used to redo recognition every time.

    Measured on a 74-second clip: 16 s of local Whisper on the second run, with
    nothing about the audio or the ASR configuration changed. On a long video it
    is minutes. The fix follows BURN's existing rule -- reuse when the artifact is
    at least as new as its inputs -- plus one thing the SRT cannot carry.
    """

    def _previous_run(self, ctx, tmp_path, *, text: str = "cached cue") -> RawMaterials:
        """Run the chain once, leaving a cached SRT and its sidecar behind."""
        raw = _raw(tmp_path)
        chain = AsrChain([FakeBackend("whisper", _cues(text))])
        chain.transcribe(raw, ctx)
        return raw

    def test_a_fresh_cache_is_reused_without_calling_a_backend(self, ctx, tmp_path) -> None:
        raw = self._previous_run(ctx, tmp_path)
        backend = FakeBackend("whisper")

        result = AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls == [], "a backend ran despite a usable cache"
        assert result.items

    def test_the_reused_cues_are_the_ones_on_disk(self, ctx, tmp_path) -> None:
        raw = self._previous_run(ctx, tmp_path, text="from the cache")

        result = AsrChain([FakeBackend("whisper")]).transcribe(raw, ctx)

        assert any("from the cache" in item.source_text for item in result.items)

    def test_the_geometry_still_comes_from_the_prepare_metadata(self, ctx, tmp_path) -> None:
        """Restoring must not lose the measured dimensions TRANSLATE needs."""
        raw = self._previous_run(ctx, tmp_path)

        result = AsrChain([FakeBackend("whisper")]).transcribe(raw, ctx)

        assert (result.video_width, result.video_height) == (1080, 1920)

    def test_force_re_runs_recognition(self, ctx, tmp_path) -> None:
        """``--force`` is the documented escape hatch and must always win."""
        raw = self._previous_run(ctx, tmp_path)
        backend = FakeBackend("whisper")
        ctx.options = JobOptions(output_dir=ctx.output_root, force=True)

        AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls, "--force returned the cache"

    def test_only_phase_transcribe_re_runs_recognition(self, ctx, tmp_path) -> None:
        """Asking for a phase is asking to *run* it, not to be handed a cache.

        Restoring the prerequisites is the point of single-phase recovery; making
        the requested phase itself a cache hit would turn ``--only-phase
        transcribe`` into a silent no-op, which is worse than not having the flag.
        """
        raw = self._previous_run(ctx, tmp_path)
        backend = FakeBackend("whisper")
        ctx.options = JobOptions(output_dir=ctx.output_root, only_phase=Phase.TRANSCRIBE)

        AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls, "--only-phase transcribe returned the cache"

    def test_another_phase_being_requested_still_reuses(self, ctx, tmp_path) -> None:
        """The counterpart: ``--only-phase burn`` must skip recognition."""
        raw = self._previous_run(ctx, tmp_path)
        backend = FakeBackend("whisper")
        ctx.options = JobOptions(output_dir=ctx.output_root, only_phase=Phase.BURN)

        AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls == []

    def test_a_missing_sidecar_means_no_reuse(self, ctx, tmp_path, monkeypatch) -> None:
        """Reuse only what can be described honestly.

        ``used_asr`` is reported to the operator ("did this job pay for
        Whisper?"). A task directory from an older build has the SRT but no record
        of where it came from, so it re-transcribes once rather than inventing an
        answer.

        The assertion on the log is what makes this test *about the gate*. Without
        it the test passed even with the gate removed, because the sidecar read
        failed instead -- green for a reason that had nothing to do with the
        check. An old cache is a normal cache miss and must not be reported as a
        problem.
        """
        import porter.asr.chain as chain_module

        warnings: list[str] = []

        class _Recorder:
            def warning(self, message: str, *args: object) -> None:
                warnings.append(message % args if args else message)

            def __getattr__(self, _name: str):
                return lambda *args, **kwargs: None

        monkeypatch.setattr(chain_module, "_logger", _Recorder())
        raw = self._previous_run(ctx, tmp_path)
        (raw.layout.cooked_dir / PROVENANCE_NAME).unlink()
        backend = FakeBackend("whisper")

        AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls, "reused a cache whose provenance was unknown"
        assert warnings == [], f"an absent sidecar was reported as a problem: {warnings}"

    def test_a_missing_srt_means_no_reuse(self, ctx, tmp_path) -> None:
        raw = self._previous_run(ctx, tmp_path)
        (raw.layout.cooked_dir / SOURCE_SRT_NAME).unlink()
        backend = FakeBackend("whisper")

        AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls

    def test_cues_older_than_the_audio_are_not_reused(self, ctx, tmp_path) -> None:
        """Same freshness rule BURN applies to a release video.

        A re-extracted master audio means the cues describe audio that no longer
        exists. The comparison is against ``raw.audio`` -- the standardised master
        -- and not against the enhanced copy, because PREPARE re-runs the
        enhancement every invocation and that file therefore gets a fresh mtime
        each time. Comparing against it made the rule unsatisfiable; a real run
        caught that, not this test.
        """
        raw = self._previous_run(ctx, tmp_path)
        backend = FakeBackend("whisper")
        future = (raw.layout.cooked_dir / SOURCE_SRT_NAME).stat().st_mtime + 60
        os.utime(raw.audio, (future, future))

        AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls, "reused cues older than the audio they describe"

    def test_the_enhanced_copy_does_not_invalidate_the_cache(self, ctx, tmp_path) -> None:
        """PREPARE rewrites ``audio_enhanced.wav`` on every run.

        Treating that as a change would re-transcribe every single time, which is
        exactly the behaviour this feature exists to remove.
        """
        raw = self._previous_run(ctx, tmp_path)
        backend = FakeBackend("whisper")
        enhanced = raw.audio_enhanced
        assert enhanced is not None
        future = (raw.layout.cooked_dir / SOURCE_SRT_NAME).stat().st_mtime + 60
        os.utime(enhanced, (future, future))

        AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls == [], "a regenerated enhancement invalidated the cache"

    def test_the_provenance_is_restored_not_guessed(self, ctx, tmp_path) -> None:
        raw = self._previous_run(ctx, tmp_path)
        sidecar = raw.layout.cooked_dir / PROVENANCE_NAME

        recorded = json.loads(sidecar.read_text(encoding="utf-8"))
        result = AsrChain([FakeBackend("whisper")]).transcribe(raw, ctx)

        assert recorded["used_asr"] is True
        assert result.used_asr is True

    def test_a_platform_track_is_recorded_as_not_asr(self, ctx, tmp_path) -> None:
        """The distinction the sidecar exists for."""
        raw = _raw(tmp_path, platform_srt=PLATFORM_SRT)
        AsrChain([FakeBackend("whisper")]).transcribe(raw, ctx)
        sidecar = raw.layout.cooked_dir / PROVENANCE_NAME

        assert json.loads(sidecar.read_text(encoding="utf-8"))["used_asr"] is False

        result = AsrChain([FakeBackend("whisper")]).transcribe(raw, ctx)
        assert result.used_asr is False

    def test_a_corrupt_sidecar_means_no_reuse(self, ctx, tmp_path) -> None:
        raw = self._previous_run(ctx, tmp_path)
        (raw.layout.cooked_dir / PROVENANCE_NAME).write_text("{not json", encoding="utf-8")
        backend = FakeBackend("whisper")

        AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls

    def test_an_empty_cached_srt_means_no_reuse(self, ctx, tmp_path) -> None:
        """A truncated file must not become a job with zero cues."""
        raw = self._previous_run(ctx, tmp_path)
        (raw.layout.cooked_dir / SOURCE_SRT_NAME).write_text("", encoding="utf-8")
        backend = FakeBackend("whisper")

        AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls


# ----------------------------------------------------------------------
# An explicitly supplied source track (§13.51)
# ----------------------------------------------------------------------


class TestSuppliedSubtitleFile:
    """``--subtitle-file``: the escape hatch §13.29 left open.

    A ``.srt`` beside a local video is still not picked up automatically -- it
    could be the source or the translation, and guessing wrong either skips ASR
    for no reason or overwrites the user's file. Naming the file removes the
    ambiguity rather than resolving it by guesswork.
    """

    def _supplied(self, tmp_path: Path, text: str, name: str = "source.srt") -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_it_is_used_instead_of_the_platform_track(self, ctx, tmp_path) -> None:
        """A direct instruction beats what we would otherwise have to guess at."""
        raw = _raw(tmp_path, platform_srt=PLATFORM_SRT)
        supplied = self._supplied(tmp_path, "1\n00:00:03,000 --> 00:00:04,000\nFrom my own file\n")
        ctx.options = JobOptions(output_dir=ctx.output_root, subtitle_file=supplied)
        backend = FakeBackend("whisper")

        result = AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls == [], "ASR ran despite an explicit subtitle file"
        assert [item.source_text for item in result.items] == ["From my own file"]

    def test_it_is_not_marked_as_asr(self, ctx, tmp_path) -> None:
        """Provenance is reported; "did this job pay for Whisper?" must be answerable."""
        raw = _raw(tmp_path)
        supplied = self._supplied(tmp_path, "1\n00:00:03,000 --> 00:00:04,000\nMine\n")
        ctx.options = JobOptions(output_dir=ctx.output_root, subtitle_file=supplied)

        result = AsrChain([FakeBackend("whisper")]).transcribe(raw, ctx)

        assert result.used_asr is False

    def test_webvtt_is_converted(self, ctx, tmp_path) -> None:
        raw = _raw(tmp_path)
        vtt = "WEBVTT\n\n00:00:03.000 --> 00:00:04.000\nFrom webvtt\n"
        supplied = self._supplied(tmp_path, vtt, name="source.vtt")
        ctx.options = JobOptions(output_dir=ctx.output_root, subtitle_file=supplied)

        result = AsrChain([FakeBackend("whisper")]).transcribe(raw, ctx)

        assert [item.source_text for item in result.items] == ["From webvtt"]

    def test_a_missing_file_fails_instead_of_falling_back_to_asr(self, ctx, tmp_path) -> None:
        """A typo must not be hidden behind minutes of recognition.

        ``load_platform_subtitles`` returns ``[]`` on anything unreadable because
        "no platform track" is normal. This one raises, because the user asked for
        this exact file.
        """
        raw = _raw(tmp_path)
        backend = FakeBackend("whisper")
        ctx.options = JobOptions(
            output_dir=ctx.output_root, subtitle_file=tmp_path / "nope.srt"
        )

        with pytest.raises(PorterError) as caught:
            AsrChain([backend]).transcribe(raw, ctx)

        assert "not found" in str(caught.value)
        assert backend.calls == []

    def test_an_unsupported_format_is_refused_with_the_supported_list(
        self, ctx, tmp_path
    ) -> None:
        raw = _raw(tmp_path)
        supplied = self._supplied(tmp_path, "whatever", name="source.ass")
        ctx.options = JobOptions(output_dir=ctx.output_root, subtitle_file=supplied)

        with pytest.raises(PorterError) as caught:
            AsrChain([FakeBackend("whisper")]).transcribe(raw, ctx)

        assert caught.value.details["supported"] == [".srt", ".vtt"]

    def test_an_empty_file_is_refused(self, ctx, tmp_path) -> None:
        """An empty track would produce a job with no text at all."""
        raw = _raw(tmp_path)
        supplied = self._supplied(tmp_path, "")
        ctx.options = JobOptions(output_dir=ctx.output_root, subtitle_file=supplied)

        with pytest.raises(PorterError) as caught:
            AsrChain([FakeBackend("whisper")]).transcribe(raw, ctx)

        assert "no cues" in str(caught.value)

    def test_editing_the_supplied_file_invalidates_the_cache(self, ctx, tmp_path) -> None:
        """The user's correction must not be ignored in favour of the old cues."""
        raw = _raw(tmp_path)
        supplied = self._supplied(tmp_path, "1\n00:00:03,000 --> 00:00:04,000\nFirst\n")
        ctx.options = JobOptions(output_dir=ctx.output_root, subtitle_file=supplied)
        chain = AsrChain([FakeBackend("whisper")])
        chain.transcribe(raw, ctx)

        supplied.write_text("1\n00:00:03,000 --> 00:00:04,000\nCorrected\n", encoding="utf-8")
        future = (raw.layout.cooked_dir / SOURCE_SRT_NAME).stat().st_mtime + 60
        os.utime(supplied, (future, future))

        result = chain.transcribe(raw, ctx)

        assert [item.source_text for item in result.items] == ["Corrected"]

    def test_an_unedited_supplied_file_is_still_reused(self, ctx, tmp_path) -> None:
        """The counterpart: the freshness check must not defeat the cache."""
        raw = _raw(tmp_path)
        supplied = self._supplied(tmp_path, "1\n00:00:03,000 --> 00:00:04,000\nMine\n")
        ctx.options = JobOptions(output_dir=ctx.output_root, subtitle_file=supplied)
        chain = AsrChain([FakeBackend("whisper")])
        chain.transcribe(raw, ctx)
        backend = FakeBackend("whisper")

        result = AsrChain([backend]).transcribe(raw, ctx)

        assert backend.calls == []
        assert [item.source_text for item in result.items] == ["Mine"]

    def test_the_provenance_sidecar_records_that_it_was_supplied(self, ctx, tmp_path) -> None:
        raw = _raw(tmp_path)
        supplied = self._supplied(tmp_path, "1\n00:00:03,000 --> 00:00:04,000\nMine\n")
        ctx.options = JobOptions(output_dir=ctx.output_root, subtitle_file=supplied)

        AsrChain([FakeBackend("whisper")]).transcribe(raw, ctx)

        sidecar = json.loads(
            (raw.layout.cooked_dir / PROVENANCE_NAME).read_text(encoding="utf-8")
        )
        assert sidecar == {"used_asr": False, "origin": "supplied"}


class TestNoCuesIsActionable:
    """The failure has to name a way out, not just report the symptom.

    With no key-free ASR engine installed and a video carrying no subtitle track
    there is nothing left to try. §13.51 added ``--subtitle-file`` as that way out,
    so the message points at it (and at the ``[asr-local]`` extra).
    """

    def test_the_hint_names_both_ways_out(self, ctx, tmp_path) -> None:
        with pytest.raises(PorterError) as caught:
            AsrChain([FakeBackend("empty", [])]).transcribe(_raw(tmp_path), ctx)

        hint = str(caught.value.details["hint"])
        assert "--subtitle-file" in hint
        assert "asr-local" in hint

    def test_the_failed_backends_are_still_reported(self, ctx, tmp_path) -> None:
        """The hint is additive: the diagnosis must survive.

        ``_run`` reports ``attempted``/``failures`` while ``_write`` reports
        ``backends`` -- they are different paths, and asserting on the wrong one is
        how the hint first ended up on the path a real job never reaches.
        """
        with pytest.raises(PorterError) as caught:
            AsrChain([FakeBackend("empty", [])]).transcribe(_raw(tmp_path), ctx)

        assert caught.value.details["attempted"] == 1
        assert caught.value.details["failures"] == ["empty: returned no cues"]
