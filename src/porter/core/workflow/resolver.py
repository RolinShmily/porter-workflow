"""Turn a probed source into exactly one resolved workflow.

The decisions here are deliberately boring and explicit:

1. Probe the source.
2. Choose the *workflow* from what the source actually is: a link with its own
   subtitle track, a link that needs recognition, a local file, a subtitle, or
   an audio file.
3. Choose exactly one capability for each stage the task needs.
4. Emit blockers with alternatives when a choice cannot be made.

There is no "try this, then that" here. The old pipeline's fallback chain is
gone from the executed path; if the selected ASR engine fails at runtime, the
executor reports that failure. Substitution is a decision for a new plan, not a
side effect of the last one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from porter import __version__
from porter.core.context import RunContext
from porter.core.models.request import BurnMode
from porter.core.workflow.capabilities import (
    ACQUIRE_LOCAL,
    ACQUIRE_YTDLP_VIDEO,
    NORMALIZE_FFMPEG,
    PROBE_FFPROBE,
    PROBE_SUBTITLE,
    PROBE_YTDLP,
    RENDER_FFMPEG,
    TRANSCRIPT_PLATFORM,
    TRANSCRIPT_SUPPLIED,
    TRANSCRIPT_WHISPER_API,
    TRANSCRIPT_WHISPER_LOCAL,
    TRANSLATE_BING,
    TRANSLATE_GOOGLE,
    TRANSLATE_LLM,
    TRANSLATE_MYMEMORY,
    WRITE_SUBTITLES,
    capability_catalog,
    capability_for,
    normalize_capability,
    selection_problem,
)
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
    WorkflowId,
    WorkflowPlan,
)
from porter.core.workflow.probe import probe_source

__all__ = ["PlanOverrides", "config_fingerprint", "resolve_plan"]

#: Translation backends tried by default, in preference order. All are key-free
#: network calls; ``translate.llm`` is appended only when the caller opted into
#: paid capabilities. The order is not a fallback chain: exactly one is chosen.
_FREE_TRANSLATORS = (TRANSLATE_BING, TRANSLATE_GOOGLE, TRANSLATE_MYMEMORY)


class PlanOverrides(BaseModel):
    """Caller choices that constrain the resolver.

    These are requests, not pipeline settings. ``transcript`` and ``translate``
    name a capability; if it is unavailable the plan is blocked rather than
    silently substituting another. ``allow_paid`` is the only way a paid
    capability can be chosen *without* being named explicitly.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: Task = Task.RELEASE
    output_dir: Path = Path("./porter_output")
    target_lang: str = "zh-Hans"
    burn: BurnMode = BurnMode.DUAL
    transcript: str | None = None
    translate: str | None = None
    render: str | None = None
    subtitle_file: Path | None = None
    cookies_file: Path | None = None
    cookies_browser: str | None = None
    llm_model: str | None = None
    force: bool = False
    audio_denoise: bool = True
    allow_paid: bool = False
    force_translate: bool = False


def resolve_plan(
    source: str | Path,
    options: PlanOverrides,
    ctx: RunContext,
) -> WorkflowPlan:
    """Build the plan for ``source`` under ``options``.

    The returned plan is sealed with a content hash. ``feasible`` is false when
    any blocker was found; the stages still describe the workflow that *would*
    run if the blocker were resolved, so an agent can show the user a concrete
    fix rather than only an error.
    """
    probe = probe_source(str(source), ctx)
    issues: list[PlanIssue] = list(probe.warnings)

    task = _effective_task(probe, options.task, issues)
    workflow = _choose_workflow(probe, options)
    catalog = capability_catalog(ctx)

    stages = _build_stages(
        workflow=workflow,
        task=task,
        probe=probe,
        options=options,
        ctx=ctx,
        catalog=catalog,
        issues=issues,
    )

    from porter.core.utils.source import normalize_source_input

    blockers = [issue for issue in issues if issue.severity == "blocker"]
    normalized_source, _ = normalize_source_input(source)
    canonical_source = (
        probe.source
        if probe.kind is SourceKind.URL and probe.source
        else (normalized_source or str(source))
    )
    plan = WorkflowPlan(
        porter_version=__version__,
        created_at=datetime.now(UTC).isoformat(),
        workflow=workflow,
        task=task,
        source=canonical_source,
        runtime=PlanRuntime(
            output_dir=str(options.output_dir),
            target_lang=options.target_lang,
            force=options.force,
            audio_denoise=options.audio_denoise,
        ),
        config_fingerprint=config_fingerprint(ctx),
        source_probe=probe,
        stages=tuple(stages),
        feasible=not blockers,
        issues=tuple(issues),
    ).sealed()
    return plan


# ----------------------------------------------------------------------
# Workflow choice
# ----------------------------------------------------------------------


def _effective_task(
    probe: SourceProbe,
    requested: Task,
    issues: list[PlanIssue],
) -> Task:
    """Resolve tasks that do not make sense for the source.

    A subtitle file cannot be *released*, and an audio file has no video to burn.
    Rather than inventing several parallel task enums, the resolver corrects the
    obvious cases and reports the ones that are genuinely impossible.
    """
    if probe.kind is SourceKind.SUBTITLE:
        if requested is Task.RELEASE:
            return Task.SUBTITLE
        if requested in (Task.TRANSCRIBE, Task.AUDIO):
            issues.append(
                PlanIssue(
                    code="TASK_REQUIRES_MEDIA",
                    message=f"task {requested.value!r} needs audio or video; this source is a subtitle file",
                    fix="use --task subtitle to translate the subtitle, or --task convert for a format change",
                )
            )
            return Task.SUBTITLE
    if probe.kind is SourceKind.AUDIO and requested is Task.RELEASE:
        issues.append(
            PlanIssue(
                code="NO_VIDEO_FOR_RELEASE",
                message="the source is audio-only, so there is no video to burn",
                fix="request --task subtitle or --task transcribe instead",
            )
        )
    return requested


def _choose_workflow(probe: SourceProbe, options: PlanOverrides) -> WorkflowId:
    if probe.kind is SourceKind.SUBTITLE:
        return WorkflowId.SUBTITLE_ONLY
    if probe.kind is SourceKind.AUDIO:
        return WorkflowId.AUDIO_ASR

    # An explicit transcript request chooses the workflow, not just a backend.
    # Without this, a URL that lists a subtitle track could not be planned for
    # ASR at all: the request was rejected as "the wrong workflow" instead of
    # producing the workflow the caller asked for.
    requested = (
        normalize_capability(options.transcript, Stage.TRANSCRIPT)
        if options.transcript
        else None
    )
    if requested in (TRANSCRIPT_WHISPER_LOCAL, TRANSCRIPT_WHISPER_API):
        subtitled = False
    elif requested in (TRANSCRIPT_PLATFORM, TRANSCRIPT_SUPPLIED):
        subtitled = True
    else:
        subtitled = options.subtitle_file is not None or probe.track("source") is not None

    if probe.kind is SourceKind.URL:
        return WorkflowId.URL_SUBTITLED if subtitled else WorkflowId.URL_ASR
    return WorkflowId.LOCAL_SUBTITLED if subtitled else WorkflowId.LOCAL_ASR


# ----------------------------------------------------------------------
# Stages
# ----------------------------------------------------------------------


def _build_stages(
    *,
    workflow: WorkflowId,
    task: Task,
    probe: SourceProbe,
    options: PlanOverrides,
    ctx: RunContext,
    catalog: tuple[CapabilityDescriptor, ...],
    issues: list[PlanIssue],
) -> list[StagePlan]:
    stages: list[StagePlan] = [
        StagePlan(
            id="probe",
            stage=Stage.PROBE,
            capability=_probe_capability(probe),
            produces=("source_facts",),
        )
    ]
    if task is Task.PROBE:
        return stages

    # Before any workflow branch: "convert" means a subtitle format change, and
    # neither the subtitle branch (which translates) nor the media branch (which
    # releases) implements that. Planning the wrong work silently is worse than
    # reporting it as not wired yet.
    if task is Task.CONVERT:
        issues.append(
            PlanIssue(
                code="TASK_NOT_SUPPORTED",
                message="subtitle format conversion is not wired into the executor yet",
                fix="use --task subtitle to translate the file for now",
                stage=Stage.WRITE,
            )
        )
        return stages

    if workflow is WorkflowId.SUBTITLE_ONLY:
        _build_subtitle_stages(stages, probe, options, ctx, catalog, issues)
        return stages

    _build_media_stages(
        stages,
        workflow=workflow,
        task=task,
        probe=probe,
        options=options,
        ctx=ctx,
        catalog=catalog,
        issues=issues,
    )
    return stages


def _build_subtitle_stages(
    stages: list[StagePlan],
    probe: SourceProbe,
    options: PlanOverrides,
    ctx: RunContext,
    catalog: tuple[CapabilityDescriptor, ...],
    issues: list[PlanIssue],
) -> None:
    requested = (
        normalize_capability(options.transcript, Stage.TRANSCRIPT)
        if options.transcript
        else None
    )
    if requested is not None and requested != TRANSCRIPT_SUPPLIED:
        issues.append(
            PlanIssue(
                code="CAPABILITY_WRONG_SOURCE",
                stage=Stage.TRANSCRIPT,
                capability=requested,
                message=(
                    f"{requested} needs audio, but the whole input is a subtitle "
                    "file; the plan will not ignore the request silently"
                ),
                alternatives=(TRANSCRIPT_SUPPLIED,),
                fix="pass a media file to transcribe, or drop --transcript",
            )
        )

    supplied = options.subtitle_file or _track_path(probe)
    stages.append(
        StagePlan(
            id="transcript",
            stage=Stage.TRANSCRIPT,
            capability=TRANSCRIPT_SUPPLIED,
            params={"path": str(supplied) if supplied else None},
            produces=("source_transcript",),
        )
    )

    translator = _choose_translate(options, catalog, issues)
    if translator is not None:
        stages.append(
            StagePlan(
                id="translate",
                stage=Stage.TRANSLATE,
                capability=translator,
                params={
                    "target_lang": options.target_lang,
                    "llm_model": options.llm_model,
                },
                needs=("source_transcript",),
                produces=("target_transcript",),
            )
        )
    stages.append(
        StagePlan(
            id="write",
            stage=Stage.WRITE,
            capability=_fixed_capability(Stage.WRITE, WRITE_SUBTITLES, catalog, issues),
            needs=("target_transcript",),
            produces=("bilingual_srt", "zh_srt", "bilingual_ass", "zh_ass"),
        )
    )


def _build_media_stages(
    stages: list[StagePlan],
    *,
    workflow: WorkflowId,
    task: Task,
    probe: SourceProbe,
    options: PlanOverrides,
    ctx: RunContext,
    catalog: tuple[CapabilityDescriptor, ...],
    issues: list[PlanIssue],
) -> None:
    if probe.kind is SourceKind.URL:
        stages.append(
            StagePlan(
                id="acquire",
                stage=Stage.ACQUIRE,
                # The current extractor always downloads media; claiming an
                # audio-only acquisition here would be a plan that describes a
                # pipeline nobody runs.
                capability=_fixed_capability(
                    Stage.ACQUIRE, ACQUIRE_YTDLP_VIDEO, catalog, issues
                ),
                params={
                    "cookies_file": str(options.cookies_file) if options.cookies_file else None,
                    "cookies_browser": options.cookies_browser,
                },
                produces=("source_media", "platform_subtitles"),
            )
        )
    else:
        stages.append(
            StagePlan(
                id="acquire",
                stage=Stage.ACQUIRE,
                capability=_fixed_capability(Stage.ACQUIRE, ACQUIRE_LOCAL, catalog, issues),
                params={"path": str(probe.source)},
                produces=("source_media",),
            )
        )

    stages.append(
        StagePlan(
            id="normalize",
            stage=Stage.NORMALIZE,
            capability=_fixed_capability(Stage.NORMALIZE, NORMALIZE_FFMPEG, catalog, issues),
            params={"video": probe.has_video, "audio": True},
            needs=("source_media",),
            produces=("master_video", "master_audio"),
        )
    )
    if task is Task.AUDIO:
        return

    transcript = _choose_transcript(
        workflow=workflow,
        probe=probe,
        options=options,
        ctx=ctx,
        catalog=catalog,
        issues=issues,
    )
    stages.append(
        StagePlan(
            id="transcript",
            stage=Stage.TRANSCRIPT,
            capability=transcript,
            params=_transcript_params(transcript, probe, options),
            needs=("master_audio",),
            produces=("source_transcript",),
        )
    )
    if task is Task.TRANSCRIBE:
        return

    target_track = probe.track("target")
    # Only skip translation when the plan actually uses the platform subtitle
    # track AND the caller did not ask for a specific translator or force
    # translation. An ASR or supplied-subtitle workflow never loads the
    # platform's Chinese track, so skipping translation there would crash the
    # executor with zero translation backends.
    reuses_platform_target = (
        transcript == TRANSCRIPT_PLATFORM
        and target_track is not None
        and not options.force_translate
        and not options.translate
    )
    needs_translation = not reuses_platform_target
    if needs_translation:
        translator = _choose_translate(options, catalog, issues)
        if translator is not None:
            stages.append(
                StagePlan(
                    id="translate",
                    stage=Stage.TRANSLATE,
                    capability=translator,
                    params={
                        "target_lang": options.target_lang,
                        "llm_model": options.llm_model,
                    },
                    needs=("source_transcript",),
                    produces=("target_transcript",),
                )
            )
    else:
        issues.append(
            PlanIssue(
                code="TRANSLATION_SKIPPED",
                severity="warning",
                stage=Stage.TRANSLATE,
                message=(
                    "the platform offers a target-language track; translation is "
                    "skipped and the planner does not verify that the track arrives"
                ),
                fix="pass --force-translate (or select a translator) to translate anyway",
            )
        )

    stages.append(
        StagePlan(
            id="write",
            stage=Stage.WRITE,
            capability=_fixed_capability(Stage.WRITE, WRITE_SUBTITLES, catalog, issues),
            needs=("target_transcript",),
            produces=("bilingual_srt", "zh_srt", "bilingual_ass", "zh_ass"),
        )
    )

    if task is Task.RELEASE and options.burn is not BurnMode.SKIP:
        render = _choose_render(options, catalog, issues)
        stages.append(
            StagePlan(
                id="render",
                stage=Stage.RENDER,
                capability=render,
                params={"mode": options.burn.value},
                needs=("master_video", "bilingual_ass", "zh_ass"),
                produces=_render_products(options.burn),
            )
        )


# ----------------------------------------------------------------------
# Capability selection
# ----------------------------------------------------------------------


def _probe_capability(probe: SourceProbe) -> str:
    if probe.kind is SourceKind.URL:
        return PROBE_YTDLP
    if probe.kind is SourceKind.SUBTITLE:
        return PROBE_SUBTITLE
    return PROBE_FFPROBE


def _track_path(probe: SourceProbe) -> str | None:
    track = probe.track("source")
    return track.path if track is not None else None


def _transcript_params(
    capability: str,
    probe: SourceProbe,
    options: PlanOverrides,
) -> dict[str, object]:
    if capability == TRANSCRIPT_PLATFORM:
        track = probe.track("source")
        return {"track_lang": track.lang if track is not None else None}
    if capability == TRANSCRIPT_SUPPLIED:
        supplied = options.subtitle_file or _track_path(probe)
        return {"path": str(supplied) if supplied else None}
    return {}


def _choose_transcript(
    *,
    workflow: WorkflowId,
    probe: SourceProbe,
    options: PlanOverrides,
    ctx: RunContext,
    catalog: tuple[CapabilityDescriptor, ...],
    issues: list[PlanIssue],
) -> str:
    requested = options.transcript

    if workflow in (WorkflowId.URL_SUBTITLED, WorkflowId.LOCAL_SUBTITLED):
        # A subtitled workflow is a promise not to run ASR. Workflow selection
        # already sends an explicit ASR request to an ASR workflow, so anything
        # that reaches here is a subtitled peer, an unknown id, or a capability
        # from another stage; _select_requested reports the latter two.
        candidates = (TRANSCRIPT_PLATFORM, TRANSCRIPT_SUPPLIED)
        if requested:
            requested_id = normalize_capability(requested, Stage.TRANSCRIPT)
            chosen = _select_requested(
                Stage.TRANSCRIPT,
                requested_id,
                candidates,
                catalog,
                issues,
                allow_paid=True,
            )
        else:
            default = (
                TRANSCRIPT_SUPPLIED
                if options.subtitle_file is not None
                else TRANSCRIPT_PLATFORM
            )
            if _available(catalog, default):
                chosen = default
            else:
                chosen = _report_missing(Stage.TRANSCRIPT, default, candidates, issues)
        _check_subtitled_source(chosen, probe, options, candidates, issues)
        return chosen

    # Config's ``asr.engine`` is a preference for which engine runs *when ASR
    # runs*; it does not force ASR (that is the workflow's decision) and it is
    # not consent to spend money. It is honoured only when it names an available,
    # policy-allowed ASR capability.
    asr_candidates = [TRANSCRIPT_WHISPER_LOCAL]
    if options.allow_paid:
        asr_candidates.append(TRANSCRIPT_WHISPER_API)
    if requested:
        requested_id = normalize_capability(requested, Stage.TRANSCRIPT)
        if requested_id in (TRANSCRIPT_PLATFORM, TRANSCRIPT_SUPPLIED):
            issues.append(
                PlanIssue(
                    code="CAPABILITY_WRONG_WORKFLOW",
                    stage=Stage.TRANSCRIPT,
                    capability=requested_id,
                    message=(
                        f"{requested_id} needs a subtitle track, but this source has "
                        "none; the plan will not silently choose a different input"
                    ),
                    alternatives=tuple(asr_candidates),
                    fix="pass --subtitle-file with an existing .srt/.vtt, or plan ASR",
                )
            )
            return requested_id
        return _select_requested(
            Stage.TRANSCRIPT,
            requested_id,
            tuple(asr_candidates),
            catalog,
            issues,
            allow_paid=True,
        )

    configured = (ctx.config.asr.engine or "").strip() or None
    if configured:
        configured_id = normalize_capability(configured, Stage.TRANSCRIPT)
        if configured_id in (TRANSCRIPT_WHISPER_LOCAL, TRANSCRIPT_WHISPER_API):
            descriptor = capability_for(catalog, configured_id)
            usable = (
                descriptor is not None
                and descriptor.available
                and (descriptor.cost is not Cost.PAID or options.allow_paid)
            )
            if usable:
                return configured_id

    return _select_default(Stage.TRANSCRIPT, tuple(asr_candidates), catalog, issues)


def _check_subtitled_source(
    capability: str,
    probe: SourceProbe,
    options: PlanOverrides,
    candidates: tuple[str, ...],
    issues: list[PlanIssue],
) -> None:
    """A subtitled peer needs something to read.

    Choosing the platform track or a supplied file is only feasible if the track
    was listed or the file was named. Without this, the plan would report
    feasible and the executor would fail at TRANSCRIBE -- the prediction the
    plan exists to make before any work starts.
    """
    if capability == TRANSCRIPT_PLATFORM and probe.track("source") is None:
        issues.append(
            PlanIssue(
                code="PLATFORM_TRACK_MISSING",
                stage=Stage.TRANSCRIPT,
                capability=capability,
                message="this source listed no source-language subtitle track",
                alternatives=tuple(
                    candidate for candidate in candidates if candidate != capability
                ),
                fix="pass --subtitle-file with an existing .srt/.vtt, or plan ASR",
            )
        )
    if (
        capability == TRANSCRIPT_SUPPLIED
        and options.subtitle_file is None
        and _track_path(probe) is None
    ):
        issues.append(
            PlanIssue(
                code="SUPPLIED_SUBTITLE_MISSING",
                stage=Stage.TRANSCRIPT,
                capability=capability,
                message="the plan chose a supplied subtitle file, but none was named",
                alternatives=tuple(
                    candidate for candidate in candidates if candidate != capability
                ),
                fix="pass --subtitle-file with an existing .srt/.vtt",
            )
        )
    if (
        capability == TRANSCRIPT_SUPPLIED
        and options.subtitle_file is not None
        and not options.subtitle_file.is_file()
    ):
        issues.append(
            PlanIssue(
                code="SOURCE_NOT_FOUND",
                stage=Stage.TRANSCRIPT,
                capability=capability,
                message=f"subtitle file not found: {options.subtitle_file}",
                fix="check the path",
            )
        )


def _choose_translate(
    options: PlanOverrides,
    catalog: tuple[CapabilityDescriptor, ...],
    issues: list[PlanIssue],
) -> str | None:
    requested = options.translate
    candidates: list[str] = list(_FREE_TRANSLATORS)
    if options.allow_paid:
        candidates.append(TRANSLATE_LLM)

    if requested:
        requested_id = normalize_capability(requested, Stage.TRANSLATE)
        return _select_requested(
            Stage.TRANSLATE,
            requested_id,
            tuple(candidates),
            catalog,
            issues,
            allow_paid=True,
        )

    return _select_default(Stage.TRANSLATE, tuple(candidates), catalog, issues)


def _choose_render(
    options: PlanOverrides,
    catalog: tuple[CapabilityDescriptor, ...],
    issues: list[PlanIssue],
) -> str:
    requested = options.render
    if requested:
        requested_id = normalize_capability(requested, Stage.RENDER)
        return _select_requested(
            Stage.RENDER,
            requested_id,
            (RENDER_FFMPEG,),
            catalog,
            issues,
            allow_paid=True,
        )
    return _select_default(Stage.RENDER, (RENDER_FFMPEG,), catalog, issues)


def _available(catalog: tuple[CapabilityDescriptor, ...], capability: str) -> bool:
    descriptor = capability_for(catalog, capability)
    return descriptor is not None and descriptor.available


def _fixed_capability(
    stage: Stage,
    capability: str,
    catalog: tuple[CapabilityDescriptor, ...],
    issues: list[PlanIssue],
) -> str:
    """A stage whose implementation cannot be swapped must still be probed.

    Acquire, normalize and write each have exactly one implementation, so there
    is no candidate list to choose from. That is not a reason to skip the
    availability check: planning those stages on a machine without ffmpeg is
    exactly the "feasible: yes, fails in the first second" promise the plan
    exists to prevent.
    """
    descriptor = capability_for(catalog, capability)
    if descriptor is not None and descriptor.available:
        return capability
    requires = ", ".join(descriptor.requires) if descriptor is not None else ""
    issues.append(
        PlanIssue(
            code="CAPABILITY_UNAVAILABLE",
            stage=stage,
            capability=capability,
            message=(
                f"{capability} is not available on this machine"
                + (f" (requires {requires})" if requires else "")
            ),
            fix=_fix_for(stage, ()),
        )
    )
    return capability


def _select_requested(
    stage: Stage,
    capability: str,
    candidates: tuple[str, ...],
    catalog: tuple[CapabilityDescriptor, ...],
    issues: list[PlanIssue],
    *,
    allow_paid: bool,
) -> str:
    descriptor = capability_for(catalog, capability)
    if descriptor is not None and descriptor.stage is not stage:
        issues.append(
            PlanIssue(
                code="CAPABILITY_WRONG_STAGE",
                stage=stage,
                capability=capability,
                message=(
                    f"{capability} is a {descriptor.stage.value} capability, not a "
                    f"{stage.value} capability"
                ),
                alternatives=tuple(
                    candidate for candidate in candidates if _available(catalog, candidate)
                ),
                fix=_fix_for(
                    stage,
                    tuple(candidate for candidate in candidates if _available(catalog, candidate)),
                ),
            )
        )
        return capability
    alternatives = tuple(
        candidate
        for candidate in candidates
        if candidate != capability and _available(catalog, candidate)
    )
    problem = selection_problem(
        descriptor,
        requested=capability,
        available_alternatives=alternatives,
        allow_paid=allow_paid,
    )
    if problem is not None:
        issues.append(
            PlanIssue(
                code=problem.code,
                stage=stage,
                capability=capability,
                message=problem.message,
                alternatives=problem.alternatives,
                fix=_fix_for(stage, problem.alternatives),
            )
        )
    return capability


def _select_default(
    stage: Stage,
    candidates: tuple[str, ...],
    catalog: tuple[CapabilityDescriptor, ...],
    issues: list[PlanIssue],
) -> str:
    for candidate in candidates:
        descriptor = capability_for(catalog, candidate)
        if descriptor is None or not descriptor.available:
            continue
        return candidate

    return _report_missing(stage, candidates[0], candidates, issues)


def _report_missing(
    stage: Stage,
    fallback: str,
    candidates: tuple[str, ...],
    issues: list[PlanIssue],
) -> str:
    available = tuple(
        candidate for candidate in candidates if candidate != fallback
    )
    issues.append(
        PlanIssue(
            code="NO_CAPABILITY",
            stage=stage,
            capability=fallback,
            message=f"no {stage.value} capability is available on this machine",
            alternatives=available,
            fix=_fix_for(stage, available),
        )
    )
    return fallback


def _fix_for(stage: Stage, alternatives: tuple[str, ...]) -> str:
    if alternatives:
        return f"choose one of: {', '.join(alternatives)}"
    if stage is Stage.TRANSCRIPT:
        return (
            "install porter-workflow[asr-local], configure an API key and pass "
            "--allow-paid, or pass --subtitle-file with an existing .srt/.vtt"
        )
    if stage is Stage.TRANSLATE:
        return "install requests (the key-free translators need it) or pass --allow-paid with an LLM key"
    if stage in (Stage.ACQUIRE, Stage.NORMALIZE):
        return (
            "install ffmpeg and ffprobe (yt-dlp is required for links); "
            "'porter doctor' lists exactly what is missing"
        )
    if stage is Stage.RENDER:
        return "install ffmpeg with libass, or plan --task subtitle to skip burning"
    return "install the missing dependency named in the issue"


def _render_products(mode: BurnMode) -> tuple[str, ...]:
    products: list[str] = []
    if mode.wants_bilingual:
        products.append("video_bilingual")
    if mode.wants_zh:
        products.append("video_zh")
    return tuple(products)


# ----------------------------------------------------------------------
# Fingerprint
# ----------------------------------------------------------------------


def config_fingerprint(ctx: RunContext) -> str:
    """Hash the non-secret configuration a plan depends on.

    Secrets are excluded: a key's *value* must not influence a plan id that an
    agent may paste into a transcript. Whether a key exists is already reflected
    in the capability probe (and therefore in the plan's stage choice).
    """
    data = ctx.config.model_dump(mode="json")
    data.get("llm", {}).pop("api_key", None)
    data.get("asr", {}).pop("whisper_api_key", None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
