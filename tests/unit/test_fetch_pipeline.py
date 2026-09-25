"""``YtDlpExtractor.fetch`` — the pipeline that replaced five pasted copies.

The harness is deliberately *behavioural* rather than mock-per-call:

* ``build_ydl`` is replaced by a fake that reads the policy it is handed and
  creates the files that policy implies (an ``outtmpl`` under ``.tmp`` for media,
  ``sub.srt`` for a subtitle fetch) — the same contract real yt-dlp honours.
* ffmpeg is replaced by a runner that creates the file named as the last argv
  element, so the *real* ``standardize_video``/``extract_audio``/
  ``enhance_for_asr`` code runs and its argv building is exercised.

That means these tests fail if the orchestration is wrong *or* if the media
layer's contract changes, without any network or any real encoding.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from porter.context import RunContext
from porter.errors import ExtractionError, JobCancelled
from porter.media.ffmpeg import FFmpegRunner, FFmpegTools
from porter.media.prepare import (
    AUDIO_NAME,
    ENHANCED_AUDIO_NAME,
    METADATA_NAME,
    VIDEO_NAME,
)
from porter.models.request import JobOptions
from porter.platforms.base import (
    SUBTITLE_NAME,
    SUBTITLE_ZH_NAME,
    YtDlpExtractor,
)
from porter.platforms.bilibili import SPEC as BILIBILI
from porter.platforms.registry import PlatformRegistry
from porter.platforms.tiktok import SPEC as TIKTOK
from porter.platforms.x import SPEC as X
from porter.platforms.youtube import SPEC as YOUTUBE

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

FAKE_SRT = "1\n00:00:00,000 --> 00:00:01,000\nhello\n"

#: Any file the engine produces must clear these thresholds.
_PLAUSIBLE_MEDIA = b"\x00" * 8192


def info_dict(**overrides: Any) -> dict[str, Any]:
    """A minimal but realistic yt-dlp info dict."""
    base: dict[str, Any] = {
        "id": "dQw4w9WgXcQ",
        "title": "A Test Video",
        "duration": 12.5,
        "uploader": "Someone",
        "channel": "Some Channel",
        "width": 1920,
        "height": 1080,
        "description": "desc",
        "thumbnail": None,
        "formats": [{"format_id": "137"}],
        "subtitles": {},
        "automatic_captions": {},
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------


class StubRunner(FFmpegRunner):
    """An ffmpeg that does no encoding but honours the file contract.

    ``standardize_video``, ``extract_audio`` and ``enhance_for_asr`` all write to
    a path that is the last argv element, so creating that file makes the real
    media code run to completion. Probing is answered from a canned shape.
    """

    def __init__(self, *, width: int = 1920, height: int = 1080) -> None:
        super().__init__(FFmpegTools(ffmpeg="ffmpeg", ffprobe="ffprobe"))
        self.width = width
        self.height = height
        self.calls: list[list[str]] = []

    def run(self, args, *, what, check=True, timeout=None):
        self.calls.append(args)
        if "-filters" in args or "-encoders" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        target = Path(args[-1])
        if str(target) != "-":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_PLAUSIBLE_MEDIA)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def probe_streams(self, path):
        return [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": self.width,
                "height": self.height,
                "duration": "12.5",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ]

    def probe_format(self, path):
        return {"duration": "12.5", "format_name": "mov,mp4", "size": "8192"}

    def commands_for(self, needle: str) -> list[list[str]]:
        return [c for c in self.calls if any(needle in a for a in c)]


class FakeYDL:
    """Stands in for ``yt_dlp.YoutubeDL``.

    Derives its side effects from the policy it receives, exactly as the real
    library does: ``skip_download`` + ``write_subtitles`` produces a subtitle
    file, anything else produces a media file at ``outtmpl``.
    """

    def __init__(self, state: SimpleNamespace, policy: Any, progress_hook: Any) -> None:
        self._state = state
        self._policy = policy
        self._progress_hook = progress_hook

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any] | None:
        self._state.extract_calls.append(url)
        if self._state.raise_on_extract is not None:
            raise self._state.raise_on_extract
        return self._state.info

    def download(self, urls: list[str]) -> None:
        self._state.download_policies.append(self._policy)
        self._state.download_calls.append(urls)

        if self._policy.skip_download:
            if self._state.raise_on_subtitle_download is not None:
                raise self._state.raise_on_subtitle_download
        elif self._state.raise_on_download is not None:
            raise self._state.raise_on_download

        if self._progress_hook is not None:
            self._progress_hook(
                {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100}
            )
            self._progress_hook({"status": "finished"})

        tmpl = self._policy.outtmpl
        if not tmpl:
            return
        directory = Path(tmpl).parent
        directory.mkdir(parents=True, exist_ok=True)

        if self._policy.skip_download:
            wants_subs = self._policy.write_subtitles or self._policy.write_auto_subs
            if wants_subs and self._state.subtitle_payload is not None:
                # The language tag is part of the filename. yt-dlp writes
                # ``sub.en.srt`` for ``outtmpl="sub.%(ext)s"``, and this fake used
                # to write ``sub.srt`` -- which is what the production code
                # searched for, so the two agreed and both were wrong. Every
                # subtitle fetch silently found nothing for as long as that held.
                lang = (self._policy.subtitle_langs or ["und"])[0]
                target = directory / f"sub.{lang}.{self._state.subtitle_extension}"
                # utf-8, like the product. This fixture used the locale encoding,
                # which on a GBK console silently wrote the payload as GBK -- so
                # the product read it as utf-8-with-replace, got replacement
                # characters, and the two bugs masked each other.
                target.write_text(self._state.subtitle_payload, encoding="utf-8")
            return

        if self._state.media_payload is not None:
            (directory / "download.mp4").write_bytes(self._state.media_payload)


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        info=info_dict(),
        extract_calls=[],
        download_calls=[],
        download_policies=[],
        raise_on_extract=None,
        raise_on_download=None,
        raise_on_subtitle_download=None,
        media_payload=_PLAUSIBLE_MEDIA,
        subtitle_payload=FAKE_SRT,
        subtitle_extension="srt",
    )

    def fake_build(policy=None, *, logger=None, progress_hook=None, extra=None):
        return FakeYDL(state, policy, progress_hook)

    monkeypatch.setattr("porter.platforms.base.build_ydl", fake_build)
    return state


@pytest.fixture
def runner() -> StubRunner:
    return StubRunner()


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(job_id="test-job", options=JobOptions(output_dir=tmp_path / "out"))


@pytest.fixture
def extractor() -> YtDlpExtractor:
    return YtDlpExtractor(YOUTUBE)


def raw_dir(ctx: RunContext) -> Path:
    """The single task directory under the output root."""
    tasks = list(ctx.output_root.iterdir())
    assert len(tasks) == 1, f"expected one task dir, found {tasks}"
    return tasks[0] / "raw"


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestFetchProducesTheContract:
    def test_creates_the_documented_raw_assets(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        materials = extractor.fetch(URL, ctx, runner=runner)

        assert materials.video.name == VIDEO_NAME
        assert materials.audio.name == AUDIO_NAME
        assert materials.video.is_file()
        assert materials.audio.is_file()
        assert materials.metadata_path is not None
        assert materials.metadata_path.name == METADATA_NAME
        assert materials.metadata_path.is_file()

    def test_enhanced_audio_is_produced(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        materials = extractor.fetch(URL, ctx, runner=runner)
        assert materials.audio_enhanced is not None
        assert materials.audio_enhanced.name == ENHANCED_AUDIO_NAME
        assert materials.audio_enhanced.is_file()

    def test_task_directory_is_video_id_underscore_title(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        extractor.fetch(URL, ctx, runner=runner)
        assert [p.name for p in ctx.output_root.iterdir()] == ["dQw4w9WgXcQ_A_Test_Video"]

    def test_master_goes_through_the_standardiser(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        extractor.fetch(URL, ctx, runner=runner)
        assert runner.commands_for("-movflags"), "master was not written with faststart"

    def test_audio_is_extracted_at_the_configured_rate(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        extractor.fetch(URL, ctx, runner=runner)
        wav_commands = [c for c in runner.calls if "pcm_s16le" in c]
        assert any("16000" in c for c in wav_commands), "audio.wav is not 16 kHz"

    def test_tmp_is_cleaned_up(self, extractor, ctx, runner, fake_ydl) -> None:
        materials = extractor.fetch(URL, ctx, runner=runner)
        assert not materials.task_dir.joinpath(".tmp").exists()

    def test_metadata_json_round_trips(self, extractor, ctx, runner, fake_ydl) -> None:
        materials = extractor.fetch(URL, ctx, runner=runner)
        payload = json.loads(materials.metadata_path.read_text(encoding="utf-8"))
        assert payload["id"] == "dQw4w9WgXcQ"
        assert payload["platform"] == "youtube"
        assert payload["title"] == "A Test Video"

    def test_emits_progress_and_artifact_events(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        events: list[Any] = []
        ctx.events = events.append
        extractor.fetch(URL, ctx, runner=runner)

        assert any(e.type == "progress_updated" for e in events)
        assert any(e.type == "artifact_ready" for e in events)
        # Download progress from the yt-dlp hook must survive translation.
        assert any(
            e.type == "artifact_ready" and e.kind.value == "video" for e in events
        )


class TestMeasuredOrientation:
    def test_dimensions_come_from_the_master_not_the_info_dict(
        self, extractor, ctx, fake_ydl
    ) -> None:
        """The info dict said 1920x1080; the file says 1080x1920."""
        fake_ydl.info = info_dict(width=1920, height=1080)
        runner = StubRunner(width=1080, height=1920)

        materials = extractor.fetch(URL, ctx, runner=runner)

        assert materials.info is not None
        assert (materials.info.width, materials.info.height) == (1080, 1920)
        assert materials.info.is_vertical is True

    def test_spec_default_is_kept_when_the_info_dict_has_no_dimensions(
        self, ctx, fake_ydl
    ) -> None:
        """TikTok reports nothing; the spec's 9:16 assumption must survive."""
        fake_ydl.info = info_dict(
            id="123", title="tt", width=None, height=None, formats=[{"format_id": "x"}]
        )
        runner = StubRunner()
        # The stub cannot measure either, so the declared default is all we have.
        runner.probe_streams = lambda path: [{"codec_type": "video", "codec_name": "h264"}]

        materials = YtDlpExtractor(TIKTOK).fetch(
            "https://www.tiktok.com/@u/video/123", ctx, runner=runner
        )

        assert materials.info is not None
        assert materials.info.is_vertical is True


# ----------------------------------------------------------------------
# Subtitle planning
# ----------------------------------------------------------------------


class TestSubtitlePlan:
    def test_human_tracks_are_both_requested(self, extractor, fake_ydl) -> None:
        info = info_dict(subtitles={"en": [{}], "zh-Hans": [{}]})
        plan = extractor.plan_subtitles(info)
        assert plan == {SUBTITLE_NAME: ("en", False), SUBTITLE_ZH_NAME: ("zh-Hans", False)}

    def test_auto_captions_are_used_when_humans_are_absent(self, extractor) -> None:
        info = info_dict(automatic_captions={"en-orig": [{}]})
        plan = extractor.plan_subtitles(info)
        assert plan == {SUBTITLE_NAME: ("en-orig", True)}

    def test_a_dubbed_video_uses_the_declared_language(self, extractor) -> None:
        """The regression: ``*-orig`` is one-per-language on a dubbed video.

        Choosing by document order picked Arabic for an English video, and
        because a Chinese track existed the plan then declared translation
        unnecessary -- Arabic and Chinese subtitles over an English video.
        """
        info = info_dict(
            language="en-US",
            automatic_captions={"ar-orig": [{}], "en-orig": [{}], "zh-Hans": [{}]},
        )
        plan = extractor.plan_subtitles(info)
        assert plan[SUBTITLE_NAME] == ("en-orig", True)
        assert plan[SUBTITLE_ZH_NAME] == ("zh-Hans", True)

    def test_a_video_declaring_no_language_still_avoids_document_order(self, extractor) -> None:
        info = info_dict(automatic_captions={"ar-orig": [{}], "en-orig": [{}]})
        assert extractor.plan_subtitles(info)[SUBTITLE_NAME] == ("en-orig", True)

    def test_human_track_wins_over_auto(self, extractor) -> None:
        info = info_dict(subtitles={"en": [{}]}, automatic_captions={"en-orig": [{}]})
        assert extractor.plan_subtitles(info)[SUBTITLE_NAME] == ("en", False)

    def test_the_metadata_and_the_plan_cannot_disagree(self, extractor) -> None:
        """Both call ``select_source_lang``; both must get the tiebreak.

        ``_build_metadata`` is the second call site of the same decision. When
        only ``plan_subtitles`` passed ``declared_lang``, the metadata and the
        plan disagreed -- the same defect fixed in one place and left standing
        in the other.

        The declared language is deliberately **non-English**: with ``en-US``
        the shared priority list would pick ``en-orig`` anyway and this test
        would pass even with the call site un-fixed (it did, until the reverse
        check caught it). ``ar`` is the case where the two signals disagree.
        """
        info = info_dict(
            language="ar",
            automatic_captions={"ar-orig": [{}], "en-orig": [{}], "zh-Hans": [{}]},
        )
        metadata = extractor._build_metadata(URL, info)
        assert metadata.official_subtitle_lang == "ar-orig"
        assert metadata.official_subtitle_lang == extractor.plan_subtitles(info)[SUBTITLE_NAME][0]

    def test_no_tracks_means_the_plan_is_empty(self, extractor) -> None:
        assert extractor.plan_subtitles(info_dict()) == {}

    def test_platform_without_remote_subtitles_plans_nothing(self) -> None:
        """X has no caption tracks; the ASR chain always runs."""
        assert YtDlpExtractor(X).plan_subtitles(info_dict(subtitles={"en": [{}]})) == {}

    def test_bilibili_plans_a_track_when_the_platform_offers_one(self) -> None:
        """The regression: bilibili was ``remote=False``, so the plan was always empty.

        Nothing fetched its CC track, and ``bilibili_json_to_srt`` -- the converter
        written for exactly that track -- was ported, exported and tested while no
        production code called it. Asserting on the plan rather than on the flag
        because the flag being right is not the point; a track being requested is.
        """
        plan = YtDlpExtractor(BILIBILI).plan_subtitles(
            info_dict(subtitles={"zh-Hans": [{}], "en": [{}]})
        )

        assert SUBTITLE_NAME in plan, "bilibili would never fetch its own CC track"
        assert SUBTITLE_ZH_NAME in plan, "the existing Chinese track would be discarded"

    def test_existing_chinese_is_reused(self, extractor) -> None:
        """A platform-provided Chinese track saves a whole translation pass."""
        info = info_dict(subtitles={"en": [{}], "zh-Hans": [{}]})
        plan = extractor.plan_subtitles(info)
        assert SUBTITLE_ZH_NAME in plan

    def test_chinese_is_skipped_when_it_is_the_source(self, extractor) -> None:
        info = info_dict(subtitles={"zh-Hans": [{}]})
        plan = extractor.plan_subtitles(info)
        assert SUBTITLE_NAME in plan
        assert SUBTITLE_ZH_NAME not in plan


class TestSubtitleDownload:
    def test_subtitle_file_is_written_to_raw(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        fake_ydl.info = info_dict(subtitles={"en": [{}]})
        materials = extractor.fetch(URL, ctx, runner=runner)

        assert materials.subtitle_src is not None
        assert materials.subtitle_src.name == SUBTITLE_NAME
        assert materials.subtitle_src.read_text(encoding="utf-8") == FAKE_SRT

    def test_each_language_gets_its_own_directory(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        """v0.1 globbed one shared directory and occasionally mismatched tracks."""
        fake_ydl.info = info_dict(subtitles={"en": [{}], "zh-Hans": [{}]})
        extractor.fetch(URL, ctx, runner=runner)

        subtitle_policies = [p for p in fake_ydl.download_policies if p.skip_download]
        templates = [p.outtmpl for p in subtitle_policies]
        assert len(templates) == 2
        assert len(set(templates)) == 2, "two languages shared an output template"

    def test_no_subtitle_file_leaves_the_field_none(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        fake_ydl.info = info_dict(subtitles={"en": [{}]})
        fake_ydl.subtitle_payload = None

        materials = extractor.fetch(URL, ctx, runner=runner)
        assert materials.subtitle_src is None

    def test_a_failing_subtitle_fetch_does_not_fail_the_job(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        """Losing a caption track means ASR runs, not that the job dies."""
        fake_ydl.info = info_dict(subtitles={"en": [{}]})
        fake_ydl.raise_on_subtitle_download = RuntimeError("403 from the caption CDN")

        materials = extractor.fetch(URL, ctx, runner=runner)
        assert materials.video.is_file()
        assert materials.subtitle_src is None

    def test_vtt_is_converted_to_srt(self, extractor, ctx, runner, fake_ydl) -> None:
        fake_ydl.info = info_dict(subtitles={"en": [{}]})
        fake_ydl.subtitle_extension = "vtt"
        fake_ydl.subtitle_payload = (
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<i>hello</i>\n"
        )

        materials = extractor.fetch(URL, ctx, runner=runner)

        assert materials.subtitle_src is not None
        text = materials.subtitle_src.read_text(encoding="utf-8")
        assert "-->" in text and "," in text, "timestamp was not converted to SRT form"
        assert "<i>" not in text, "VTT markup survived conversion"

    def test_an_srt_track_is_copied_unchanged(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        fake_ydl.info = info_dict(subtitles={"en": [{}]})
        materials = extractor.fetch(URL, ctx, runner=runner)
        assert materials.subtitle_src is not None
        assert materials.subtitle_src.read_text(encoding="utf-8") == FAKE_SRT

    def test_the_requested_format_includes_json(self, extractor, ctx, runner, fake_ydl) -> None:
        """Bilibili's CC track exists only as JSON.

        Asking for ``srt/vtt`` alone requests containers the platform does not
        have, so the track is skipped. This is the second half of the bug: even
        with the fetch enabled, the converter stayed unreachable.
        """
        fake_ydl.info = info_dict(subtitles={"en": [{}]})
        extractor.fetch(URL, ctx, runner=runner)

        formats = [p.subtitle_format for p in fake_ydl.download_policies if p.skip_download]
        assert formats, "no subtitle fetch happened"
        assert all("json" in fmt for fmt in formats), formats

    def test_a_bilibili_json_track_is_converted_to_srt(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        """Bilibili hands back cue times in seconds, not timestamps.

        Copying the payload verbatim would produce a file that parses as zero
        cues: ASR would be skipped because a track "exists", and the job would
        emit subtitles containing no text.
        """
        fake_ydl.info = info_dict(subtitles={"en": [{}]})
        fake_ydl.subtitle_extension = "json"
        fake_ydl.subtitle_payload = '{"body": [{"from": 1.5, "to": 4.0, "content": "你好"}]}'

        materials = extractor.fetch(URL, ctx, runner=runner)

        assert materials.subtitle_src is not None
        text = materials.subtitle_src.read_text(encoding="utf-8")
        assert "00:00:01,500 --> 00:00:04,000" in text, "seconds were not converted"
        assert "你好" in text

    def test_a_json_track_with_no_cues_is_not_written(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        """An empty CC body must fall through to ASR, not leave an empty file.

        A zero-byte ``subtitle.srt`` is worse than none: it makes PREPARE look
        successful, and the ASR chain's "the platform track exists" branch then
        produces a transcript with no cues in it.
        """
        fake_ydl.info = info_dict(subtitles={"en": [{}]})
        fake_ydl.subtitle_extension = "json"
        fake_ydl.subtitle_payload = '{"body": []}'

        materials = extractor.fetch(URL, ctx, runner=runner)

        assert materials.subtitle_src is None


# ----------------------------------------------------------------------
# Resumption and cancellation
# ----------------------------------------------------------------------


class TestResumption:
    def test_second_run_does_not_download(self, extractor, ctx, runner, fake_ydl) -> None:
        extractor.fetch(URL, ctx, runner=runner)
        downloads_before = len(fake_ydl.download_calls)

        extractor.fetch(URL, ctx, runner=runner)

        assert len(fake_ydl.download_calls) == downloads_before, (
            "a complete master should have short-circuited the download"
        )

    def test_force_redownloads(self, extractor, ctx, runner, fake_ydl) -> None:
        extractor.fetch(URL, ctx, runner=runner)
        downloads_before = len(fake_ydl.download_calls)

        ctx.options.force = True
        extractor.fetch(URL, ctx, runner=runner)

        assert len(fake_ydl.download_calls) > downloads_before

    def test_reused_master_measures_dimensions_from_existing_video(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        """A reused master must correct initial yt-dlp metadata from real pixels."""
        extractor.fetch(URL, ctx, runner=runner)

        fake_ydl.info = {**fake_ydl.info, "width": 640, "height": 360}
        materials = extractor.fetch(URL, ctx, runner=runner)
        assert materials.info.width == 1920
        assert materials.info.height == 1080

    def test_truncated_master_is_not_reused(
        self, extractor, ctx, runner, fake_ydl, monkeypatch
    ) -> None:
        """v0.1 accepted the size alone, so a half-written master was reused."""
        extractor.fetch(URL, ctx, runner=runner)
        downloads_before = len(fake_ydl.download_calls)

        # Same size, no longer a decodable video.
        (raw_dir(ctx) / VIDEO_NAME).write_bytes(b"\x00" * 8192)
        # The reuse check lives in the shared PREPARE module now, because the
        # local-file producer needs the same validation.
        monkeypatch.setattr("porter.media.prepare.is_valid_video", lambda *a, **k: False)

        extractor.fetch(URL, ctx, runner=runner)

        assert len(fake_ydl.download_calls) > downloads_before


class TestCancellation:
    def test_cancel_before_fetch_raises(self, extractor, ctx, runner, fake_ydl) -> None:
        ctx.request_cancel()
        with pytest.raises(JobCancelled):
            extractor.fetch(URL, ctx, runner=runner)

    def test_cancel_midway_stops_the_download(self, extractor, ctx, runner, fake_ydl) -> None:
        original = FakeYDL.download

        def cancel_then_download(self, urls):
            raise JobCancelled("cancelled between steps")

        FakeYDL.download = cancel_then_download
        try:
            with pytest.raises(JobCancelled):
                extractor.fetch(URL, ctx, runner=runner)
        finally:
            FakeYDL.download = original

    def test_cancel_between_phases_is_not_swallowed(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        """A cancelled job must propagate, not be reported as a download error."""
        fake_ydl.raise_on_download = JobCancelled("stop")

        with pytest.raises(JobCancelled):
            extractor.fetch(URL, ctx, runner=runner)


# ----------------------------------------------------------------------
# Failure and degradation
# ----------------------------------------------------------------------


class TestFailureHandling:
    def test_no_metadata_raises(self, extractor, ctx, runner, fake_ydl) -> None:
        fake_ydl.info = None
        with pytest.raises(ExtractionError, match="no metadata"):
            extractor.fetch(URL, ctx, runner=runner)

    def test_a_ytdlp_error_is_reported_as_data_not_a_traceback(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        """A site-side yt-dlp failure must surface as ``ExtractionError``.

        yt-dlp reports a dead format selector, a bot check or a geo-block as
        ``yt_dlp.utils.YoutubeDLError``. Letting one escape hands the user a
        Python traceback for a condition the tool is supposed to explain -- and
        it did, on a real YouTube URL whose formats YouTube had stopped serving.
        """
        import yt_dlp

        fake_ydl.raise_on_extract = yt_dlp.utils.DownloadError(
            "ERROR: [youtube] 0tqty8ltKDA: Requested format is not available"
        )

        with pytest.raises(ExtractionError, match="Requested format is not available"):
            extractor.fetch(URL, ctx, runner=runner)

    def test_no_downloadable_media_raises(self, extractor, ctx, runner, fake_ydl) -> None:
        fake_ydl.media_payload = None
        with pytest.raises(ExtractionError, match="could not download"):
            extractor.fetch(URL, ctx, runner=runner)

    def test_carousel_takes_the_first_entry_with_a_video(self, extractor) -> None:
        info = {"entries": [{"id": "a"}, {"id": "b", "formats": [{"format_id": "1"}]}]}
        chosen = extractor._first_video_entry(URL, info["entries"])
        assert chosen["id"] == "b"

    def test_carousel_without_any_video_raises(self, extractor) -> None:
        with pytest.raises(ExtractionError, match="no downloadable video"):
            extractor._first_video_entry(URL, [{"id": "a"}, {"id": "b"}])

    def test_no_denoise_skips_enhancement(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        ctx.options.audio_denoise = False
        materials = extractor.fetch(URL, ctx, runner=runner)
        assert materials.audio_enhanced is None

    def test_cover_is_none_without_a_thumbnail(
        self, extractor, ctx, runner, fake_ydl
    ) -> None:
        materials = extractor.fetch(URL, ctx, runner=runner)
        assert materials.cover is None

    def test_cover_failure_is_not_fatal(
        self, extractor, ctx, runner, fake_ydl, monkeypatch
    ) -> None:
        fake_ydl.info = info_dict(thumbnail="https://example.com/t.jpg")

        import requests

        def explode(*args, **kwargs):
            raise OSError("network down")

        monkeypatch.setattr(requests, "get", explode)

        materials = extractor.fetch(URL, ctx, runner=runner)
        assert materials.cover is None
        assert materials.video.is_file()


class TestRateLimitRetry:
    def test_retrying_platform_attempts_twice(self, ctx, fake_ydl) -> None:
        fake_ydl.info = info_dict(id="123", title="tt", formats=[{"format_id": "x"}])
        fake_ydl.media_payload = None  # both attempts fail

        with pytest.raises(ExtractionError):
            YtDlpExtractor(TIKTOK).fetch(
                "https://www.tiktok.com/@u/video/123", ctx, runner=StubRunner()
            )

        media_attempts = [p for p in fake_ydl.download_policies if not p.skip_download]
        assert len(media_attempts) == 2, "TikTok should retry once on rate limit"

    def test_non_retrying_platform_attempts_once(self, extractor, ctx, fake_ydl) -> None:
        fake_ydl.media_payload = None
        with pytest.raises(ExtractionError):
            extractor.fetch(URL, ctx, runner=StubRunner())

        media_attempts = [p for p in fake_ydl.download_policies if not p.skip_download]
        assert len(media_attempts) == 1, "YouTube failure is not retried; it is not transient"

    def test_retry_recovers_when_the_second_attempt_succeeds(
        self, ctx, fake_ydl
    ) -> None:
        fake_ydl.info = info_dict(id="123", title="tt", formats=[{"format_id": "x"}])

        original = FakeYDL.download
        calls = {"n": 0}

        def flaky(self, urls):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("rate limited")
            original(self, urls)

        FakeYDL.download = flaky
        try:
            materials = YtDlpExtractor(TIKTOK).fetch(
                "https://www.tiktok.com/@u/video/123", ctx, runner=StubRunner()
            )
        finally:
            FakeYDL.download = original

        assert materials.video.is_file()


class TestNoStdoutFromThePipeline:
    def test_fetch_writes_nothing_to_stdout(
        self, extractor, ctx, runner, fake_ydl, capsys
    ) -> None:
        """stdout is the MCP protocol channel; a stray print corrupts it."""
        extractor.fetch(URL, ctx, runner=runner)
        captured = capsys.readouterr()
        assert captured.out == ""


class TestRegistryRoundTrip:
    def test_extractor_resolves_from_a_url(self) -> None:
        reg = PlatformRegistry()
        reg.register(YtDlpExtractor(YOUTUBE))
        reg.register(YtDlpExtractor(X))
        assert reg.find(URL).name == "youtube"
        assert reg.find("https://x.com/u/status/1").name == "x"


class TestFindingTheSubtitleFile:
    """The bug that made every subtitle fetch a silent no-op.

    ``outtmpl="sub.%(ext)s`` with ``subtitleslangs=("en",)`` makes yt-dlp write
    ``sub.en.srt`` -- the language tag is part of the name. The code searched for
    the literal ``sub.srt``, found nothing, and logged one warning, because a
    missing caption track is best-effort by design (ASR takes over). So every
    platform with a subtitle track silently fell through to recognition, and the
    unit tests agreed with the code because the fake wrote ``sub.srt`` too.

    Tested directly rather than only through ``fetch`` so the convention is
    pinned: a future change to the fake cannot quietly restore the old agreement.
    """

    def test_the_language_tag_is_part_of_the_name(self, tmp_path) -> None:
        from porter.platforms.base import _find_subtitle_file

        (tmp_path / "sub.en.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")

        assert _find_subtitle_file(tmp_path) == tmp_path / "sub.en.srt"

    def test_a_hyphenated_tag_is_handled(self, tmp_path) -> None:
        """``zh-Hans`` produces ``sub.zh-Hans.srt``; a naive split on ``.`` breaks."""
        from porter.platforms.base import _find_subtitle_file

        (tmp_path / "sub.zh-Hans.srt").write_text("x")

        assert _find_subtitle_file(tmp_path) == tmp_path / "sub.zh-Hans.srt"

    def test_srt_is_preferred_over_vtt_and_json(self, tmp_path) -> None:
        from porter.platforms.base import _find_subtitle_file

        (tmp_path / "sub.en.json").write_text("{}")
        (tmp_path / "sub.en.vtt").write_text("WEBVTT\n")
        (tmp_path / "sub.en.srt").write_text("x")

        assert _find_subtitle_file(tmp_path).suffix == ".srt"

    def test_vtt_is_preferred_over_json(self, tmp_path) -> None:
        from porter.platforms.base import _find_subtitle_file

        (tmp_path / "sub.en.json").write_text("{}")
        (tmp_path / "sub.en.vtt").write_text("WEBVTT\n")

        assert _find_subtitle_file(tmp_path).suffix == ".vtt"

    def test_an_empty_file_is_not_a_track(self, tmp_path) -> None:
        """A zero-byte caption is worse than none: ASR would be skipped."""
        from porter.platforms.base import _find_subtitle_file

        (tmp_path / "sub.en.srt").write_text("")

        assert _find_subtitle_file(tmp_path) is None

    def test_an_empty_directory_yields_nothing(self, tmp_path) -> None:
        from porter.platforms.base import _find_subtitle_file

        assert _find_subtitle_file(tmp_path) is None

    def test_media_files_are_not_mistaken_for_subtitles(self, tmp_path) -> None:
        from porter.platforms.base import _find_subtitle_file

        (tmp_path / "sub.en.mp4").write_bytes(b"\x00" * 64)

        assert _find_subtitle_file(tmp_path) is None
