"""The capability catalog: what porter can do, stage by stage.

Each capability is a *peer*: one implementation of one stage. There is no order
here that means "try these in turn". The resolver picks exactly one, and the
executor runs exactly that one. A capability that is unavailable is reported as
unavailable, with its alternatives named, and planning stops; it is not quietly
replaced by the next entry in a list.

The catalog is also the honest place to answer "what will this cost?". A
capability records whether it is free, needs the network, or spends money, and
whether audio or text leaves the machine. The default policy refuses ``PAID``
unless the caller names it or passes ``--allow-paid``: an API key in the
environment is a configuration, not consent.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from porter.core.context import RunContext
from porter.core.logging import get_logger
from porter.core.workflow.model import (
    CapabilityDescriptor,
    Cost,
    Egress,
    Stage,
)

__all__ = [
    "CAPABILITY_ALIASES",
    "CAPABILITY_IDS",
    "capability_catalog",
    "capability_for",
    "default_candidates",
    "normalize_capability",
    "selection_problem",
]

_logger = get_logger(__name__)

# -- ids -----------------------------------------------------------------

PROBE_YTDLP = "probe.ytdlp"
PROBE_FFPROBE = "probe.ffprobe"
PROBE_SUBTITLE = "probe.subtitle"

ACQUIRE_YTDLP_VIDEO = "acquire.ytdlp-video"
ACQUIRE_YTDLP_AUDIO = "acquire.ytdlp-audio"
ACQUIRE_YTDLP_SUBS = "acquire.ytdlp-subs"
ACQUIRE_LOCAL = "acquire.local"

NORMALIZE_FFMPEG = "normalize.ffmpeg"

TRANSCRIPT_PLATFORM = "transcript.platform-track"
TRANSCRIPT_SUPPLIED = "transcript.supplied"
TRANSCRIPT_WHISPER_LOCAL = "transcript.whisper-local"
TRANSCRIPT_WHISPER_API = "transcript.whisper-api"

TRANSLATE_BING = "translate.bing"
TRANSLATE_GOOGLE = "translate.google"
TRANSLATE_MYMEMORY = "translate.mymemory"
TRANSLATE_LLM = "translate.llm"

WRITE_SUBTITLES = "write.subtitles"
RENDER_FFMPEG = "render.ffmpeg-libass"

CAPABILITY_IDS = (
    PROBE_YTDLP,
    PROBE_FFPROBE,
    PROBE_SUBTITLE,
    ACQUIRE_YTDLP_VIDEO,
    ACQUIRE_YTDLP_AUDIO,
    ACQUIRE_YTDLP_SUBS,
    ACQUIRE_LOCAL,
    NORMALIZE_FFMPEG,
    TRANSCRIPT_PLATFORM,
    TRANSCRIPT_SUPPLIED,
    TRANSCRIPT_WHISPER_LOCAL,
    TRANSCRIPT_WHISPER_API,
    TRANSLATE_BING,
    TRANSLATE_GOOGLE,
    TRANSLATE_MYMEMORY,
    TRANSLATE_LLM,
    WRITE_SUBTITLES,
    RENDER_FFMPEG,
)

#: Names accepted from the old ``--asr-engine`` / ``--translator`` flags and from
#: configuration. A bare name is a convenience, never a second catalog: it maps
#: onto the same ids above and then goes through the same availability check.
CAPABILITY_ALIASES: dict[str, str] = {
    "platform": TRANSCRIPT_PLATFORM,
    "platform-track": TRANSCRIPT_PLATFORM,
    "supplied": TRANSCRIPT_SUPPLIED,
    "whisper-local": TRANSCRIPT_WHISPER_LOCAL,
    "whisper-api": TRANSCRIPT_WHISPER_API,
    "bing": TRANSLATE_BING,
    "google": TRANSLATE_GOOGLE,
    "mymemory": TRANSLATE_MYMEMORY,
    "llm": TRANSLATE_LLM,
    "ffmpeg": RENDER_FFMPEG,
    "ffmpeg-libass": RENDER_FFMPEG,
}

#: Which capabilities a stage may choose from, in the order a free-first
#: resolver prefers. Paid entries are last here and are filtered out unless the
#: caller opted in.
_DEFAULT_CANDIDATES: dict[Stage, tuple[str, ...]] = {
    Stage.PROBE: (PROBE_FFPROBE, PROBE_YTDLP, PROBE_SUBTITLE),
    Stage.ACQUIRE: (ACQUIRE_LOCAL, ACQUIRE_YTDLP_VIDEO, ACQUIRE_YTDLP_AUDIO, ACQUIRE_YTDLP_SUBS),
    Stage.NORMALIZE: (NORMALIZE_FFMPEG,),
    Stage.TRANSCRIPT: (
        TRANSCRIPT_PLATFORM,
        TRANSCRIPT_SUPPLIED,
        TRANSCRIPT_WHISPER_LOCAL,
        TRANSCRIPT_WHISPER_API,
    ),
    Stage.TRANSLATE: (
        TRANSLATE_BING,
        TRANSLATE_GOOGLE,
        TRANSLATE_MYMEMORY,
        TRANSLATE_LLM,
    ),
    Stage.WRITE: (WRITE_SUBTITLES,),
    Stage.RENDER: (RENDER_FFMPEG,),
}

#: Approximate first-run download sizes, in MiB. A cached model reports 0. The
#: values are faster-whisper's published size names, not measurements of the
#: exact bytes this machine receives.
_MODEL_DOWNLOAD_MB: dict[str, int] = {
    "tiny": 75,
    "tiny.en": 75,
    "base": 145,
    "base.en": 145,
    "small": 484,
    "small.en": 484,
    "medium": 1530,
    "medium.en": 1530,
    "large": 3090,
    "large-v1": 3090,
    "large-v2": 3090,
    "large-v3": 3090,
    "distil-large-v3": 1510,
}


@dataclass(frozen=True)
class _Probe:
    available: bool
    verified: bool = True
    download_mb: int | None = None


def normalize_capability(name: str, stage: Stage | None = None) -> str:
    """Map a user-facing name onto a capability id.

    ``whisper-local`` becomes ``transcript.whisper-local``; an already-qualified
    id is returned unchanged. When ``stage`` is known and the name has no dot,
    the stage prefix is applied, so a future stage can reuse a short name without
    a second alias table.
    """
    cleaned = name.strip()
    if cleaned in CAPABILITY_ALIASES:
        return CAPABILITY_ALIASES[cleaned]
    if "." in cleaned:
        return cleaned
    if stage is not None:
        return f"{stage.value}.{cleaned}"
    return cleaned


def default_candidates(stage: Stage) -> tuple[str, ...]:
    """The neutral candidate order for ``stage``.

    A resolver narrows this by workflow (a subtitled workflow has no ASR
    candidates, an ASR workflow has no platform-track candidate) before asking
    for availability. Keeping the broad order here means there is one place to
    add a capability, not one per workflow.
    """
    return _DEFAULT_CANDIDATES[stage]


def _probe_ffmpeg(ctx: RunContext) -> _Probe:
    try:
        from porter.core.media.ffmpeg import FFmpegTools

        tools = FFmpegTools.resolve(
            ctx.config.ffmpeg.ffmpeg_path,
            ctx.config.ffmpeg.ffprobe_path,
        )
        return _Probe(available=not tools.missing())
    except Exception:  # a probe must never break the catalog
        _logger.debug("ffmpeg probe raised", exc_info=True)
        return _Probe(available=False)


def _probe_ytdlp() -> _Probe:
    try:
        return _Probe(available=importlib.util.find_spec("yt_dlp") is not None)
    except Exception:
        _logger.debug("yt-dlp probe raised", exc_info=True)
        return _Probe(available=False)


def _probe_whisper_local(ctx: RunContext) -> _Probe:
    try:
        from porter.core.asr.whisper_local import (
            WhisperLocalBackend,
            model_is_cached,
        )

        if not WhisperLocalBackend().available(ctx):
            return _Probe(available=False)
        model = ctx.config.asr.whisper_local_model
        cached = model_is_cached(model)
        size = 0 if cached else _MODEL_DOWNLOAD_MB.get(model, 500)
        return _Probe(available=True, download_mb=size)
    except Exception:
        _logger.debug("local Whisper probe raised", exc_info=True)
        return _Probe(available=False)


def _probe_whisper_api(ctx: RunContext) -> _Probe:
    try:
        from porter.core.asr.whisper_api import WhisperApiBackend

        return _Probe(available=WhisperApiBackend().available(ctx))
    except Exception:
        _logger.debug("Whisper API probe raised", exc_info=True)
        return _Probe(available=False)


def _probe_translation(ctx: RunContext, capability: str) -> _Probe:
    try:
        backend: object
        if capability == TRANSLATE_LLM:
            from porter.core.translate.llm import LLMTranslationBackend

            backend = LLMTranslationBackend()
        elif capability == TRANSLATE_BING:
            from porter.core.translate.bing import BingTranslateBackend

            backend = BingTranslateBackend()
        elif capability == TRANSLATE_GOOGLE:
            from porter.core.translate.google import GoogleTranslateBackend

            backend = GoogleTranslateBackend()
        elif capability == TRANSLATE_MYMEMORY:
            from porter.core.translate.mymemory import MyMemoryBackend

            backend = MyMemoryBackend()
        else:  # pragma: no cover - guarded by the caller
            return _Probe(available=False)

        available = backend.available(ctx)
        return _Probe(
            available=bool(available),
            verified=bool(getattr(backend, "endpoint_verified", False)),
        )
    except Exception:
        _logger.debug("%s probe raised", capability, exc_info=True)
        return _Probe(available=False)


def _describe(ctx: RunContext, capability: str) -> CapabilityDescriptor:
    """Build one descriptor, probing only the capability's own dependencies."""
    ffmpeg = _probe_ffmpeg(ctx)
    ytdlp = _probe_ytdlp()

    if capability == PROBE_YTDLP:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.PROBE,
            available=ytdlp.available,
            verified=ytdlp.verified,
            requires=("yt-dlp",),
            cost=Cost.NETWORK,
            description="Read platform metadata without downloading media.",
        )
    if capability == PROBE_FFPROBE:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.PROBE,
            available=ffmpeg.available,
            verified=ffmpeg.verified,
            requires=("ffprobe",),
            description="Read local media facts with ffprobe.",
        )
    if capability == PROBE_SUBTITLE:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.PROBE,
            available=True,
            description="Parse a supplied subtitle file.",
        )
    if capability == ACQUIRE_YTDLP_VIDEO:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.ACQUIRE,
            available=ytdlp.available and ffmpeg.available,
            verified=ytdlp.verified and ffmpeg.verified,
            requires=("yt-dlp", "ffmpeg"),
            cost=Cost.NETWORK,
            description="Download the source video and audio with yt-dlp.",
        )
    if capability == ACQUIRE_YTDLP_AUDIO:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.ACQUIRE,
            available=ytdlp.available and ffmpeg.available,
            verified=ytdlp.verified and ffmpeg.verified,
            requires=("yt-dlp", "ffmpeg"),
            cost=Cost.NETWORK,
            description="Download only the audio stream with yt-dlp.",
        )
    if capability == ACQUIRE_YTDLP_SUBS:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.ACQUIRE,
            available=ytdlp.available,
            verified=ytdlp.verified,
            requires=("yt-dlp",),
            cost=Cost.NETWORK,
            description="Fetch the platform's subtitle track without media.",
        )
    if capability == ACQUIRE_LOCAL:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.ACQUIRE,
            available=ffmpeg.available,
            verified=ffmpeg.verified,
            requires=("ffmpeg",),
            description="Use a file already on this machine.",
        )
    if capability == NORMALIZE_FFMPEG:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.NORMALIZE,
            available=ffmpeg.available,
            verified=ffmpeg.verified,
            requires=("ffmpeg",),
            description="Standardise the master and extract 16 kHz mono audio.",
        )
    if capability == TRANSCRIPT_PLATFORM:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.TRANSCRIPT,
            available=True,
            description="Use the platform's own subtitle track; no recognition.",
        )
    if capability == TRANSCRIPT_SUPPLIED:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.TRANSCRIPT,
            available=True,
            description="Use a subtitle file named by the caller.",
        )
    if capability == TRANSCRIPT_WHISPER_LOCAL:
        probe = _probe_whisper_local(ctx)
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.TRANSCRIPT,
            available=probe.available,
            verified=probe.verified,
            requires=("faster-whisper",),
            cost=Cost.FREE,
            egress=Egress.NONE,
            download_mb=probe.download_mb,
            description=(
                "Local faster-whisper recognition. No key and no audio leaves "
                "the machine; the model may download once."
            ),
        )
    if capability == TRANSCRIPT_WHISPER_API:
        probe = _probe_whisper_api(ctx)
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.TRANSCRIPT,
            available=probe.available,
            verified=probe.verified,
            requires=("openai", "api key"),
            cost=Cost.PAID,
            egress=Egress.AUDIO,
            description="OpenAI-compatible Whisper API. Uploads the audio.",
        )
    if capability in (TRANSLATE_BING, TRANSLATE_GOOGLE, TRANSLATE_MYMEMORY):
        probe = _probe_translation(ctx, capability)
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.TRANSLATE,
            available=probe.available,
            verified=probe.verified,
            requires=("requests",),
            cost=Cost.NETWORK,
            egress=Egress.TEXT,
            description=f"Key-free {capability.split('.', 1)[1]} translation endpoint.",
        )
    if capability == TRANSLATE_LLM:
        probe = _probe_translation(ctx, capability)
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.TRANSLATE,
            available=probe.available,
            verified=probe.verified,
            requires=("openai", "api key"),
            cost=Cost.PAID,
            egress=Egress.TEXT,
            description="OpenAI-compatible chat-completions translation.",
        )
    if capability == WRITE_SUBTITLES:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.WRITE,
            available=True,
            description="Write bilingual and target-language SRT/ASS files.",
        )
    if capability == RENDER_FFMPEG:
        return CapabilityDescriptor(
            id=capability,
            stage=Stage.RENDER,
            available=ffmpeg.available,
            verified=ffmpeg.verified,
            requires=("ffmpeg", "libass"),
            description="Burn hardsubs with ffmpeg's subtitles filter.",
        )
    raise KeyError(f"unknown capability id: {capability}")


def capability_catalog(ctx: RunContext) -> tuple[CapabilityDescriptor, ...]:
    """Describe every capability, probing this machine once per dependency."""
    return tuple(_describe(ctx, capability) for capability in CAPABILITY_IDS)


def capability_for(
    catalog: Iterable[CapabilityDescriptor],
    capability: str,
) -> CapabilityDescriptor | None:
    """Look ``capability`` up in an already-built catalog."""
    for descriptor in catalog:
        if descriptor.id == capability:
            return descriptor
    return None


@dataclass(frozen=True)
class SelectionProblem:
    """Why a requested capability cannot be used, and what to do instead."""

    code: str
    message: str
    alternatives: tuple[str, ...]


def selection_problem(
    descriptor: CapabilityDescriptor | None,
    *,
    requested: str,
    available_alternatives: Sequence[str],
    allow_paid: bool,
) -> SelectionProblem | None:
    """Return the reason ``requested`` is unusable, or ``None`` when it is fine."""
    if descriptor is None:
        return SelectionProblem(
            code="UNKNOWN_CAPABILITY",
            message=f"{requested!r} is not a capability porter knows",
            alternatives=tuple(available_alternatives),
        )
    if not descriptor.available:
        return SelectionProblem(
            code="CAPABILITY_UNAVAILABLE",
            message=(
                f"{requested} is not available on this machine"
                + (f" (requires {', '.join(descriptor.requires)})" if descriptor.requires else "")
            ),
            alternatives=tuple(available_alternatives),
        )
    if descriptor.cost is Cost.PAID and not allow_paid:
        return SelectionProblem(
            code="PAID_CAPABILITY_NOT_ALLOWED",
            message=(
                f"{requested} costs money; pass --allow-paid to permit it"
            ),
            alternatives=tuple(available_alternatives),
        )
    return None
