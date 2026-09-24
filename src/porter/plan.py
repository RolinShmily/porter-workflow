"""Build the resolved execution plan for a source.

The builder behind the MCP ``porter_plan`` tool. It answers "what would this job
do, and will it work?" without downloading media.

## Derived, never authored

The plan is read off the objects that would do the work:

* ``Pipeline.default(ctx)`` builds the same transcriber, translator and renderer
  the run would use -- the *same constructor*, not a parallel copy of its
  assembly logic.
* ``phases_for(request)`` is the method the run itself iterates.
* ``YtDlpExtractor.plan_subtitles(info)`` is the method PREPARE calls to decide
  which tracks to fetch.

Nothing here re-derives an ordering or a selection rule. That matters more than
it looks: a plan that describes a pipeline nobody runs is worse than no plan,
because an agent will act on it.

## Why there is no time estimate

A time estimate is deliberately absent, and the honest answer is that this module
cannot produce one. A defensible number needs a measured
encode rate for *this* machine at *this* resolution with *this* encoder, and the
only thing that knows it is a trial encode (``porter doctor`` runs those). So the
plan reports the measured inputs that drive the cost -- duration, resolution,
which phases run -- and says what determines the rest. A fabricated "about 12
minutes" would be acted on, and would be wrong on the first unusual video.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from porter.context import RunContext
from porter.models.plan import BackendPlan, Plan, SubtitlePlan, TranslationPlan
from porter.models.request import JobOptions, JobRequest

__all__ = ["plan_for"]

#: Reported as the acquisition component for URL sources. Named after the
#: mechanism, not the class: the class name is an implementation detail and an
#: agent reading the plan has no use for it.
ACQUISITION_YDLP = "yt-dlp"


def plan_for(
    source: str,
    options: JobOptions | None = None,
    ctx: RunContext | None = None,
) -> Plan:
    """Describe the job ``source`` would produce.

    Never raises for an unusable source: like :func:`inspect_url`, a bad link is a
    *result*. Genuine faults (a missing dependency, a cancelled job) propagate.

    Args:
        source: A link or a path to a local file. Classified by the same rule the
            CLI and the job tools use.
        options: Per-invocation options. Defaults are used when omitted, so an
            agent can ask about a source before deciding the flags.
        ctx: Run context. Built from the resolved config when omitted.

    Returns:
        A :class:`~porter.models.plan.Plan`. Check ``feasible`` before starting.
    """
    from porter.config import resolve
    from porter.pipeline import Pipeline

    # An explicit ``options`` wins over the ones already on ``ctx``, mirroring the
    # pipeline's own rule that the request is authoritative. It did not, and the
    # argument was silently ignored whenever a context was passed -- the same
    # two-options-objects trap the pipeline had, reintroduced one layer up. A
    # caller asking about ``--burn skip`` would have been told about a full run.
    if options is not None:
        if ctx is None:
            config = resolve(None)
            ctx = RunContext(job_id="plan", options=options, config=config)
        else:
            ctx.options = options
    elif ctx is None:
        config = resolve(None)
        ctx = RunContext(
            job_id="plan",
            options=JobOptions(output_dir=config.output_dir),
            config=config,
        )

    request = JobRequest.from_source(source, ctx.options)

    # The pipeline that would run, from the constructor that would build it.
    pipeline = Pipeline.default(ctx)
    phases = [phase.value for phase in pipeline.phases_for(request)]
    asr = _backend_plans(pipeline.transcriber, ctx)
    translators = _backend_plans(pipeline.translator, ctx)

    if request.local_video is not None:
        return _local_plan(source, request.local_video, request, ctx, phases, asr, translators)
    return _url_plan(source, request, ctx, phases, asr, translators)


# ----------------------------------------------------------------------
# Local files
# ----------------------------------------------------------------------


def _local_plan(
    source: str,
    path: Path,
    request: JobRequest,
    ctx: RunContext,
    phases: list[str],
    asr: list[BackendPlan],
    translators: list[BackendPlan],
) -> Plan:
    """A local file has no platform track, so ASR always runs.

    Worth stating plainly rather than discovering at minute thirty: sidecar
    ``.srt`` files next to the video are **not** picked up, so a local file with
    a perfect subtitle sitting beside it still pays for recognition.
    """
    plan = _base_plan(
        source=source,
        kind="local",
        platform="local",
        phases=phases,
        acquisition="local",
        subtitles=SubtitlePlan(route="asr", asr_runs=True, asr_backends=asr),
        translation=_translation_plan(ctx, translators, reused_chinese=False),
        request=request,
        notes=[
            "a local file has no platform subtitle track, so speech recognition always runs",
            "a sidecar .srt next to the video is not picked up",
        ],
    )
    plan.notes.extend(_asr_warnings(asr))

    # A missing file is the most trivially doomed job there is, and it was the
    # one case this function called feasible: the plan described a pipeline that
    # would fail in its first second. Checking here rather than in the frontends
    # keeps the CLI and the MCP tool honest by construction.
    if not path.is_file():
        return _apply_blocking(plan, [f"no such file: {path}"])

    return _apply_blocking(plan, _asr_blocking(asr))


# ----------------------------------------------------------------------
# URLs
# ----------------------------------------------------------------------


def _url_plan(
    source: str,
    request: JobRequest,
    ctx: RunContext,
    phases: list[str],
    asr: list[BackendPlan],
    translators: list[BackendPlan],
) -> Plan:
    """Inspect the link, then read the route off the platform's own logic."""
    from porter.platforms.base import YtDlpExtractor
    from porter.platforms.inspector import inspect_url
    from porter.platforms.registry import registry

    result = inspect_url(source, ctx)
    if not result.is_valid:
        plan = _base_plan(
            source=source,
            kind="url",
            platform=result.platform,
            phases=phases,
            acquisition=ACQUISITION_YDLP,
            subtitles=SubtitlePlan(route="unknown", asr_runs=True, asr_backends=asr),
            translation=_translation_plan(ctx, translators, reused_chinese=False),
            request=request,
            notes=["the link could not be inspected, so no route could be resolved"],
        )
        return _apply_blocking(plan, [result.error_message or "the link is not usable"])

    handler = registry().find_or_none(result.canonical_url)
    tracks: dict[str, str] = {}
    if isinstance(handler, YtDlpExtractor):
        # The platform's own selection rules, applied to the real info dict.
        tracks = {
            name: lang for name, (lang, _is_auto) in handler.plan_subtitles(result.raw_info).items()
        }

    route = "platform" if tracks else "asr"
    reused_chinese = _reused_chinese(tracks)
    translation = _translation_plan(ctx, translators, reused_chinese=reused_chinese)

    notes: list[str] = []
    blocking: list[str] = []
    if route == "platform":
        notes.append(
            "the platform's own track is used as the source transcript: correct "
            "spelling and punctuation, and no recognition cost. Requested, not "
            "guaranteed -- a caption fetch that fails falls back to speech "
            "recognition, and platforms rate-limit those fetches (HTTP 429)."
        )
        if reused_chinese:
            notes.append(
                "the platform also provides a Chinese track, so translation is "
                "skipped entirely -- if that track is fetched. If it is not, the "
                "translation chain runs instead."
            )
    else:
        notes.append(
            "the platform provides no usable subtitle track for this link, so "
            "speech recognition runs"
        )
        blocking.extend(_asr_blocking(asr))
        notes.extend(_asr_warnings(asr))

    if translation.needed and not any(backend.available for backend in translators):
        blocking.append(
            "no translation backend is available, and the platform provides no "
            "Chinese track: TRANSLATE will fail. Configure an LLM API key, or check "
            "which key-free backends this build can reach (porter doctor)."
        )

    plan = _base_plan(
        source=source,
        kind="url",
        platform=result.platform,
        phases=phases,
        acquisition=ACQUISITION_YDLP,
        subtitles=SubtitlePlan(
            route=route,
            tracks=tracks,
            asr_runs=route == "asr",
            asr_backends=asr,
        ),
        translation=translation,
        request=request,
        notes=notes,
        duration_seconds=result.duration_seconds,
        width=result.width,
        height=result.height,
        is_vertical=result.is_vertical if result.width and result.height else None,
    )
    return _apply_blocking(plan, blocking)


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------


def _base_plan(
    *,
    source: str,
    kind: str,
    platform: str | None,
    phases: list[str],
    acquisition: str,
    subtitles: SubtitlePlan,
    translation: TranslationPlan,
    request: JobRequest,
    notes: list[str],
    duration_seconds: float | None = None,
    width: int | None = None,
    height: int | None = None,
    is_vertical: bool | None = None,
) -> Plan:
    """The parts every plan shares, so the branches only supply what differs."""
    return Plan(
        source=source,
        kind=kind,
        platform=platform,
        phases=phases,
        acquisition=acquisition,
        subtitles=subtitles,
        translation=translation,
        burn_mode=request.options.burn.value,
        burn_runs=request.options.burn.value != "skip",
        renderer="ffmpeg",
        duration_seconds=duration_seconds,
        width=width,
        height=height,
        is_vertical=is_vertical,
        notes=notes,
    )


def _apply_blocking(plan: Plan, blocking: list[str]) -> Plan:
    """Mark a plan infeasible when a phase is certain to fail."""
    if not blocking:
        return plan
    plan.feasible = False
    plan.blocking_issues = blocking
    return plan


def _backend_plans(chain: Any, ctx: RunContext) -> list[BackendPlan]:
    """Read a chain's ordered backends, their probe results and their provenance.

    Uses the chain's own ``availability``, so the report and the pipeline share
    one ordering and one availability rule.
    """
    availability = getattr(chain, "availability", None)
    if availability is None:  # pragma: no cover - both chains define it
        return []
    verified = {
        backend.name: getattr(backend, "endpoint_verified", False)
        for backend in getattr(chain, "backends", [])
    }
    return [
        BackendPlan(name=name, available=ok, verified=verified.get(name, False))
        for name, ok in availability(ctx)
    ]


def _asr_blocking(asr: list[BackendPlan]) -> list[str]:
    """The message for "nothing can produce a source transcript"."""
    if any(backend.available for backend in asr):
        return []
    names = ", ".join(backend.name for backend in asr) or "none"
    return [
        "no speech recognition engine is available, and no platform subtitle track "
        f"exists for this source: TRANSCRIBE will fail. Tried: {names}. Set an "
        "OpenAI-compatible API key, or install the videocaptioner CLI "
        "(porter doctor reports what is missing)."
    ]


def _asr_warnings(asr: list[BackendPlan]) -> list[str]:
    """Warn when recognition rests only on engines nobody has verified.

    ``available`` alone would call this plan feasible, and it would be wrong in
    practice: the two key-free backends probe as available and their endpoints
    answered with empty results on every probe made while building this port. A
    warning rather than a blocking issue, because "unverified" is not "broken" --
    they may work from another network, or again next week.
    """
    if any(backend.available and backend.verified for backend in asr):
        return []
    usable = [backend.name for backend in asr if backend.available]
    if not usable:
        return []
    return [
        "speech recognition will be attempted only with unverified endpoints "
        f"({', '.join(usable)}). They probe as available, but both returned empty "
        "results for every audio segment when this build was tested, so expect "
        "TRANSCRIBE to fail. Set an OpenAI-compatible API key for a verified "
        "engine, or install the videocaptioner CLI."
    ]


def _translation_plan(
    ctx: RunContext, translators: list[BackendPlan], *, reused_chinese: bool
) -> TranslationPlan:
    """Whether translation runs, and with which engines."""
    return TranslationPlan(
        target_lang=ctx.options.target_lang,
        needed=not reused_chinese,
        backends=translators,
    )


def _reused_chinese(tracks: dict[str, str]) -> bool:
    """Whether a platform Chinese track will be reused instead of translated.

    Keyed on the filename PREPARE writes for that track. The ASR chain aligns that
    file into the cues as target text, and TRANSLATE then finds every cue already
    translated and skips all backends.
    """
    from porter.platforms.base import SUBTITLE_ZH_NAME

    return SUBTITLE_ZH_NAME in tracks
