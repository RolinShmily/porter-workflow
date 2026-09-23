"""Pre-flight link inspection: "is this usable, and what will happen to it?"

Backs ``porter inspect`` and the MCP ``porter_inspect`` tool. Answers in about a
second without downloading anything, so a human or an agent can decide whether to
commit to a job that may run for an hour.

What changed from v0.1, and why
-------------------------------
**No printing.** v0.1's ``format_summary`` wrote straight to stdout via four
``print()`` calls. That is fatal behind MCP, where stdout carries JSON-RPC:
inspecting a link would corrupt the protocol stream. The engine returns data; the
frontend renders it.

**No unconditional retry loop with ``time.sleep``.** v0.1 slept
``1.5 * attempt`` between three attempts — up to 4.5 s of dead time — and retried
errors that are not transient. Backoff now waits on the run context's cancel
event, so a cancelled job stops immediately instead of finishing its nap, and
only rate-limit/anti-bot responses are retried. A 404 or a private post returns
at once.

**``is_valid`` and ``has_video`` come from the same evidence.** v0.1 computed a
``has_video_stream`` flag, used it for the "no video" rejection, then **fell
through and returned ``is_valid=True, has_video=True`` unconditionally** — so a
post with formats that carry no video codec (a photo carousel, an audio-only
entry) was reported as a valid video. It escaped notice because the test used
``formats: []``. Both fields are now derived from the measured video stream.

**Orientation stays tri-state.** ``is_vertical`` is only True/False when width
and height are actually known; the spec's ``default_vertical`` fills in
otherwise. v0.1 set ``is_vertical = bool(width and height and height > width)``,
which reports a 1080p horizontal video for an unmeasured vertical one.
"""

from __future__ import annotations

from typing import Any

from porter.context import RunContext
from porter.logging import get_logger
from porter.models.inspection import InspectionResult
from porter.platforms.base import YtDlpExtractor
from porter.platforms.registry import PlatformRegistry, registry
from porter.platforms.spec import PlatformSpec
from porter.platforms.ydl import has_video_stream
from porter.utils.text import sanitize_filename

__all__ = ["BACKOFF_BASE_SECONDS", "NO_VIDEO_MESSAGE", "UNSUPPORTED", "inspect_url"]

_logger = get_logger(__name__)

#: Shown when no spec claims the URL. There is no generic extractor, so this is a
#: dead end and saying so beats v0.1's ``"generic"``, which implied a fallback
#: that did not exist.
UNSUPPORTED = "unsupported"

#: The documented "no video here" message. Part of the ported contract, which
#: asserts on the substring ``does not contain any video``.
NO_VIDEO_MESSAGE = "The provided post/link does not contain any video streams."

#: Cap on metadata attempts. Each one is a live network round trip.
_MAX_ATTEMPTS = 3

#: Seconds multiplied by the attempt number to get the backoff. Module-level so
#: tests can zero it instead of sleeping for 4.5 seconds.
BACKOFF_BASE_SECONDS = 1.5

#: Substrings that mean "come back later" rather than "this link is bad".
_TRANSIENT_MARKERS = ("429", "412", "too many requests", "rate limit", "temporarily")

#: Substrings worth translating into advice, because the raw yt-dlp text is
#: opaque to whoever pasted the link.
_AUTH_MARKERS = ("login", "authentication", "private", "sign in", "members-only")

_FATAL_MARKERS = ("not found", "404", "deleted", "unavailable", "removed")

_AUTH_MESSAGE = "Authentication required. Configure cookies via --cookies or --cookies-from-browser."
_RATE_LIMIT_MESSAGE = (
    "Rate limit or anti-bot challenge (HTTP 429/412). Retry later or supply cookies."
)
_NOT_FOUND_MESSAGE = "Resource not found, deleted, or private (404)."


def inspect_url(
    url: str,
    ctx: RunContext | None = None,
    *,
    registry_: PlatformRegistry | None = None,
) -> InspectionResult:
    """Probe ``url`` and describe what a job on it would do.

    Never raises for a bad link: an unusable URL is a *result*, not an error,
    because the whole point of the call is to find out. Genuine faults (a missing
    dependency, a cancelled job) still propagate.

    Args:
        url: The link to inspect. May be a bare host or a shortener.
        ctx: Run context, for cookies, cancellation and events.
        registry_: Override the platform registry (tests).

    Returns:
        An :class:`~porter.models.inspection.InspectionResult`. Check
        :attr:`~porter.models.inspection.InspectionResult.is_valid` before
        trusting the rest.
    """
    reg = registry_ or registry()
    canonical = reg.canonicalize(url)

    handler = reg.find_or_none(canonical)
    if not isinstance(handler, YtDlpExtractor):
        return InspectionResult(
            input_url=url,
            canonical_url=canonical,
            platform=UNSUPPORTED,
            is_valid=False,
            has_video=False,
            error_message=(
                f"Unsupported URL platform. Supported: {', '.join(reg.names()) or 'none registered'}."
            ),
        )

    spec: PlatformSpec = handler.spec
    info, message = _extract_with_retry(canonical, spec, handler, ctx)
    if info is None:
        return InspectionResult(
            input_url=url,
            canonical_url=canonical,
            platform=spec.name,
            is_valid=False,
            has_video=False,
            error_message=message,
        )

    return _build_result(url, canonical, spec, info)


# ----------------------------------------------------------------------
# Metadata acquisition
# ----------------------------------------------------------------------

def _extract_with_retry(
    canonical: str,
    spec: PlatformSpec,
    handler: YtDlpExtractor,
    ctx: RunContext | None,
) -> tuple[dict[str, Any] | None, str]:
    """Fetch metadata, retrying only transient failures.

    Returns the info dict and a failure message. The message travels back as a
    return value rather than through a module-level cache keyed by URL: such a
    cache grows without bound and lets one job's failure surface in another's
    report, which is the kind of state that only shows up in production.

    Backoff waits on the cancel event rather than sleeping, so a cancelled job
    stops immediately instead of finishing its nap first.
    """
    message = "Could not extract metadata from this URL."

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if ctx is not None:
            ctx.check_cancelled()
        try:
            return handler.fetch_info(canonical, ctx), message
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            raw = str(exc)
            lowered = raw.lower()
            _logger.debug("inspection attempt %d/%d failed: %s", attempt, _MAX_ATTEMPTS, raw)

            if any(marker in lowered for marker in _AUTH_MARKERS):
                return None, _AUTH_MESSAGE
            if any(marker in lowered for marker in _FATAL_MARKERS):
                return None, _NOT_FOUND_MESSAGE

            message = (
                _RATE_LIMIT_MESSAGE
                if any(marker in lowered for marker in _TRANSIENT_MARKERS)
                else (raw or message)
            )

            if attempt < _MAX_ATTEMPTS:
                delay = BACKOFF_BASE_SECONDS * attempt
                if ctx is not None:
                    if ctx.cancel.wait(delay):
                        ctx.check_cancelled()
                else:  # pragma: no cover - production always has a context
                    import time

                    time.sleep(delay)

    return None, message


# ----------------------------------------------------------------------
# Interpretation
# ----------------------------------------------------------------------


def _measure(info: dict[str, Any]) -> tuple[int | None, int | None]:
    """Best available width/height: the entry, then the largest video format."""
    width = _as_int(info.get("width"))
    height = _as_int(info.get("height"))
    if width and height:
        return width, height

    best: tuple[int | None, int | None] = (width, height)
    best_height = height or 0
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict) or fmt.get("vcodec") in (None, "none", ""):
            continue
        fmt_height = _as_int(fmt.get("height")) or 0
        if fmt_height > best_height:
            best_height = fmt_height
            best = (_as_int(fmt.get("width")), _as_int(fmt.get("height")))
    return best


def _build_result(
    url: str,
    canonical: str,
    spec: PlatformSpec,
    info: dict[str, Any],
) -> InspectionResult:
    """Turn an info dict into an :class:`InspectionResult`."""
    has_video = has_video_stream(info)
    if not has_video:
        return InspectionResult(
            input_url=url,
            canonical_url=canonical,
            platform=spec.name,
            is_valid=False,
            has_video=False,
            video_id=str(info.get("id")) if info.get("id") else None,
            error_message=NO_VIDEO_MESSAGE,
            raw_info=dict(info),
        )

    width, height = _measure(info)
    raw_title = info.get("title") or info.get("description") or "video"
    title = spec.clean_title(raw_title, str(info.get("id") or "unknown_id"))

    # Tri-state: only claim an orientation when the pixels are known, otherwise
    # fall back to what the platform implies. A guessed False styles a vertical
    # video with horizontal subtitle margins.
    is_vertical = height > width if width and height else spec.default_vertical

    subtitles = info.get("subtitles") or {}
    auto_captions = info.get("automatic_captions") or {}

    return InspectionResult(
        input_url=url,
        canonical_url=canonical,
        platform=spec.name,
        is_valid=True,
        has_video=True,
        video_id=str(info.get("id") or spec.video_id(canonical) or "unknown_id"),
        title=title,
        safe_title=sanitize_filename(title, max_length=60),
        uploader=info.get("uploader") or info.get("channel"),
        channel=info.get("channel") or info.get("uploader"),
        duration_seconds=_as_float(info.get("duration")),
        width=width,
        height=height,
        is_vertical=is_vertical,
        has_subtitles=bool(subtitles or auto_captions),
        thumbnail_url=info.get("thumbnail"),
        raw_info=dict(info),
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
