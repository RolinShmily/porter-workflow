"""The resolved execution plan.

The plan's whole value is that an agent acts on it, so the tests are mostly about
it being *derived* rather than *authored*: the phase list must be the one the run
iterates, the backend order must be the chain's order, and the subtitle route must
come from the platform's own selection rules. A plan that describes a pipeline
nobody runs is worse than no plan.
"""

from __future__ import annotations

import pytest

from porter.models.plan import BackendPlan, Plan, SubtitlePlan, TranslationPlan
from porter.models.request import BurnMode, JobOptions
from porter.plan import plan_for

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture
def ctx(tmp_path):
    from porter.context import RunContext

    return RunContext(job_id="plan", options=JobOptions(output_dir=tmp_path / "out"))


@pytest.fixture
def local_media(tmp_path):
    """A local media file that actually exists.

    ``plan_for`` now reports a missing path as infeasible, so a test that wants
    to exercise the ASR decision must give it a real file -- otherwise the file
    check decides first and the test silently stops covering what it claims to.
    """
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not a real video; the plan never decodes it")
    return path


@pytest.fixture
def inspected(monkeypatch):
    """Serve a canned inspection result instead of probing the network."""
    from porter.models.inspection import InspectionResult
    from porter.platforms import inspector as inspector_module

    def install(**overrides):
        fields = {
            "input_url": URL,
            "canonical_url": URL,
            "platform": "youtube",
            "is_valid": True,
            "has_video": True,
            "video_id": "dQw4w9WgXcQ",
            "duration_seconds": 213.0,
            "width": 1920,
            "height": 1080,
            "raw_info": {},
        }
        fields.update(overrides)
        result = InspectionResult(**fields)
        monkeypatch.setattr(
            inspector_module, "inspect_url", lambda url, c=None, **k: result
        )
        return result

    return install


@pytest.fixture
def asr_available(monkeypatch):
    """Control the ASR chain's availability without touching real probes."""
    from porter.asr.chain import AsrChain

    def install(entries):
        monkeypatch.setattr(AsrChain, "availability", lambda self, ctx: list(entries))

    return install


class TestModel:
    def test_a_plan_round_trips_to_json(self) -> None:
        plan = Plan(
            source="x",
            kind="local",
            acquisition="local",
            subtitles=SubtitlePlan(route="asr"),
            translation=TranslationPlan(target_lang="zh-Hans"),
            burn_mode="dual",
            burn_runs=True,
            renderer="ffmpeg",
        )
        payload = plan.to_dict()

        assert payload["kind"] == "local"
        assert payload["subtitles"]["route"] == "asr"
        assert payload["feasible"] is True
        assert payload["blocking_issues"] == []

    def test_verified_defaults_to_false(self) -> None:
        """Unstated provenance must never read as verified."""
        assert BackendPlan(name="x", available=True).verified is False


class TestLocalFiles:
    def test_a_local_file_always_needs_asr(self, ctx, asr_available, local_media) -> None:
        asr_available([("whisper-api", True)])
        plan = plan_for(str(local_media), ctx=ctx)

        assert plan.kind == "local"
        assert plan.platform == "local"
        assert plan.acquisition == "local"
        assert plan.subtitles.route == "asr"
        assert plan.subtitles.asr_runs is True
        assert plan.subtitles.tracks == {}

    def test_it_says_that_sidecar_subtitles_are_ignored(self, ctx, asr_available) -> None:
        """Worth stating: a local file with a perfect .srt beside it still pays."""
        asr_available([("whisper-api", True)])
        plan = plan_for(str(local_media), ctx=ctx)

        assert any("sidecar" in note for note in plan.notes)

    def test_no_available_engine_makes_it_infeasible(
        self, ctx, asr_available, local_media
    ) -> None:
        asr_available([("whisper-api", False), ("bcut", False)])
        plan = plan_for(str(local_media), ctx=ctx)

        assert plan.feasible is False
        assert "TRANSCRIBE will fail" in plan.blocking_issues[0]
        assert "whisper-api, bcut" in plan.blocking_issues[0]

    def test_a_missing_local_file_is_infeasible(self, ctx, asr_available, tmp_path) -> None:
        """The file check must win over the ASR check when both would fire.

        Ordering matters for the message: "no such file" is actionable, while a
        lecture about ASR backends for a path that does not exist is noise.
        """
        asr_available([("whisper-api", True)])
        plan = plan_for(str(tmp_path / "absent.mp4"), ctx=ctx)

        assert plan.feasible is False
        assert "no such file" in plan.blocking_issues[0]

    def test_a_real_file_with_an_engine_is_feasible(self, ctx, asr_available, local_media) -> None:
        """The other direction: the file check must not reject real files."""
        asr_available([("whisper-api", True)])
        plan = plan_for(str(local_media), ctx=ctx)

        assert plan.feasible is True
        assert plan.blocking_issues == []


class TestSubtitleRoute:
    def test_a_platform_track_is_chosen_when_one_exists(self, ctx, asr_available, inspected) -> None:
        asr_available([("whisper-api", True)])
        inspected(raw_info={"subtitles": {"en": [{}]}})

        plan = plan_for(URL, ctx=ctx)

        assert plan.subtitles.route == "platform"
        assert plan.subtitles.tracks == {"subtitle.srt": "en"}
        assert plan.subtitles.asr_runs is False

    def test_a_platform_chinese_track_removes_translation(
        self, ctx, asr_available, inspected
    ) -> None:
        """The most valuable thing the plan predicts: this job is nearly free.

        Both an English and a Chinese track exist, so nothing is recognised and
        nothing is translated. An agent that predicted from ``has_subtitles`` alone
        would have no idea.
        """
        asr_available([("whisper-api", True)])
        inspected(raw_info={"subtitles": {"en": [{}], "zh-Hans": [{}]}})

        plan = plan_for(URL, ctx=ctx)

        assert plan.subtitles.tracks == {"subtitle.srt": "en", "subtitle_zh.srt": "zh-Hans"}
        assert plan.translation.needed is False
        assert any("translation is skipped" in note for note in plan.notes)

    def test_the_route_is_described_as_requested_not_guaranteed(
        self, ctx, asr_available, inspected
    ) -> None:
        """A caption fetch is a network call and platforms rate-limit it.

        Found the hard way: the plan promised ``asr_runs=False`` for a video whose
        Chinese track then failed to download with HTTP 429, so TRANSLATE ran and
        died. The plan cannot know a fetch will succeed, and must not imply it can.
        """
        asr_available([("whisper-api", True)])
        inspected(raw_info={"subtitles": {"en": [{}]}})

        plan = plan_for(URL, ctx=ctx)

        notes = " ".join(plan.notes)
        assert "Requested, not guaranteed" in notes
        assert "429" in notes

    def test_no_track_falls_back_to_asr(self, ctx, asr_available, inspected) -> None:
        asr_available([("whisper-api", True)])
        inspected(raw_info={})

        plan = plan_for(URL, ctx=ctx)

        assert plan.subtitles.route == "asr"
        assert plan.subtitles.asr_runs is True
        assert plan.translation.needed is True

    def test_the_route_comes_from_the_platform_not_from_has_subtitles(
        self, ctx, asr_available, inspected
    ) -> None:
        """The trap the plan exists to avoid.

        ``has_subtitles`` is a platform *listing*. Instagram advertises nothing and
        fetches nothing; a plan built on the flag would call the route "platform"
        and be wrong.
        """
        from porter.platforms.instagram import SPEC as INSTAGRAM

        asr_available([("whisper-api", True)])
        # A listing that says captions exist, on a platform that never fetches any.
        inspected(
            platform=INSTAGRAM.name,
            canonical_url="https://www.instagram.com/reel/abc/",
            has_subtitles=True,
            raw_info={"subtitles": {"en": [{}]}},
        )

        plan = plan_for("https://www.instagram.com/reel/abc/", ctx=ctx)

        assert plan.subtitles.route == "asr", "a listing is not a fetchable track"
        assert plan.subtitles.tracks == {}

    def test_an_unusable_link_is_infeasible(self, ctx, asr_available, inspected) -> None:
        asr_available([("whisper-api", True)])
        inspected(
            is_valid=False,
            has_video=False,
            error_message="Resource not found, deleted, or private (404).",
        )

        plan = plan_for(URL, ctx=ctx)

        assert plan.feasible is False
        assert plan.subtitles.route == "unknown"
        assert "404" in plan.blocking_issues[0]


class TestHonestyAboutUnverifiedEndpoints:
    """``available`` alone would promise jobs that were going to fail."""

    def test_an_unverified_only_chain_is_flagged(self, ctx, asr_available, inspected) -> None:
        asr_available([("whisper-api", False), ("bcut", True), ("google-web", True)])
        inspected(raw_info={})

        plan = plan_for(URL, ctx=ctx)

        warning = " ".join(plan.notes)
        assert "unverified endpoints" in warning
        assert "bcut" in warning and "google-web" in warning

    def test_a_verified_engine_silences_the_warning(self, ctx, asr_available, inspected) -> None:
        asr_available([("whisper-api", True), ("bcut", True)])
        inspected(raw_info={})

        plan = plan_for(URL, ctx=ctx)

        assert not any("unverified endpoints" in note for note in plan.notes)

    def test_the_platform_route_needs_no_warning(self, ctx, asr_available, inspected) -> None:
        """Nothing is recognised, so unverified recognisers are irrelevant."""
        asr_available([("bcut", True)])
        inspected(raw_info={"subtitles": {"en": [{}]}})

        plan = plan_for(URL, ctx=ctx)

        assert not any("unverified endpoints" in note for note in plan.notes)

    def test_the_real_chains_provenance_is_reported(self, ctx, inspected) -> None:
        """Read from the backends, not from a table in the plan module.

        A table would be a second source of truth and would drift the first time a
        backend was verified.
        """
        inspected(raw_info={})

        plan = plan_for(URL, ctx=ctx)

        by_name = {backend.name: backend.verified for backend in plan.subtitles.asr_backends}
        assert by_name["whisper-local"] is True
        assert by_name["whisper-api"] is True, "a documented API should be verified"
        assert by_name["videocaptioner"] is True


class TestDerivedNotAuthored:
    """The plan must describe the pipeline that would actually run."""

    def test_the_backend_order_is_the_chain_order(self, ctx, inspected) -> None:
        inspected(raw_info={})

        plan = plan_for(URL, ctx=ctx)

        from porter.pipeline import Pipeline

        chain = Pipeline.default(ctx).transcriber
        assert [b.name for b in plan.subtitles.asr_backends] == [b.name for b in chain.backends]

    def test_the_phase_list_honours_burn(self, ctx, asr_available, inspected) -> None:
        asr_available([("whisper-api", True)])
        inspected(raw_info={})

        plan = plan_for(
            URL,
            options=JobOptions(output_dir=ctx.options.output_dir, burn=BurnMode.SKIP),
            ctx=ctx,
        )

        assert "burn" not in plan.phases
        assert plan.burn_runs is False

    def test_the_phase_list_honours_only_phase(self, ctx, asr_available, inspected) -> None:
        from porter.events import Phase

        asr_available([("whisper-api", True)])
        inspected(raw_info={})

        plan = plan_for(
            URL,
            options=JobOptions(
                output_dir=ctx.options.output_dir,
                only_phase=Phase.PREPARE,
            ),
            ctx=ctx,
        )

        assert plan.phases == ["prepare"]

    def test_measured_facts_are_carried_through(self, ctx, asr_available, inspected) -> None:
        asr_available([("whisper-api", True)])
        inspected(raw_info={"subtitles": {"en": [{}]}})

        plan = plan_for(URL, ctx=ctx)

        assert plan.duration_seconds == 213.0
        assert (plan.width, plan.height) == (1920, 1080)
        assert plan.is_vertical is False

    def test_orientation_is_unknown_when_unmeasured(self, ctx, asr_available, inspected) -> None:
        """Tri-state: an unmeasured video must not be reported as horizontal."""
        asr_available([("whisper-api", True)])
        inspected(width=None, height=None, raw_info={})

        plan = plan_for(URL, ctx=ctx)

        assert plan.is_vertical is None
