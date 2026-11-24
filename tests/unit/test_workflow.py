"""The workflow contract: one workflow, one capability per stage, no silent fallback.

These tests are written against the decisions the refactor exists to make:

* the *workflow* is chosen from what the source is, not from the platform name;
* the resolver selects exactly one capability per stage, never a candidate list;
* a paid capability is never chosen implicitly;
* an executable plan carries everything the run needs, and a changed
  configuration refuses to execute an old plan.

Everything here stubs the two probes the resolver owns (``probe_source`` and
``capability_catalog``) rather than patching a chain. Selection is a pure
decision over facts, so the facts are supplied directly. No test touches the
network or needs ffmpeg.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from porter.core.asr.chain import PROVENANCE_NAME, AsrChain
from porter.core.config import PorterConfig
from porter.core.context import RunContext
from porter.core.errors import PorterError
from porter.core.events import JobState
from porter.core.models.materials import RawMaterials, TaskLayout
from porter.core.models.request import BurnMode, JobOptions
from porter.core.workflow.capabilities import CAPABILITY_IDS
from porter.core.workflow.executor import _build_transcriber, _job_options, execute_plan
from porter.core.workflow.model import (
    CapabilityDescriptor,
    Cost,
    PlanIssue,
    PlanRuntime,
    SourceKind,
    SourceProbe,
    Stage,
    StagePlan,
    Task,
    TrackInfo,
    WorkflowId,
    WorkflowPlan,
)
from porter.core.workflow.probe import probe_source
from porter.core.workflow.resolver import PlanOverrides, config_fingerprint, resolve_plan

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
LOCAL = "/media/talk.mp4"

_PAID = frozenset(
    {"transcript.whisper-api", "translate.llm"}
)


# ----------------------------------------------------------------------
# Fixtures and builders
# ----------------------------------------------------------------------


def _ctx(tmp_path: Path, config: PorterConfig | None = None) -> RunContext:
    return RunContext(
        job_id="test-job",
        options=JobOptions(output_dir=tmp_path / "out"),
        config=config or PorterConfig(),
    )


def _catalog(*missing: str) -> tuple[CapabilityDescriptor, ...]:
    """A deterministic catalog: everything is available except ``missing``."""
    return tuple(
        CapabilityDescriptor(
            id=capability,
            stage=Stage(capability.split(".", 1)[0]),
            available=capability not in missing,
            cost=Cost.PAID if capability in _PAID else Cost.FREE,
        )
        for capability in CAPABILITY_IDS
    )


def _source_probe(
    *,
    kind: SourceKind = SourceKind.URL,
    source: str = URL,
    tracks: tuple[TrackInfo, ...] = (),
    has_video: bool = True,
    has_audio: bool = True,
) -> SourceProbe:
    return SourceProbe(
        kind=kind,
        source=source,
        platform="youtube" if kind is SourceKind.URL else "local",
        has_video=has_video,
        has_audio=has_audio,
        tracks=tracks,
    )


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    probe: SourceProbe | None = None,
    catalog: tuple[CapabilityDescriptor, ...] | None = None,
) -> None:
    """Replace the resolver's two external facts."""
    if probe is not None:
        monkeypatch.setattr(
            "porter.core.workflow.resolver.probe_source",
            lambda source, ctx: probe,
        )
    monkeypatch.setattr(
        "porter.core.workflow.resolver.capability_catalog",
        lambda ctx: catalog if catalog is not None else _catalog(),
    )


def _resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe: SourceProbe | None = None,
    catalog: tuple[CapabilityDescriptor, ...] | None = None,
    source: str = URL,
    config: PorterConfig | None = None,
    **overrides: object,
) -> WorkflowPlan:
    _stub(monkeypatch, probe or _source_probe(source=source), catalog)
    options = PlanOverrides(output_dir=tmp_path / "out", **overrides)  # type: ignore[arg-type]
    return resolve_plan(source, options, _ctx(tmp_path, config))


def _codes(plan: WorkflowPlan) -> set[str]:
    return {issue.code for issue in plan.issues}


def _stage_names(plan: WorkflowPlan) -> list[Stage]:
    return [planned.stage for planned in plan.stages]


def _plan(**overrides: object) -> WorkflowPlan:
    """A sealed plan for tests that do not need a resolver."""
    data: dict[str, object] = {
        "workflow": WorkflowId.URL_ASR,
        "task": Task.RELEASE,
        "source": URL,
        "runtime": PlanRuntime(output_dir="porter-test-out"),
        "source_probe": _source_probe(),
        "stages": (
            StagePlan(id="probe", stage=Stage.PROBE, capability="probe.ytdlp"),
        ),
    }
    data.update(overrides)
    return WorkflowPlan.model_validate(data).sealed()  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# The plan is the contract
# ----------------------------------------------------------------------


class TestPlanIdentity:
    def test_identical_content_seals_to_the_same_id(self) -> None:
        first = _plan(created_at="2026-01-01T00:00:00+00:00")
        second = _plan(created_at="2026-06-01T12:00:00+00:00")

        assert first.plan_id == second.plan_id
        assert first.plan_id.startswith("sha256:")

    def test_a_different_capability_changes_the_id(self) -> None:
        platform = _plan(
            stages=(
                StagePlan(
                    id="transcript",
                    stage=Stage.TRANSCRIPT,
                    capability="transcript.platform-track",
                ),
            )
        )
        supplied = _plan(
            stages=(
                StagePlan(
                    id="transcript",
                    stage=Stage.TRANSCRIPT,
                    capability="transcript.supplied",
                ),
            )
        )

        assert platform.plan_id != supplied.plan_id

    def test_a_feasible_plan_round_trips_through_json(self) -> None:
        plan = _plan()

        restored = WorkflowPlan.model_validate_json(plan.model_dump_json())

        assert restored == plan
        assert restored.plan_id == plan.plan_id


# ----------------------------------------------------------------------
# Workflow choice
# ----------------------------------------------------------------------


class TestWorkflowChoice:
    def test_a_url_without_a_track_plans_asr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(tmp_path, monkeypatch, probe=_source_probe())

        assert plan.workflow is WorkflowId.URL_ASR
        assert plan.task is Task.RELEASE
        assert plan.feasible is True
        assert _stage_names(plan) == [
            Stage.PROBE,
            Stage.ACQUIRE,
            Stage.NORMALIZE,
            Stage.TRANSCRIPT,
            Stage.TRANSLATE,
            Stage.WRITE,
            Stage.RENDER,
        ]
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.whisper-local"  # type: ignore[union-attr]

    def test_a_url_with_a_source_track_plans_the_subtitled_workflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = _source_probe(
            tracks=(TrackInfo(lang="en", role="source"),),
        )

        plan = _resolve(tmp_path, monkeypatch, probe=probe)

        assert plan.workflow is WorkflowId.URL_SUBTITLED
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.platform-track"  # type: ignore[union-attr]
        # A subtitled workflow still needs the master audio: the platform track
        # may be listed but fail to fetch, and the fallback is a *new plan*.
        assert plan.stage(Stage.ACQUIRE).capability == "acquire.ytdlp-video"  # type: ignore[union-attr]

    def test_a_supplied_file_turns_a_url_into_a_subtitled_workflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        supplied = tmp_path / "captions.srt"
        supplied.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
        )

        plan = _resolve(
            tmp_path,
            monkeypatch,
            probe=_source_probe(),
            subtitle_file=supplied,
        )

        assert plan.workflow is WorkflowId.URL_SUBTITLED
        transcript = plan.stage(Stage.TRANSCRIPT)
        assert transcript is not None
        assert transcript.capability == "transcript.supplied"
        assert transcript.params["path"] == str(supplied)
        assert plan.feasible is True

    def test_a_named_but_missing_subtitle_file_is_a_blocker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The path was explicitly named, so its absence is decidable now."""
        plan = _resolve(
            tmp_path,
            monkeypatch,
            probe=_source_probe(),
            subtitle_file=tmp_path / "gone.srt",
        )

        assert plan.feasible is False
        assert "SOURCE_NOT_FOUND" in _codes(plan)

    def test_a_local_video_without_subtitles_plans_local_asr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path,
            monkeypatch,
            source=LOCAL,
            probe=_source_probe(kind=SourceKind.LOCAL_VIDEO, source=LOCAL),
        )

        assert plan.workflow is WorkflowId.LOCAL_ASR
        assert plan.stage(Stage.ACQUIRE).capability == "acquire.local"  # type: ignore[union-attr]

    def test_a_local_video_with_a_supplied_file_is_subtitled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        supplied = tmp_path / "captions.srt"
        supplied.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
        )

        plan = _resolve(
            tmp_path,
            monkeypatch,
            source=LOCAL,
            probe=_source_probe(kind=SourceKind.LOCAL_VIDEO, source=LOCAL),
            subtitle_file=supplied,
        )

        assert plan.workflow is WorkflowId.LOCAL_SUBTITLED
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.supplied"  # type: ignore[union-attr]

    def test_a_subtitle_file_runs_without_any_media(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "captions.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
        )
        _stub(monkeypatch)

        plan = resolve_plan(
            str(source), PlanOverrides(output_dir=tmp_path / "out"), _ctx(tmp_path)
        )

        assert plan.workflow is WorkflowId.SUBTITLE_ONLY
        # A subtitle cannot be *released*; the resolver corrects the task rather
        # than planning a burn that has no video.
        assert plan.task is Task.SUBTITLE
        assert plan.feasible is True
        assert _stage_names(plan) == [Stage.PROBE, Stage.TRANSCRIPT, Stage.TRANSLATE, Stage.WRITE]
        assert plan.stage(Stage.PROBE).capability == "probe.subtitle"  # type: ignore[union-attr]
        assert plan.stage(Stage.TRANSCRIPT).params["path"] == str(source)  # type: ignore[union-attr]

    def test_an_asr_request_on_a_subtitle_file_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The input has no audio, so an explicit ASR request cannot be honoured.

        It must be reported rather than silently ignored: the plan still says
        what would run, and the caller learns why their request did not apply.
        """
        source = tmp_path / "captions.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
        )
        _stub(monkeypatch)

        plan = resolve_plan(
            str(source),
            PlanOverrides(
                output_dir=tmp_path / "out", transcript="whisper-local"
            ),
            _ctx(tmp_path),
        )

        assert plan.workflow is WorkflowId.SUBTITLE_ONLY
        assert plan.feasible is False
        assert "CAPABILITY_WRONG_SOURCE" in _codes(plan)

    def test_audio_only_source_uses_the_audio_workflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path,
            monkeypatch,
            source="/media/talk.mp3",
            probe=_source_probe(
                kind=SourceKind.AUDIO, source="/media/talk.mp3", has_video=False
            ),
            task=Task.SUBTITLE,
        )

        assert plan.workflow is WorkflowId.AUDIO_ASR
        assert plan.feasible is True
        normalize = plan.stage(Stage.NORMALIZE)
        assert normalize is not None
        assert normalize.params["video"] is False
        assert plan.stage(Stage.RENDER) is None

    def test_releasing_an_audio_source_is_a_blocker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path,
            monkeypatch,
            source="/media/talk.mp3",
            probe=_source_probe(
                kind=SourceKind.AUDIO, source="/media/talk.mp3", has_video=False
            ),
        )

        assert plan.feasible is False
        assert "NO_VIDEO_FOR_RELEASE" in _codes(plan)


# ----------------------------------------------------------------------
# Task truncation
# ----------------------------------------------------------------------


class TestTaskTruncation:
    def test_probe_stops_at_the_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(tmp_path, monkeypatch, task=Task.PROBE)

        assert _stage_names(plan) == [Stage.PROBE]

    def test_audio_stops_after_normalize(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(tmp_path, monkeypatch, task=Task.AUDIO)

        assert _stage_names(plan) == [Stage.PROBE, Stage.ACQUIRE, Stage.NORMALIZE]

    def test_transcribe_stops_after_the_transcript(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(tmp_path, monkeypatch, task=Task.TRANSCRIBE)

        assert _stage_names(plan) == [
            Stage.PROBE,
            Stage.ACQUIRE,
            Stage.NORMALIZE,
            Stage.TRANSCRIPT,
        ]

    def test_subtitle_stops_before_the_render(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(tmp_path, monkeypatch, task=Task.SUBTITLE)

        assert _stage_names(plan) == [
            Stage.PROBE,
            Stage.ACQUIRE,
            Stage.NORMALIZE,
            Stage.TRANSCRIPT,
            Stage.TRANSLATE,
            Stage.WRITE,
        ]
        assert plan.stage(Stage.RENDER) is None

    def test_convert_is_reported_as_not_wired_yet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Better a blocker than a plan that silently does nothing."""
        plan = _resolve(
            tmp_path,
            monkeypatch,
            source=LOCAL,
            probe=_source_probe(kind=SourceKind.LOCAL_VIDEO, source=LOCAL),
            task=Task.CONVERT,
        )

        assert plan.feasible is False
        assert "TASK_NOT_SUPPORTED" in _codes(plan)

    def test_convert_on_a_subtitle_does_not_plan_a_translation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``convert`` means a format change, not "translate it instead".

        The subtitle-only branch used to run before the convert check, so asking
        a subtitle to be converted silently planned translation -- work the user
        did not ask for.
        """
        source = tmp_path / "captions.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
        )
        _stub(monkeypatch)

        plan = resolve_plan(
            str(source),
            PlanOverrides(output_dir=tmp_path / "out", task=Task.CONVERT),
            _ctx(tmp_path),
        )

        assert plan.feasible is False
        assert "TASK_NOT_SUPPORTED" in _codes(plan)
        assert Stage.TRANSLATE not in _stage_names(plan)


# ----------------------------------------------------------------------
# Translation is planned from observed tracks
# ----------------------------------------------------------------------


class TestTranslationChoice:
    def test_a_target_track_skips_translation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = _source_probe(
            tracks=(
                TrackInfo(lang="en", role="source"),
                TrackInfo(lang="zh-Hans", role="target"),
            )
        )

        plan = _resolve(tmp_path, monkeypatch, probe=probe)

        assert plan.stage(Stage.TRANSLATE) is None
        assert "TRANSLATION_SKIPPED" in _codes(plan)
        # It is a warning, not a blocker: the track is a listing, and the
        # executor reports the truth if it does not arrive.
        assert plan.feasible is True

    def test_force_translate_keeps_the_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = _source_probe(
            tracks=(
                TrackInfo(lang="en", role="source"),
                TrackInfo(lang="zh-Hans", role="target"),
            )
        )

        plan = _resolve(tmp_path, monkeypatch, probe=probe, force_translate=True)

        assert plan.stage(Stage.TRANSLATE) is not None
        assert "TRANSLATION_SKIPPED" not in _codes(plan)


# ----------------------------------------------------------------------
# Capability selection: peers, not a fallback chain
# ----------------------------------------------------------------------


class TestCapabilitySelection:
    def test_a_paid_capability_is_not_chosen_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key in the environment is configuration, not consent to spend."""
        plan = _resolve(
            tmp_path,
            monkeypatch,
            probe=_source_probe(),
            catalog=_catalog("transcript.whisper-local"),
        )

        assert plan.feasible is False
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.whisper-local"  # type: ignore[union-attr]
        assert "NO_CAPABILITY" in _codes(plan)

    def test_allow_paid_permits_the_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path,
            monkeypatch,
            probe=_source_probe(),
            catalog=_catalog("transcript.whisper-local"),
            allow_paid=True,
        )

        assert plan.feasible is True
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.whisper-api"  # type: ignore[union-attr]

    def test_naming_a_paid_capability_is_consent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path,
            monkeypatch,
            probe=_source_probe(),
            catalog=_catalog("transcript.whisper-local"),
            transcript="whisper-api",
        )

        assert plan.feasible is True
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.whisper-api"  # type: ignore[union-attr]

    def test_an_unavailable_request_blocks_with_alternatives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path,
            monkeypatch,
            probe=_source_probe(),
            catalog=_catalog("transcript.whisper-local"),
            transcript="whisper-local",
            allow_paid=True,
        )

        assert plan.feasible is False
        issue = next(i for i in plan.issues if i.code == "CAPABILITY_UNAVAILABLE")
        assert "transcript.whisper-api" in issue.alternatives
        # The failed request is still what the plan names: the fix is a new
        # plan, not a silent substitution in this one.
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.whisper-local"  # type: ignore[union-attr]

    def test_a_paid_alternative_is_not_offered_without_consent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path,
            monkeypatch,
            probe=_source_probe(),
            catalog=_catalog("transcript.whisper-local"),
            transcript="whisper-local",
        )

        issue = next(i for i in plan.issues if i.code == "CAPABILITY_UNAVAILABLE")
        assert "transcript.whisper-api" not in issue.alternatives

    def test_an_unknown_capability_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path, monkeypatch, probe=_source_probe(), transcript="nonsense"
        )

        assert plan.feasible is False
        assert "UNKNOWN_CAPABILITY" in _codes(plan)
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.nonsense"  # type: ignore[union-attr]

    def test_naming_asr_overrides_a_listed_track(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A listed platform track is a default, not a veto on ASR."""
        probe = _source_probe(tracks=(TrackInfo(lang="en", role="source"),))

        plan = _resolve(tmp_path, monkeypatch, probe=probe, transcript="whisper-local")

        assert plan.workflow is WorkflowId.URL_ASR
        assert plan.feasible is True
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.whisper-local"  # type: ignore[union-attr]

    def test_naming_the_platform_track_selects_the_subtitled_workflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = _source_probe(tracks=(TrackInfo(lang="en", role="source"),))

        plan = _resolve(tmp_path, monkeypatch, probe=probe, transcript="platform")

        assert plan.workflow is WorkflowId.URL_SUBTITLED
        assert plan.feasible is True
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.platform-track"  # type: ignore[union-attr]

    def test_a_platform_request_without_a_track_is_a_blocker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path,
            monkeypatch,
            source=LOCAL,
            probe=_source_probe(kind=SourceKind.LOCAL_VIDEO, source=LOCAL),
            transcript="platform",
        )

        assert plan.workflow is WorkflowId.LOCAL_SUBTITLED
        assert plan.feasible is False
        assert "PLATFORM_TRACK_MISSING" in _codes(plan)

    def test_a_supplied_request_without_a_file_is_a_blocker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path,
            monkeypatch,
            source=LOCAL,
            probe=_source_probe(kind=SourceKind.LOCAL_VIDEO, source=LOCAL),
            transcript="supplied",
        )

        assert plan.workflow is WorkflowId.LOCAL_SUBTITLED
        assert plan.feasible is False
        assert "SUPPLIED_SUBTITLE_MISSING" in _codes(plan)

    def test_no_free_translator_blocks_instead_of_spending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = _catalog(
            "translate.bing", "translate.google", "translate.mymemory"
        )

        plan = _resolve(tmp_path, monkeypatch, probe=_source_probe(), catalog=catalog)

        assert plan.feasible is False
        translate = plan.stage(Stage.TRANSLATE)
        assert translate is not None
        # The plan still names the first free peer, so a human reading it sees
        # what would run once the dependency is installed. It is not rewritten
        # to the paid backend.
        assert translate.capability == "translate.bing"
        assert "NO_CAPABILITY" in _codes(plan)

    def test_a_named_llm_translation_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = _catalog(
            "translate.bing", "translate.google", "translate.mymemory"
        )

        plan = _resolve(
            tmp_path,
            monkeypatch,
            probe=_source_probe(),
            catalog=catalog,
            translate="llm",
            llm_model="some-model",
        )

        assert plan.feasible is True
        translate = plan.stage(Stage.TRANSLATE)
        assert translate is not None
        assert translate.capability == "translate.llm"
        assert translate.params["llm_model"] == "some-model"

    def test_a_configured_engine_does_not_force_asr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``asr.engine`` says which engine runs *when* ASR runs, not whether."""
        probe = _source_probe(tracks=(TrackInfo(lang="en", role="source"),))
        config = PorterConfig.model_validate({"asr": {"engine": "whisper-local"}})

        plan = _resolve(tmp_path, monkeypatch, probe=probe, config=config)

        assert plan.workflow is WorkflowId.URL_SUBTITLED
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.platform-track"  # type: ignore[union-attr]

    def test_a_configured_paid_engine_is_not_consent_to_spend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = PorterConfig.model_validate({"asr": {"engine": "whisper-api"}})

        plan = _resolve(
            tmp_path,
            monkeypatch,
            catalog=_catalog("transcript.whisper-local"),
            config=config,
        )

        assert plan.feasible is False
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.whisper-local"  # type: ignore[union-attr]
        assert "NO_CAPABILITY" in _codes(plan)

    def test_a_configured_paid_engine_is_honoured_with_allow_paid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = PorterConfig.model_validate({"asr": {"engine": "whisper-api"}})

        plan = _resolve(
            tmp_path,
            monkeypatch,
            catalog=_catalog("transcript.whisper-local"),
            config=config,
            allow_paid=True,
        )

        assert plan.feasible is True
        assert plan.stage(Stage.TRANSCRIPT).capability == "transcript.whisper-api"  # type: ignore[union-attr]

    def test_a_missing_ffmpeg_blocks_before_execution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single-implementation stage must still be probed.

        Acquire, normalize and render have one implementation each, but that is
        not a reason to promise a plan on a machine that cannot run them.
        """
        plan = _resolve(
            tmp_path,
            monkeypatch,
            probe=_source_probe(),
            catalog=_catalog(
                "acquire.ytdlp-video", "normalize.ffmpeg", "render.ffmpeg-libass"
            ),
        )

        assert plan.feasible is False
        blocked = {issue.stage for issue in plan.blockers}
        assert {Stage.ACQUIRE, Stage.NORMALIZE, Stage.RENDER} <= blocked

    def test_there_is_exactly_one_capability_per_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(tmp_path, monkeypatch, probe=_source_probe())

        stages = [planned.stage for planned in plan.stages]
        assert len(stages) == len(set(stages))
        assert all(planned.capability for planned in plan.stages)


# ----------------------------------------------------------------------
# The fingerprint
# ----------------------------------------------------------------------


class TestConfigFingerprint:
    def test_secrets_do_not_change_the_fingerprint(self, tmp_path: Path) -> None:
        first = PorterConfig.model_validate({"llm": {"api_key": "sk-first"}})
        second = PorterConfig.model_validate({"llm": {"api_key": "sk-second"}})

        assert config_fingerprint(_ctx(tmp_path, first)) == config_fingerprint(
            _ctx(tmp_path, second)
        )

    def test_a_non_secret_setting_does_change_it(self, tmp_path: Path) -> None:
        first = PorterConfig()
        second = PorterConfig.model_validate({"ffmpeg": {"crf": 23}})

        assert config_fingerprint(_ctx(tmp_path, first)) != config_fingerprint(
            _ctx(tmp_path, second)
        )


# ----------------------------------------------------------------------
# Executor preflight
# ----------------------------------------------------------------------


class TestExecutorPreflight:
    def test_an_infeasible_plan_never_starts_work(self, tmp_path: Path) -> None:
        plan = _plan(
            feasible=False,
            issues=(
                PlanIssue(
                    code="SOURCE_UNUSABLE",
                    message="the link is dead",
                    fix="check the link",
                ),
            ),
        )

        outcome = execute_plan(plan, _ctx(tmp_path))

        assert outcome.workflow.state is JobState.FAILED
        assert outcome.workflow.error is not None
        assert outcome.workflow.error.code == "SOURCE_UNUSABLE"
        assert outcome.job.state is JobState.FAILED

    def test_a_plan_made_under_another_configuration_is_refused(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(config_fingerprint="sha256:stale")

        outcome = execute_plan(plan, _ctx(tmp_path))

        assert outcome.workflow.state is JobState.FAILED
        assert outcome.workflow.error is not None
        assert outcome.workflow.error.code == "PLAN_CONFIG_CHANGED"


# ----------------------------------------------------------------------
# Plan -> JobOptions
# ----------------------------------------------------------------------


class TestPlanToJobOptions:
    def _media_plan(self, tmp_path: Path) -> WorkflowPlan:
        cookies = tmp_path / "cookies.txt"
        captions = tmp_path / "captions.srt"
        return _plan(
            runtime=PlanRuntime(
                output_dir=str(tmp_path / "out"),
                target_lang="zh-Hant",
                force=True,
                audio_denoise=False,
            ),
            stages=(
                StagePlan(
                    id="acquire",
                    stage=Stage.ACQUIRE,
                    capability="acquire.ytdlp-video",
                    params={
                        "cookies_file": str(cookies),
                        "cookies_browser": "firefox",
                    },
                ),
                StagePlan(
                    id="transcript",
                    stage=Stage.TRANSCRIPT,
                    capability="transcript.supplied",
                    params={"path": str(captions)},
                ),
                StagePlan(
                    id="translate",
                    stage=Stage.TRANSLATE,
                    capability="translate.llm",
                    params={"target_lang": "zh-Hant", "llm_model": "a-model"},
                ),
                StagePlan(
                    id="render",
                    stage=Stage.RENDER,
                    capability="render.ffmpeg-libass",
                    params={"mode": "zh_only"},
                ),
            ),
        )

    def test_every_execution_setting_comes_from_the_plan(self, tmp_path: Path) -> None:
        options = _job_options(self._media_plan(tmp_path))

        assert options.burn is BurnMode.ZH_ONLY
        assert options.target_lang == "zh-Hant"
        assert options.llm_model == "a-model"
        assert options.cookies_file == tmp_path / "cookies.txt"
        assert options.cookies_browser == "firefox"
        assert options.subtitle_file == tmp_path / "captions.srt"
        assert options.force is True
        assert options.audio_denoise is False

    def test_the_selected_translator_is_part_of_the_cache_key(
        self, tmp_path: Path
    ) -> None:
        """A new plan with another backend must not reuse the old text."""
        options = _job_options(self._media_plan(tmp_path))

        assert options.translator == "translate.llm"

    def test_refinement_is_off_unless_the_plan_asks_for_it(
        self, tmp_path: Path
    ) -> None:
        """A hidden LLM call would be the implicit spending the resolver forbids."""
        assert _job_options(self._media_plan(tmp_path)).refine is False


# ----------------------------------------------------------------------
# Transcriber construction is single-capability
# ----------------------------------------------------------------------


class TestTranscriberAssembly:
    def _with_transcript(self, capability: str) -> WorkflowPlan:
        return _plan(
            stages=(
                StagePlan(
                    id="transcript",
                    stage=Stage.TRANSCRIPT,
                    capability=capability,
                ),
            )
        )

    def test_platform_only_never_runs_asr(self) -> None:
        chain = _build_transcriber(self._with_transcript("transcript.platform-track"))

        assert chain.allow_platform is True
        assert chain.allow_supplied is False
        assert chain.allow_asr is False
        assert chain.expected_used_asr is False
        assert chain.expected_origin == "platform"

    def test_supplied_only_never_runs_asr(self) -> None:
        chain = _build_transcriber(self._with_transcript("transcript.supplied"))

        assert chain.allow_supplied is True
        assert chain.allow_platform is False
        assert chain.allow_asr is False
        assert chain.expected_origin == "supplied"

    def test_whisper_local_is_the_only_backend(self) -> None:
        chain = _build_transcriber(self._with_transcript("transcript.whisper-local"))

        assert chain.allow_asr is True
        assert chain.allow_platform is False
        assert chain.allow_supplied is False
        assert chain.expected_used_asr is True
        assert chain.expected_origin_prefix == "faster-whisper:"
        assert [backend.name for backend in chain.backends] == ["whisper-local"]

    def test_whisper_api_is_the_only_backend(self) -> None:
        chain = _build_transcriber(self._with_transcript("transcript.whisper-api"))

        assert chain.allow_asr is True
        assert chain.expected_origin_prefix == "whisper:"
        assert [backend.name for backend in chain.backends] == ["whisper-api"]


# ----------------------------------------------------------------------
# Subtitle-only execution
# ----------------------------------------------------------------------


class _FakeTranslator:
    """Stands in for the network so the subtitle workflow is testable offline."""

    def translate(self, subtitles, target_lang, ctx):
        return subtitles


class TestSubtitleExecution:
    def test_a_subtitle_file_is_translated_without_media(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "captions.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
        )
        _stub(monkeypatch)
        ctx = _ctx(tmp_path)
        plan = resolve_plan(
            str(source), PlanOverrides(output_dir=tmp_path / "out"), ctx
        )

        monkeypatch.setattr(
            "porter.core.workflow.executor._build_translator",
            lambda plan: _FakeTranslator(),
        )
        outcome = execute_plan(plan, ctx)

        assert outcome.workflow.state is JobState.DONE
        assert outcome.workflow.workflow is WorkflowId.SUBTITLE_ONLY
        assert outcome.workflow.artifacts["source_transcript"].endswith("subtitle.srt")
        statuses = {result.stage: result.status for result in outcome.workflow.stages}
        assert statuses[Stage.TRANSCRIPT] == "done"
        assert statuses[Stage.TRANSLATE] == "done"
        assert statuses[Stage.WRITE] == "done"
        assert outcome.workflow.task_dir is not None
        assert (
            Path(outcome.workflow.task_dir) / "cooked" / "subtitle.srt"
        ).is_file()


# ----------------------------------------------------------------------
# AsrChain refuses cross-capability reuse
# ----------------------------------------------------------------------


def _raw(tmp_path: Path) -> RawMaterials:
    layout = TaskLayout.build(tmp_path / "out", "id", "title").ensure_dirs()
    audio = layout.raw_dir / "audio.wav"
    audio.write_bytes(b"not really audio")
    return RawMaterials(
        layout=layout,
        video=layout.raw_dir / "video.mp4",
        audio=audio,
    )


def _write_cached_cues(raw: RawMaterials, *, used_asr: bool, origin: str) -> None:
    cooked = raw.layout.cooked_dir
    (cooked / "subtitle.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
    )
    (cooked / PROVENANCE_NAME).write_text(
        json.dumps({"used_asr": used_asr, "origin": origin}), encoding="utf-8"
    )


class TestAsrChainStaysWithinItsCapability:
    def test_platform_only_fails_instead_of_running_asr(self, tmp_path: Path) -> None:
        raw = _raw(tmp_path)
        ctx = _ctx(tmp_path)
        chain = AsrChain(
            [],
            allow_platform=True,
            allow_supplied=False,
            allow_asr=False,
            expected_used_asr=False,
            expected_origin="platform",
        )

        with pytest.raises(PorterError, match="did not provide a usable subtitle"):
            chain.transcribe(raw, ctx)

    def test_a_supplied_file_is_refused_when_the_plan_did_not_choose_it(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "captions.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
        )
        raw = _raw(tmp_path)
        ctx = _ctx(tmp_path)
        ctx.options.subtitle_file = source
        chain = AsrChain(
            [],
            allow_platform=False,
            allow_supplied=False,
            allow_asr=False,
        )

        with pytest.raises(PorterError, match="does not allow a supplied subtitle"):
            chain.transcribe(raw, ctx)

    def test_cached_asr_cues_are_not_reused_by_a_platform_plan(
        self, tmp_path: Path
    ) -> None:
        """The cache must not substitute a capability the plan did not select."""
        raw = _raw(tmp_path)
        _write_cached_cues(raw, used_asr=True, origin="faster-whisper:local")
        ctx = _ctx(tmp_path)
        chain = AsrChain(
            [],
            allow_platform=True,
            allow_supplied=False,
            allow_asr=False,
            expected_used_asr=False,
            expected_origin="platform",
        )

        with pytest.raises(PorterError, match="did not provide a usable subtitle"):
            chain.transcribe(raw, ctx)

    def test_matching_cached_cues_are_reused_without_a_backend(
        self, tmp_path: Path
    ) -> None:
        raw = _raw(tmp_path)
        _write_cached_cues(raw, used_asr=True, origin="faster-whisper:local")
        ctx = _ctx(tmp_path)
        chain = AsrChain(
            [],
            allow_platform=False,
            allow_supplied=False,
            allow_asr=True,
            expected_used_asr=True,
            expected_origin_prefix="faster-whisper:",
        )

        subtitles = chain.transcribe(raw, ctx)

        assert subtitles.used_asr is True
        assert len(subtitles.items) == 1


# ----------------------------------------------------------------------
# Source classification
# ----------------------------------------------------------------------


class TestSourceClassification:
    def test_an_audio_suffix_is_audio_even_when_the_file_is_missing(
        self, tmp_path: Path
    ) -> None:
        probe = probe_source("/media/missing.mp3", _ctx(tmp_path))

        assert probe.kind is SourceKind.AUDIO
        assert {issue.code for issue in probe.warnings} == {"SOURCE_NOT_FOUND"}

    def test_a_subtitle_file_is_parsed_and_verified(self, tmp_path: Path) -> None:
        source = tmp_path / "captions.vtt"
        source.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n", encoding="utf-8"
        )

        probe = probe_source(source, _ctx(tmp_path))

        assert probe.kind is SourceKind.SUBTITLE
        track = probe.track("source")
        assert track is not None
        assert track.verified is True
        assert probe.warnings == ()

    def test_an_unreadable_subtitle_is_a_plan_issue_not_an_exception(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "empty.srt"
        source.write_text("", encoding="utf-8")

        probe = probe_source(source, _ctx(tmp_path))

        assert probe.kind is SourceKind.SUBTITLE
        assert {issue.code for issue in probe.warnings} == {"SOURCE_UNUSABLE"}

    def test_bare_urls_and_share_text_are_classified_as_url(self, tmp_path: Path) -> None:
        from porter.core.utils.source import normalize_source_input

        assert normalize_source_input("www.youtube.com/watch?v=dQw4w9WgXcQ") == (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            True,
        )
        assert normalize_source_input("youtu.be/dQw4w9WgXcQ") == (
            "https://youtu.be/dQw4w9WgXcQ",
            True,
        )
        assert normalize_source_input("bilibili.com/video/BV1GJ411x7h7") == (
            "https://bilibili.com/video/BV1GJ411x7h7",
            True,
        )
        assert normalize_source_input("【官方 MV】 https://b23.tv/abc123。") == (
            "https://b23.tv/abc123",
            True,
        )
        quoted_missing = f'"{tmp_path / "missing.mp4"}"'
        assert normalize_source_input(quoted_missing) == (str(tmp_path / "missing.mp4"), False)


class TestWorkflowEdgeCases:
    def test_asr_workflow_does_not_skip_translation_when_platform_lists_target_track(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = _source_probe(
            tracks=(
                TrackInfo(lang="en", role="source"),
                TrackInfo(lang="zh-Hans", role="target", auto_generated=True),
            )
        )
        plan = _resolve(tmp_path, monkeypatch, probe=probe, transcript="whisper-local")

        assert plan.workflow is WorkflowId.URL_ASR
        assert "TRANSLATION_SKIPPED" not in _codes(plan)
        assert plan.stage(Stage.TRANSLATE) is not None

    def test_explicit_translate_capability_overrides_platform_target_track(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = _source_probe(
            tracks=(
                TrackInfo(lang="en", role="source"),
                TrackInfo(lang="zh-Hans", role="target", auto_generated=True),
            )
        )
        plan = _resolve(tmp_path, monkeypatch, probe=probe, translate="google")

        assert plan.workflow is WorkflowId.URL_SUBTITLED
        assert "TRANSLATION_SKIPPED" not in _codes(plan)
        translate = plan.stage(Stage.TRANSLATE)
        assert translate is not None
        assert translate.capability == "translate.google"

    def test_release_with_burn_skip_omits_render_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve(
            tmp_path,
            monkeypatch,
            probe=_source_probe(),
            catalog=_catalog("render.ffmpeg-libass"),
            task=Task.RELEASE,
            burn=BurnMode.SKIP,
        )

        assert plan.feasible is True
        assert plan.stage(Stage.RENDER) is None

    def test_probe_task_on_subtitle_file_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "captions.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        _stub(monkeypatch)
        ctx = _ctx(tmp_path)
        plan = resolve_plan(
            str(source), PlanOverrides(task=Task.PROBE, output_dir=tmp_path / "out"), ctx
        )

        outcome = execute_plan(plan, ctx)

        assert outcome.workflow.state is JobState.DONE
        assert outcome.workflow.error is None
        assert [stage.stage for stage in outcome.workflow.stages] == [Stage.PROBE]

    def test_runtime_failure_populates_alternatives_and_suggested_workflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from porter.core.workflow.executor import _error_for

        ctx = _ctx(tmp_path)
        monkeypatch.setattr(
            "porter.core.workflow.capabilities.capability_catalog",
            lambda _ctx: _catalog("transcript.whisper-api", "translate.llm"),
        )

        subtitled_plan = _plan(
            workflow=WorkflowId.URL_SUBTITLED,
            stages=(
                StagePlan(
                    id="transcript",
                    stage=Stage.TRANSCRIPT,
                    capability="transcript.platform-track",
                ),
            ),
        )
        t_err = _error_for(
            subtitled_plan,
            subtitled_plan.stage(Stage.TRANSCRIPT),
            PorterError("platform caption 429"),
            ctx,
        )
        assert t_err.suggested_workflow is WorkflowId.URL_ASR
        assert "transcript.whisper-local" in t_err.alternatives

        trans_plan = _plan(
            workflow=WorkflowId.URL_ASR,
            stages=(
                StagePlan(id="translate", stage=Stage.TRANSLATE, capability="translate.bing"),
            ),
        )
        tr_err = _error_for(
            trans_plan,
            trans_plan.stage(Stage.TRANSLATE),
            PorterError("bing 429"),
            ctx,
        )
        assert tr_err.suggested_workflow is None
        assert set(tr_err.alternatives) == {"translate.google", "translate.mymemory"}

    def test_empty_string_cookies_in_config_coerce_to_none(self) -> None:
        cfg = PorterConfig.model_validate({"cookies_file": "", "cookies_browser": "   "})
        assert cfg.cookies_file is None
        assert cfg.cookies_browser is None

    def test_file_uri_and_quoted_local_path_build_valid_executor_request(
        self, tmp_path: Path
    ) -> None:
        from porter.core.workflow.executor import _request

        sample = tmp_path / "sample.mp4"
        plan = _plan(
            workflow=WorkflowId.LOCAL_ASR,
            source=sample.as_uri(),
            source_probe=_source_probe(kind=SourceKind.LOCAL_VIDEO, source=str(sample)),
        )
        req = _request(plan)
        assert req.local_video == sample
        assert req.url is None
