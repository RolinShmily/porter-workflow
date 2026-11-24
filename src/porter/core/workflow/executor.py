"""Execute exactly the stages and capabilities a plan names.

The rules this module enforces, because they are the point of the refactor:

* **No cross-capability substitution.** The transcriber and translator are built
  from the plan's single selected capability. If it fails, the job fails with a
  structured error and alternatives for the *next* plan. It does not silently
  try another engine.
* **The plan is the unit of work.** ``--plan`` is executable: the output
  directory, target language, burn mode, supplied subtitle and cookies all come
  from the plan, not from hidden defaults.
* **A changed configuration invalidates a plan.** The plan stores a fingerprint
  of the non-secret configuration it was built against. Running it under a
  different ffmpeg path or ASR model is refused rather than silently reinterpreted.

Expected failures are returned as data (:class:`WorkflowResult` and
:class:`JobResult`), not raised; the CLI and MCP frontends map them to exit codes
and job records respectively.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from porter.core.asr.chain import (
    BILINGUAL_ASS_NAME,
    BILINGUAL_SRT_NAME,
    TRANSCRIPT_JSON_NAME,
    TRANSCRIPT_TXT_NAME,
    ZH_ASS_NAME,
    ZH_SRT_NAME,
    AsrChain,
)
from porter.core.context import RunContext
from porter.core.errors import JobCancelled, PorterError
from porter.core.events import (
    ErrorInfo,
    JobState,
    Phase,
    PhaseCompleted,
    PhaseFailed,
    PhaseStarted,
)
from porter.core.logging import get_logger
from porter.core.models.materials import RawMaterials, TaskLayout
from porter.core.models.request import BurnMode, BurnResult, JobOptions, JobRequest, JobResult
from porter.core.models.subtitle import SubtitleSet
from porter.core.subtitles.srt import generate_zh_srt
from porter.core.workflow.capabilities import (
    TRANSCRIPT_PLATFORM,
    TRANSCRIPT_SUPPLIED,
    TRANSCRIPT_WHISPER_API,
    TRANSCRIPT_WHISPER_LOCAL,
    TRANSLATE_BING,
    TRANSLATE_GOOGLE,
    TRANSLATE_LLM,
    TRANSLATE_MYMEMORY,
)
from porter.core.workflow.model import (
    PlanIssue,
    SourceKind,
    Stage,
    StageError,
    StagePlan,
    StageResult,
    Task,
    WorkflowId,
    WorkflowPlan,
    WorkflowResult,
)
from porter.core.workflow.resolver import config_fingerprint

__all__ = ["ExecutionOutcome", "execute_plan"]

_logger = get_logger(__name__)

_PHASE_FOR_STAGE: dict[Stage, Phase] = {
    Stage.ACQUIRE: Phase.PREPARE,
    Stage.NORMALIZE: Phase.PREPARE,
    Stage.TRANSCRIPT: Phase.TRANSCRIBE,
    Stage.TRANSLATE: Phase.TRANSLATE,
    Stage.WRITE: Phase.TRANSLATE,
    Stage.RENDER: Phase.BURN,
}


@dataclass(frozen=True)
class ExecutionOutcome:
    """Both views of one execution.

    ``workflow`` is the agent-facing result (stages, artifacts, error codes);
    ``job`` is the existing job-registry shape. Keeping both lets the refactor
    move the CLI without changing the job store or MCP record format.
    """

    workflow: WorkflowResult
    job: JobResult


def execute_plan(plan: WorkflowPlan, ctx: RunContext) -> ExecutionOutcome:
    """Run ``plan`` against ``ctx``.

    The context's ``options`` are replaced from the plan, so an executor can
    never read a stale frontend default. The caller supplies only the job id and
    the resolved configuration.
    """
    started = time.monotonic()
    ctx.options = _job_options(plan)

    preflight = _preflight(plan, ctx)
    if preflight is not None:
        return _failure_outcome(plan, ctx, started, preflight)

    if plan.workflow is WorkflowId.SUBTITLE_ONLY:
        return _execute_subtitle_plan(plan, ctx, started)
    return _execute_media_plan(plan, ctx, started)


# ----------------------------------------------------------------------
# Preflight
# ----------------------------------------------------------------------


def _preflight(plan: WorkflowPlan, ctx: RunContext) -> StageError | None:
    if not plan.feasible:
        issue = plan.blockers[0] if plan.blockers else None
        if issue is None:  # defensive: feasible=False without an issue is a bug
            return StageError(code="PLAN_INFEASIBLE", message="the plan is not feasible")
        return _error_from_issue(issue)

    if plan.config_fingerprint and plan.config_fingerprint != config_fingerprint(ctx):
        return StageError(
            code="PLAN_CONFIG_CHANGED",
            message=(
                "the configuration changed since this plan was made; its capability "
                "choices and parameters can no longer be trusted"
            ),
            retryable=False,
            details={
                "planned": plan.config_fingerprint,
                "current": config_fingerprint(ctx),
            },
        )
    return None


# ----------------------------------------------------------------------
# Media workflows (URL or local, with or without a platform track)
# ----------------------------------------------------------------------


def _execute_media_plan(
    plan: WorkflowPlan,
    ctx: RunContext,
    started: float,
) -> ExecutionOutcome:
    request = _request(plan)
    pipeline = _build_pipeline(plan, ctx)

    raw: RawMaterials | None = None
    subtitles: SubtitleSet | None = None
    burn: BurnResult | None = None
    results: list[StageResult] = []
    current: StagePlan | None = None

    try:
        for phase, group in _phase_groups(plan.stages):
            if phase is None:
                for stage in group:
                    results.append(_stage_result(stage, "reused", 0.0, {}))
                continue

            current = group[0]
            ctx.check_cancelled()
            ctx.emit(PhaseStarted(phase=phase))
            stage_started = time.monotonic()

            if phase is Phase.PREPARE:
                raw = pipeline.prepare(request, ctx)
                elapsed = time.monotonic() - stage_started
                for stage in group:
                    outputs = _outputs_for(stage, raw=raw)
                    results.append(_stage_result(stage, "done", elapsed, outputs))
                    elapsed = 0.0

            elif phase is Phase.TRANSCRIBE:
                if raw is None:
                    raise PorterError("no raw materials: the acquire stage did not run")
                subtitles = pipeline.transcribe(raw, ctx)
                elapsed = time.monotonic() - stage_started
                for stage in group:
                    results.append(
                        _stage_result(stage, "done", elapsed, _outputs_for(stage, raw=raw))
                    )
                    elapsed = 0.0

            elif phase is Phase.TRANSLATE:
                if subtitles is None:
                    raise PorterError("no source transcript: the transcript stage did not run")
                current = next(
                    (stage for stage in group if stage.stage is Stage.TRANSLATE),
                    group[0],
                )
                subtitles = pipeline.translate(subtitles, ctx)
                elapsed = time.monotonic() - stage_started
                for stage in group:
                    results.append(
                        _stage_result(
                            stage,
                            "done",
                            elapsed,
                            _outputs_for(stage, subtitles=subtitles),
                        )
                    )
                    elapsed = 0.0

            elif phase is Phase.BURN:
                if raw is None or subtitles is None:
                    raise PorterError("the burn stage requires a master and subtitles")
                burn = pipeline.burn(raw, subtitles, ctx)
                results.append(
                    _stage_result(
                        plan.stage(Stage.RENDER) or group[0],
                        "done",
                        time.monotonic() - stage_started,
                        _outputs_for(plan.stage(Stage.RENDER) or group[0], burn=burn),
                    )
                )

            ctx.emit(PhaseCompleted(phase=phase))

    except JobCancelled:
        _logger.warning("job %s cancelled", ctx.job_id)
        return _cancelled_outcome(plan, ctx, started, results, raw, subtitles, burn)

    except PorterError as exc:
        _logger.error("job %s failed at %s: %s", ctx.job_id, current.id if current else "?", exc)
        error = _error_for(plan, current, exc, ctx)
        if current is not None:
            results.append(
                _stage_result(current, "failed", 0.0, {}, error=error)
            )
        if current is not None:
            ctx.emit(
                PhaseFailed(
                    phase=_PHASE_FOR_STAGE.get(current.stage, Phase.PREPARE),
                    error=ErrorInfo(code=error.code, message=error.message, details=error.details),
                )
            )
        return _failure_outcome(plan, ctx, started, error, raw, subtitles, burn, results)

    workflow = _workflow_result(plan, ctx, JobState.DONE, started, results, raw, subtitles, burn)
    return ExecutionOutcome(workflow=workflow, job=_job_result_from(workflow, raw, subtitles, burn))


# ----------------------------------------------------------------------
# Subtitle-only workflow
# ----------------------------------------------------------------------


def _execute_subtitle_plan(
    plan: WorkflowPlan,
    ctx: RunContext,
    started: float,
) -> ExecutionOutcome:
    """Translate a subtitle file without any media.

    This is the local-media-processor path: no ffmpeg, no ASR, no download. The
    supplied file is the source track; the translation chain writes the four
    subtitle outputs exactly as it does for a video job.
    """

    results: list[StageResult] = []
    current: StagePlan | None = None
    probe_stage = plan.stage(Stage.PROBE)
    if probe_stage is not None:
        results.append(_stage_result(probe_stage, "reused", 0.0, {}))

    if plan.task is Task.PROBE:
        workflow = _workflow_result(plan, ctx, JobState.DONE, started, results, None, None, None)
        return ExecutionOutcome(
            workflow=workflow,
            job=_job_result_from(workflow, None, None, None),
        )

    try:
        transcript_stage = plan.stage(Stage.TRANSCRIPT)
        if transcript_stage is None:
            raise PorterError("the subtitle workflow has no transcript stage")
        path = transcript_stage.params.get("path")
        if not path:
            raise PorterError("the subtitle workflow has no input file")
        source_path = Path(str(path))

        from porter.core.asr.platform_subs import load_supplied_subtitles
        from porter.core.platforms.local import local_video_id

        items = load_supplied_subtitles(source_path)
        layout = TaskLayout.build(
            Path(plan.runtime.output_dir),
            local_video_id(source_path),
            source_path.stem,
        ).ensure_dirs()

        cooked = layout.cooked_dir
        subtitles = SubtitleSet(
            subtitle_bilingual_srt=cooked / BILINGUAL_SRT_NAME,
            subtitle_bilingual_ass=cooked / BILINGUAL_ASS_NAME,
            subtitle_zh_srt=cooked / ZH_SRT_NAME,
            subtitle_zh_ass=cooked / ZH_ASS_NAME,
            items=items,
            video_width=None,
            video_height=None,
            transcript_json_path=cooked / TRANSCRIPT_JSON_NAME,
            transcript_txt_path=cooked / TRANSCRIPT_TXT_NAME,
        )

        ctx.emit(PhaseStarted(phase=Phase.TRANSCRIBE))
        current = transcript_stage
        # Keep the on-disk contract: cooked/subtitle.srt is the source track.
        (cooked / "subtitle.srt").write_text(generate_zh_srt(items), encoding="utf-8")
        results.append(
            _stage_result(
                transcript_stage,
                "done",
                0.0,
                {"source_transcript": str(cooked / "subtitle.srt")},
            )
        )
        ctx.emit(PhaseCompleted(phase=Phase.TRANSCRIBE))

        translator = _build_translator(plan)
        ctx.emit(PhaseStarted(phase=Phase.TRANSLATE))
        current = plan.stage(Stage.TRANSLATE) or plan.stage(Stage.WRITE)
        stage_started = time.monotonic()
        subtitles = translator.translate(subtitles, plan.runtime.target_lang, ctx)
        elapsed = time.monotonic() - stage_started
        for stage in (plan.stage(Stage.TRANSLATE), plan.stage(Stage.WRITE)):
            if stage is None:
                continue
            results.append(
                _stage_result(stage, "done", elapsed, _outputs_for(stage, subtitles=subtitles))
            )
            elapsed = 0.0
        ctx.emit(PhaseCompleted(phase=Phase.TRANSLATE))

    except JobCancelled:
        return _cancelled_outcome(plan, ctx, started, results, None, None, None)

    except PorterError as exc:
        error = _error_for(plan, current, exc, ctx)
        if current is not None:
            results.append(_stage_result(current, "failed", 0.0, {}, error=error))
            ctx.emit(
                PhaseFailed(
                    phase=_PHASE_FOR_STAGE.get(current.stage, Phase.TRANSCRIBE),
                    error=ErrorInfo(code=error.code, message=error.message, details=error.details),
                )
            )
        return _failure_outcome(plan, ctx, started, error, None, None, None, results)

    workflow = _workflow_result(
        plan,
        ctx,
        JobState.DONE,
        started,
        results,
        None,
        subtitles,
        None,
        task_dir=layout.task_dir,
    )
    return ExecutionOutcome(
        workflow=workflow,
        job=_job_result_from(workflow, None, subtitles, None, task_dir=layout.task_dir),
    )


# ----------------------------------------------------------------------
# Pipeline assembly
# ----------------------------------------------------------------------


def _build_pipeline(plan: WorkflowPlan, ctx: RunContext) -> Any:
    from porter.core.media.burn import FfmpegRenderer
    from porter.core.media.ffmpeg import FFmpegRunner, FFmpegTools
    from porter.core.pipeline import Pipeline
    from porter.core.platforms.downloader import PlatformDownloader
    from porter.core.platforms.local import LocalFileDownloader

    tools = FFmpegTools.resolve(
        ctx.config.ffmpeg.ffmpeg_path,
        ctx.config.ffmpeg.ffprobe_path,
    )
    return Pipeline(
        downloader=PlatformDownloader(),
        transcriber=_build_transcriber(plan),
        translator=_build_translator(plan),
        renderer=FfmpegRenderer(
            runner=FFmpegRunner(tools),
            config=ctx.config.ffmpeg,
        ),
        local=LocalFileDownloader(),
    )


def _build_transcriber(plan: WorkflowPlan) -> AsrChain:
    stage = plan.stage(Stage.TRANSCRIPT)
    if stage is None:
        return AsrChain(
            [],
            allow_platform=False,
            allow_supplied=False,
            allow_asr=False,
        )

    capability = stage.capability
    if capability == TRANSCRIPT_PLATFORM:
        return AsrChain(
            [],
            allow_platform=True,
            allow_supplied=False,
            allow_asr=False,
            expected_used_asr=False,
            expected_origin="platform",
        )
    if capability == TRANSCRIPT_SUPPLIED:
        return AsrChain(
            [],
            allow_platform=False,
            allow_supplied=True,
            allow_asr=False,
            expected_used_asr=False,
            expected_origin="supplied",
        )

    backend = _asr_backend(capability)
    prefix = "faster-whisper:" if capability == TRANSCRIPT_WHISPER_LOCAL else "whisper:"
    return AsrChain(
        [backend],
        allow_platform=False,
        allow_supplied=False,
        allow_asr=True,
        expected_used_asr=True,
        expected_origin_prefix=prefix,
    )


def _asr_backend(capability: str) -> Any:
    if capability == TRANSCRIPT_WHISPER_LOCAL:
        from porter.core.asr.whisper_local import WhisperLocalBackend

        return WhisperLocalBackend()
    if capability == TRANSCRIPT_WHISPER_API:
        from porter.core.asr.whisper_api import WhisperApiBackend

        return WhisperApiBackend()
    raise PorterError(
        f"the plan selected an ASR capability this build cannot execute: {capability}",
        capability=capability,
    )


def _build_translator(plan: WorkflowPlan) -> Any:
    stage = plan.stage(Stage.TRANSLATE)
    if stage is None:
        from porter.core.refine.passthrough import PassthroughRefiner
        from porter.core.translate.chain import TranslationChain

        return TranslationChain(backends=[], refiner=PassthroughRefiner())
    return build_translation_chain(stage.capability)


def build_translation_chain(capability: str) -> Any:
    """A translation chain containing exactly ``capability``.

    Public because the artifact-level ``porter_translate`` tool needs the same
    one-capability guarantee as the executor. A chain built from config would
    reintroduce the fallback list the plan model removed.
    """
    from porter.core.refine.passthrough import PassthroughRefiner
    from porter.core.translate.chain import TranslationChain

    return TranslationChain(
        backends=[_translation_backend(capability)],
        refiner=PassthroughRefiner(),
    )


def _translation_backend(capability: str) -> Any:
    if capability == TRANSLATE_BING:
        from porter.core.translate.bing import BingTranslateBackend

        return BingTranslateBackend()
    if capability == TRANSLATE_GOOGLE:
        from porter.core.translate.google import GoogleTranslateBackend

        return GoogleTranslateBackend()
    if capability == TRANSLATE_MYMEMORY:
        from porter.core.translate.mymemory import MyMemoryBackend

        return MyMemoryBackend()
    if capability == TRANSLATE_LLM:
        from porter.core.translate.llm import LLMTranslationBackend

        return LLMTranslationBackend()
    raise PorterError(
        f"the plan selected a translation capability this build cannot execute: {capability}",
        capability=capability,
    )


# ----------------------------------------------------------------------
# Plan -> execution options
# ----------------------------------------------------------------------


def _job_options(plan: WorkflowPlan) -> JobOptions:
    render = plan.stage(Stage.RENDER)
    transcript = plan.stage(Stage.TRANSCRIPT)
    acquire = plan.stage(Stage.ACQUIRE)
    translate = plan.stage(Stage.TRANSLATE)

    burn = BurnMode.SKIP
    if render is not None:
        burn = BurnMode(str(render.params.get("mode") or BurnMode.SKIP.value))

    subtitle_file: Path | None = None
    if (
        transcript is not None
        and transcript.capability == TRANSCRIPT_SUPPLIED
        and transcript.params.get("path")
    ):
        subtitle_file = Path(str(transcript.params["path"]))

    cookies_file = None
    cookies_browser = None
    if acquire is not None:
        raw_cookies = acquire.params.get("cookies_file")
        cookies_file = Path(str(raw_cookies)) if raw_cookies else None
        cookies_browser = (
            str(acquire.params["cookies_browser"])
            if acquire.params.get("cookies_browser")
            else None
        )

    llm_model = None
    if translate is not None and translate.params.get("llm_model"):
        llm_model = str(translate.params["llm_model"])

    return JobOptions(
        output_dir=Path(plan.runtime.output_dir),
        burn=burn,
        # The selected capability is part of the translation cache key, so a new
        # plan that chooses a different backend cannot reuse the previous
        # backend's text. This is the cache-level counterpart of "no
        # cross-capability substitution".
        translator=translate.capability if translate is not None else None,
        target_lang=plan.runtime.target_lang,
        llm_model=llm_model,
        cookies_file=cookies_file,
        cookies_browser=cookies_browser,
        subtitle_file=subtitle_file,
        audio_denoise=plan.runtime.audio_denoise,
        force=plan.runtime.force,
        # No refinement: it is a separate paid capability, and the plan did not
        # select one. A hidden LLM call would be exactly the implicit spending
        # the resolver is designed to avoid.
        refine=False,
    )


def _request(plan: WorkflowPlan) -> JobRequest:
    options = _job_options(plan)
    if plan.source_probe.kind is SourceKind.URL:
        return JobRequest(url=plan.source, options=options)
    return JobRequest.from_source(plan.source, options=options)


# ----------------------------------------------------------------------
# Stage grouping and results
# ----------------------------------------------------------------------


def _phase_groups(stages: tuple[StagePlan, ...]) -> list[tuple[Phase | None, list[StagePlan]]]:
    """Group consecutive stages by the event phase they belong to."""
    groups: list[tuple[Phase | None, list[StagePlan]]] = []
    for stage in stages:
        phase = _PHASE_FOR_STAGE.get(stage.stage)
        if groups and groups[-1][0] is phase:
            groups[-1][1].append(stage)
        else:
            groups.append((phase, [stage]))
    return groups


def _stage_result(
    stage: StagePlan,
    status: Literal["done", "reused", "skipped", "failed"],
    duration: float,
    outputs: dict[str, str],
    *,
    error: StageError | None = None,
) -> StageResult:
    return StageResult(
        id=stage.id,
        stage=stage.stage,
        capability=stage.capability,
        status=status,
        params=dict(stage.params),
        inputs={},
        outputs=outputs,
        error=error,
        duration_seconds=duration,
    )


def _outputs_for(
    stage: StagePlan,
    *,
    raw: RawMaterials | None = None,
    subtitles: SubtitleSet | None = None,
    burn: BurnResult | None = None,
) -> dict[str, str]:
    if stage.stage is Stage.ACQUIRE and raw is not None:
        outputs: dict[str, str] = {}
        if raw.video.is_file():
            outputs["source_media"] = str(raw.video)
        elif raw.audio.is_file():
            outputs["source_media"] = str(raw.audio)
        if raw.subtitle_src is not None:
            outputs["platform_subtitles"] = str(raw.subtitle_src)
        return outputs
    if stage.stage is Stage.NORMALIZE and raw is not None:
        outputs = {}
        if raw.video.is_file():
            outputs["master_video"] = str(raw.video)
        if raw.audio.is_file():
            outputs["master_audio"] = str(raw.audio)
        if raw.audio_enhanced is not None:
            outputs["audio_enhanced"] = str(raw.audio_enhanced)
        return outputs
    if stage.stage is Stage.TRANSCRIPT and raw is not None:
        source = raw.layout.cooked_dir / "subtitle.srt"
        return {"source_transcript": str(source)} if source.is_file() else {}
    if stage.stage is Stage.TRANSLATE and subtitles is not None:
        outputs = {}
        if subtitles.transcript_json_path.is_file():
            outputs["transcript_json"] = str(subtitles.transcript_json_path)
        return outputs
    if stage.stage is Stage.WRITE and subtitles is not None:
        return _subtitle_outputs(subtitles)
    if stage.stage is Stage.RENDER and burn is not None:
        outputs = {}
        if burn.video_bilingual is not None:
            outputs["video_bilingual"] = str(burn.video_bilingual)
        if burn.video_zh is not None:
            outputs["video_zh"] = str(burn.video_zh)
        return outputs
    return {}


def _subtitle_outputs(subtitles: SubtitleSet) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for key, path in (
        ("subtitle_bilingual_srt", subtitles.subtitle_bilingual_srt),
        ("subtitle_bilingual_ass", subtitles.subtitle_bilingual_ass),
        ("subtitle_zh_srt", subtitles.subtitle_zh_srt),
        ("subtitle_zh_ass", subtitles.subtitle_zh_ass),
        ("transcript_json", subtitles.transcript_json_path),
        ("transcript_txt", subtitles.transcript_txt_path),
    ):
        if path.is_file():
            outputs[key] = str(path)
    return outputs


# ----------------------------------------------------------------------
# Results and errors
# ----------------------------------------------------------------------


def _workflow_result(
    plan: WorkflowPlan,
    ctx: RunContext,
    state: JobState,
    started: float,
    stages: list[StageResult],
    raw: RawMaterials | None,
    subtitles: SubtitleSet | None,
    burn: BurnResult | None,
    *,
    task_dir: Path | None = None,
    error: StageError | None = None,
) -> WorkflowResult:
    artifacts: dict[str, str] = {}
    for stage in stages:
        artifacts.update(stage.outputs)
    if raw is not None:
        if raw.video.is_file():
            artifacts["video"] = str(raw.video)
        if raw.audio.is_file():
            artifacts["audio"] = str(raw.audio)
        if raw.audio_enhanced is not None and raw.audio_enhanced.is_file():
            artifacts["audio_enhanced"] = str(raw.audio_enhanced)
        if raw.cover is not None and raw.cover.is_file():
            artifacts["cover"] = str(raw.cover)
        if raw.metadata_path is not None and raw.metadata_path.is_file():
            artifacts["metadata"] = str(raw.metadata_path)
    if subtitles is not None:
        artifacts.update(_subtitle_outputs(subtitles))
    if burn is not None:
        artifacts.update(_outputs_for(plan.stage(Stage.RENDER) or _dummy_stage(), burn=burn))

    return WorkflowResult(
        job_id=ctx.job_id,
        state=state,
        plan_id=plan.plan_id,
        workflow=plan.workflow,
        task=plan.task,
        task_dir=str(task_dir or (raw.layout.task_dir if raw is not None else "")) or None,
        stages=tuple(stages),
        artifacts=artifacts,
        error=error,
        duration_seconds=time.monotonic() - started,
    )


def _job_result_from(
    workflow: WorkflowResult,
    raw: RawMaterials | None,
    subtitles: SubtitleSet | None,
    burn: BurnResult | None,
    *,
    task_dir: Path | None = None,
) -> JobResult:
    error = None
    if workflow.error is not None:
        error = ErrorInfo(
            code=workflow.error.code,
            message=workflow.error.message,
            details=workflow.error.details,
        )
    return JobResult(
        job_id=workflow.job_id,
        state=workflow.state,
        task_dir=task_dir or (raw.layout.task_dir if raw is not None else None),
        raw=raw,
        subtitles=subtitles,
        burn=burn,
        error=error,
        duration_seconds=workflow.duration_seconds,
    )


def _failure_outcome(
    plan: WorkflowPlan,
    ctx: RunContext,
    started: float,
    error: StageError,
    raw: RawMaterials | None = None,
    subtitles: SubtitleSet | None = None,
    burn: BurnResult | None = None,
    stages: list[StageResult] | None = None,
) -> ExecutionOutcome:
    workflow = _workflow_result(
        plan,
        ctx,
        JobState.FAILED,
        started,
        stages or [],
        raw,
        subtitles,
        burn,
        error=error,
    )
    return ExecutionOutcome(
        workflow=workflow,
        job=_job_result_from(workflow, raw, subtitles, burn),
    )


def _cancelled_outcome(
    plan: WorkflowPlan,
    ctx: RunContext,
    started: float,
    stages: list[StageResult],
    raw: RawMaterials | None,
    subtitles: SubtitleSet | None,
    burn: BurnResult | None,
) -> ExecutionOutcome:
    workflow = _workflow_result(
        plan,
        ctx,
        JobState.CANCELLED,
        started,
        stages,
        raw,
        subtitles,
        burn,
    )
    return ExecutionOutcome(
        workflow=workflow,
        job=_job_result_from(workflow, raw, subtitles, burn),
    )


def _error_for(
    plan: WorkflowPlan,
    stage: StagePlan | None,
    exc: PorterError,
    ctx: RunContext | None = None,
) -> StageError:
    issue: PlanIssue | None = None
    if stage is not None:
        issue = next(
            (
                candidate
                for candidate in plan.issues
                if candidate.stage is stage.stage and candidate.severity == "blocker"
            ),
            None,
        )

    alternatives: tuple[str, ...] = issue.alternatives if issue is not None else ()
    suggested_workflow: WorkflowId | None = (
        issue.suggested_workflow if issue is not None else None
    )

    if stage is not None and not alternatives and ctx is not None:
        alternatives, inferred_workflow = _runtime_alternatives(plan, stage, ctx)
        if suggested_workflow is None:
            suggested_workflow = inferred_workflow

    return StageError(
        code=exc.code,
        message=exc.message,
        retryable=False,
        alternatives=alternatives,
        suggested_workflow=suggested_workflow,
        details=dict(exc.details),
    )


def _runtime_alternatives(
    plan: WorkflowPlan,
    stage: StagePlan,
    ctx: RunContext,
) -> tuple[tuple[str, ...], WorkflowId | None]:
    """Suggest peer capabilities / replanning workflow after a runtime failure."""
    from porter.core.workflow.capabilities import capability_catalog

    try:
        catalog = capability_catalog(ctx)
    except Exception:  # noqa: BLE001 - error enrichment must never mask the real failure
        return (), None

    if stage.stage is Stage.TRANSCRIPT:
        if stage.capability == TRANSCRIPT_PLATFORM:
            peers = tuple(
                cap.id
                for cap in catalog
                if cap.stage is Stage.TRANSCRIPT
                and cap.id in (TRANSCRIPT_WHISPER_LOCAL, TRANSCRIPT_WHISPER_API)
                and cap.available
            )
            suggested = (
                WorkflowId.URL_ASR
                if plan.workflow is WorkflowId.URL_SUBTITLED
                else WorkflowId.LOCAL_ASR
            )
            return peers, suggested
        if stage.capability in (TRANSCRIPT_WHISPER_LOCAL, TRANSCRIPT_WHISPER_API):
            peers = tuple(
                cap.id
                for cap in catalog
                if cap.stage is Stage.TRANSCRIPT
                and cap.id != stage.capability
                and cap.id in (TRANSCRIPT_WHISPER_LOCAL, TRANSCRIPT_WHISPER_API)
                and cap.available
            )
            return peers, None

    if stage.stage is Stage.TRANSLATE:
        peers = tuple(
            cap.id
            for cap in catalog
            if cap.stage is Stage.TRANSLATE
            and cap.id != stage.capability
            and cap.available
        )
        return peers, None

    return (), None


def _error_from_issue(issue: PlanIssue) -> StageError:
    return StageError(
        code=issue.code,
        message=issue.message,
        retryable=False,
        alternatives=issue.alternatives,
        suggested_workflow=issue.suggested_workflow,
        details={"severity": issue.severity},
    )


def _dummy_stage() -> StagePlan:
    """A render stage used only to label burn outputs on a completed plan.

    Every plan that can reach BURN has a render stage; this exists so the result
    builder does not need to assert that, which would be an assertion in a code
    path that must not fail after a successful render.
    """
    return StagePlan(
        id="render",
        stage=Stage.RENDER,
        capability="render.ffmpeg-libass",
    )
