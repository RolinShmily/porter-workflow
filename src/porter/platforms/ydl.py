"""The single yt-dlp construction point.

Every ``YoutubeDL`` instance in the codebase is created here.

Why that matters
----------------
``yt-dlp`` writes to **stdout** by default. ``YoutubeDL.__init__`` sets::

    self._out_files = Namespace(
        out=sys.stderr if logtostderr else sys.stdout,
        screen=sys.stderr if quiet else sys.stdout,
        ...
    )

so ``quiet=True`` alone is *not* enough: it redirects ``screen`` but leaves
``out`` — used by the progress printer in ``downloader/common.py`` — pointing at
stdout. In an MCP stdio server stdout is the JSON-RPC channel, so a single
progress line corrupts the protocol and the client drops the connection.

``v0.1`` constructed ``YoutubeDL`` inline in eight places, and one of them
(``youtube.py``) passed ``quiet: False``, so downloads did write progress to
stdout. Consolidating construction here makes the safe configuration the *only*
configuration.

Defence in depth, in the order it takes effect:

1. ``noprogress=True``  -> the progress printer becomes a no-op.
2. ``quiet=True``       -> ``screen`` is moved to stderr.
3. ``logtostderr=True`` -> ``out`` is moved to stderr as well.
4. ``logger``           -> ``to_screen``/``to_stderr`` route into porter logging.
5. The MCP frontend's stdout guard catches anything that still escapes.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import yt_dlp

from porter.errors import ExtractionError
from porter.events import EventSink, Phase, ProgressUpdated
from porter.logging import get_logger

#: Containers that mean "this is a video" when a direct URL is all we have.
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {"mp4", "mkv", "webm", "mov", "m4v", "avi", "flv", "ts"}
)

#: Extensions that settle the question the other way. A lone direct URL whose
#: extension is one of these is not a video job, whatever else is missing.
NON_VIDEO_EXTENSIONS: frozenset[str] = VIDEO_EXTENSIONS.union(
    {
        # Images. Instagram and TikTok photo posts arrive as entries like this.
        "jpg", "jpeg", "png", "webp", "gif", "heic", "avif", "bmp",
        # Audio-only.
        "m4a", "mp3", "opus", "ogg", "wav", "aac", "flac", "weba",
    }
)

__all__ = [
    "JS_RUNTIME_PRIORITY",
    "VIDEO_EXTENSIONS",
    "YdlPolicy",
    "available_js_runtimes",
    "build_ydl",
    "download_progress_hook",
    "has_video_stream",
    "translate_ydl_errors",
]

_logger = get_logger(__name__)

#: yt-dlp's own retry count. 3 tolerates a transient challenge without stalling
#: a job for minutes on a genuinely broken URL.
_DEFAULT_RETRIES = 3

#: Package-private alias for the EJS source yt-dlp-pypi users must name
#: explicitly. The official standalone builds bundle the same scripts.
_EJS_GITHUB = "ejs:github"

#: JS runtimes yt-dlp can drive, highest priority first. Quoted from its own
#: ``--js-runtimes`` help -- not a preference invented here. Two consequences
#: worth knowing: only ``deno`` is enabled without being named, and ``bun`` is
#: *last*, below ``quickjs``, despite being the fastest of the four.
JS_RUNTIME_PRIORITY: tuple[str, ...] = ("deno", "node", "quickjs", "bun")

#: Player clients tried in order for YouTube. Ordered by observed reliability:
#: the embedded client is least likely to hit a bot check, and the mobile
#: clients cover formats the web client hides.
_DEFAULT_PLAYER_CLIENTS: tuple[str, ...] = (
    "web_embedded",
    "web",
    "mweb",
    "android_vr",
    "ios",
    "android",
)


class YdlLogRouter:
    """Route yt-dlp's output into ``porter`` logging (i.e. onto stderr).

    yt-dlp calls ``debug`` for ordinary screen output and ``error`` for stderr
    output, which is the inverse of what the names suggest. Both are mapped
    through faithfully rather than guessed at, so ``PORTER_LOG_LEVEL=debug``
    reproduces yt-dlp's normal chatter.
    """

    def __init__(self, logger: Any = None) -> None:
        self._logger = logger or _logger

    def debug(self, message: str) -> None:
        self._logger.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        self._logger.info("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        self._logger.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        self._logger.error("yt-dlp: %s", message)


@dataclass(frozen=True)
class YdlPolicy:
    """Per-call yt-dlp behaviour, independent of any platform.

    Platform-specific bits (format selector, subtitle languages, extractor
    args) are passed via :attr:`extractor_args` and :attr:`extra`.
    """

    cookies_file: str | None = None
    cookies_browser: str | None = None
    player_clients: Sequence[str] | None = None
    extract_flat: bool = False
    retries: int = _DEFAULT_RETRIES
    #: Remote components allowed to be fetched on demand.
    #:
    #: **This is a list of ``"name:source"`` strings, not a mapping.** yt-dlp does
    #: ``set(params['remote_components'])``, so a dict such as
    #: ``{"ejs": "github"}`` iterates to ``{"ejs"}``, which is rejected as
    #: unsupported and silently dropped — leaving YouTube's JS challenge
    #: unsolved. v0.1 shipped exactly that bug; see
    #: ``tests/unit/test_ydl.py::test_remote_components_use_the_documented_form``.
    remote_components: Sequence[str] = (_EJS_GITHUB,)
    #: ``{runtime: {config}}``. ``None`` leaves yt-dlp's own detection in place,
    #: which enables Deno by default when it is on PATH.
    js_runtimes: Mapping[str, Any] | None = None
    extractor_args: Mapping[str, Any] = field(default_factory=dict)
    #: yt-dlp ``format`` selector. ``None`` lets the platform spec decide.
    format: str | None = None
    #: Output template. The pipeline points this at a scratch directory.
    outtmpl: str | None = None
    #: Container for merged video+audio streams.
    merge_output_format: str | None = None
    #: Metadata-only run. Used for subtitle fetches, which must not pull media.
    skip_download: bool = False
    #: Fetch human-authored subtitle tracks.
    write_subtitles: bool = False
    #: Fetch machine-generated caption tracks. Lower quality, always available.
    write_auto_subs: bool = False
    #: Which subtitle languages to request. One language per invocation keeps the
    #: resulting filename unambiguous, so no glob-and-guess is needed.
    subtitle_langs: Sequence[str] = ()
    #: Preference order for subtitle containers.
    #:
    #: ``json`` is in the list because Bilibili's CC track is only ever offered in
    #: that container. Without it the request is for containers the platform does
    #: not have, and the track is skipped -- which is what happened, leaving the
    #: JSON-to-SRT converter ported, exported and tested but unreachable.
    subtitle_format: str = "srt/vtt/json/best"
    #: Continue past a failing entry. Used for best-effort subtitle fetches.
    ignoreerrors: bool = False
    #: Parallel fragment downloads. yt-dlp's default is 1, which is slow on
    #: fragmented HLS/DASH sources.
    concurrent_fragments: int = 5
    #: Escape hatch for options with no first-class field. Cannot override the
    #: stdout-safety keys — see :func:`build_ydl`.
    extra: Mapping[str, Any] = field(default_factory=dict)


def is_video_format(fmt: Mapping[str, Any]) -> bool:
    """Whether a yt-dlp format entry carries a video track.

    Three states, not two:

    ``vcodec == "none"``
        An **explicit** statement that the stream is audio-only. YouTube serves
        ``140``/``251`` this way, and an image carousel marks its photos the
        same. Not a video.
    ``vcodec`` set to anything else
        A video track.
    ``vcodec`` absent
        **Unknown, not "no".** Treated as possibly-video.

    That last choice is deliberate, and the asymmetry is the point: guessing
    "video" when wrong starts a job that fails at download, while guessing "no
    video" when wrong refuses a perfectly good link the user cannot override. The
    two errors are not equally bad, so the tie breaks toward trying.

    What was actually broken in v0.1 (and in the first draft of
    ``base._first_video_entry``) is treating the *presence of a ``formats`` key*
    as proof of video, which collapses the "none" case into the video case.
    """
    return fmt.get("vcodec") != "none"


def has_video_stream(info: Mapping[str, Any]) -> bool:
    """Whether a yt-dlp info dict (or one entry of a playlist) is a video.

    The single definition used by both :meth:`YtDlpExtractor._first_video_entry`
    and ``porter.platforms.inspector``. Two copies of this predicate is how the
    carousel bug came back: the inspector got it right and the extractor did not.
    """
    for fmt in info.get("formats") or []:
        if isinstance(fmt, Mapping) and is_video_format(fmt):
            return True

    entries = info.get("entries")
    if isinstance(entries, list):
        return any(isinstance(e, Mapping) and has_video_stream(e) for e in entries)

    # A lone direct URL with no format table.
    if info.get("url"):
        if not is_video_format(info):
            # vcodec is explicitly "none": an audio-only stream.
            return False
        # Only a *known* non-video extension settles it. An unrecognised or
        # absent extension gets the benefit of the doubt, per the asymmetry
        # above: v0.1 accepted any url at all, which reported photo posts as
        # videos, while the opposite reflex refuses usable links.
        ext = str(info.get("ext") or "").lower()
        return ext not in NON_VIDEO_EXTENSIONS

    return False


def available_js_runtimes(which: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """Every JS runtime on ``PATH``, keyed the way yt-dlp's ``js_runtimes`` expects.

    This exists because yt-dlp's default is ``{'deno': {}}`` -- Deno *only*, hard
    coded in ``YoutubeDL.__init__`` -- and it detects nothing else. So a machine
    with Node installed and no Deno ends up with **no usable runtime at all**,
    while ``porter doctor`` happily reported "JavaScript runtime: OK, node ...
    fully supported by yt-dlp". The advice was true of yt-dlp and false of
    porter, because nothing here ever handed Node over.

    Handing over everything present is safe: yt-dlp selects among the enabled and
    *available* runtimes by its own priority, so Deno still wins wherever it
    exists and Node only takes over in its absence.

    Shared with ``porter.doctor`` rather than duplicated, because the failure
    above was precisely two lists of runtimes that disagreed.
    """
    lookup = which or shutil.which
    return {name: {} for name in JS_RUNTIME_PRIORITY if lookup(name)}


def build_ydl(
    policy: YdlPolicy | None = None,
    *,
    logger: Any = None,
    progress_hook: Callable[[Mapping[str, Any]], None] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> yt_dlp.YoutubeDL:
    """Construct a stdout-safe :class:`yt_dlp.YoutubeDL`.

    Args:
        policy: Platform-level behaviour. Defaults to a plain policy.
        logger: Override the log destination. Defaults to ``porter.platforms.ydl``.
        progress_hook: Receives yt-dlp's raw progress dict. Use
            :func:`download_progress_hook` to turn it into pipeline events.
        extra: Additional options merged last.

    Returns:
        A configured ``YoutubeDL``. Always use it as a context manager so the
        underlying file handles are released.

    Raises:
        ValueError: If ``policy.extra`` or ``extra`` tries to set a
            stdout-safety option. Failing loudly is deliberate: silently
            ignoring the override would hide a protocol-corrupting mistake.
    """
    policy = policy or YdlPolicy()

    opts: dict[str, Any] = {
        # --- stdout safety (see module docstring) -------------------------
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "logtostderr": True,
        "logger": YdlLogRouter(logger),
        # --- behaviour ----------------------------------------------------
        "extract_flat": policy.extract_flat,
        "retries": policy.retries,
        "nocheckcertificate": False,
        "ignoreerrors": policy.ignoreerrors,
        "no_color": True,
    }

    if policy.format:
        opts["format"] = policy.format
    if policy.outtmpl:
        opts["outtmpl"] = policy.outtmpl
    if policy.merge_output_format:
        opts["merge_output_format"] = policy.merge_output_format
    if policy.concurrent_fragments != 1:
        opts["concurrent_fragment_downloads"] = policy.concurrent_fragments
    if policy.skip_download:
        opts["skip_download"] = True
    if policy.write_subtitles or policy.write_auto_subs:
        opts["writesubtitles"] = policy.write_subtitles
        opts["writeautomaticsub"] = policy.write_auto_subs
        opts["subtitleslangs"] = list(policy.subtitle_langs)
        opts["subtitlesformat"] = policy.subtitle_format

    if policy.remote_components:
        opts["remote_components"] = list(policy.remote_components)
    if policy.js_runtimes is not None:
        opts["js_runtimes"] = dict(policy.js_runtimes)
    else:
        # Rather than leaving yt-dlp's Deno-only default in place, name whatever
        # is actually installed. An empty result leaves the key unset, so a
        # machine with no runtime behaves exactly as it did before.
        discovered = available_js_runtimes()
        if discovered:
            opts["js_runtimes"] = discovered
    if policy.cookies_file:
        opts["cookiefile"] = policy.cookies_file
    if policy.cookies_browser:
        opts["cookiesfrombrowser"] = (policy.cookies_browser,)

    # extractor_args is built up rather than assigned, so that setting
    # player_clients does not silently discard platform-specific args.
    extractor_args: dict[str, Any] = _merge_extractor_args(policy.extractor_args)
    if policy.player_clients:
        youtube_args = dict(extractor_args.get("youtube") or {})
        youtube_args["player_client"] = list(policy.player_clients)
        extractor_args["youtube"] = youtube_args
    if extractor_args:
        opts["extractor_args"] = extractor_args

    opts.update(policy.extra)
    if extra:
        opts.update(extra)

    _reject_unsafe_overrides(opts)

    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]

    return yt_dlp.YoutubeDL(opts)


@contextmanager
def translate_ydl_errors(*, url: str, platform: str) -> Iterator[None]:
    """Turn yt-dlp's own exceptions into :class:`~porter.errors.ExtractionError`.

    yt-dlp raises :class:`yt_dlp.utils.YoutubeDLError` for a whole family of
    site-side conditions: a removed video, a geo-block, a bot check, or -- the
    case that exposed this -- a format selector that matches nothing because the
    site stopped serving those streams to the client that was used.

    Letting one escape hands the user a Python traceback, which the engine
    promises never to do. Every expected failure is a ``PorterError`` with a
    ``code`` and structured detail, so the CLI can render it and the MCP
    frontend can return it as data. The yt-dlp message is preserved verbatim,
    because it is usually the most precise description of what happened.
    """
    try:
        yield
    except yt_dlp.utils.YoutubeDLError as exc:
        raise ExtractionError(str(exc), url=url, platform=platform) from exc


def _merge_extractor_args(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-ish copy of ``extractor_args`` so per-call edits cannot mutate policy."""
    merged: dict[str, Any] = {}
    for key, value in raw.items():
        merged[key] = dict(value) if isinstance(value, Mapping) else value
    return merged


def _reject_unsafe_overrides(opts: Mapping[str, Any]) -> None:
    """Fail loudly if anything disabled stdout safety.

    The check runs *after* merging and compares against the safe values we set,
    so it catches ``extra={"quiet": False}`` rather than merely noticing that
    the key was mentioned. Failing is deliberate: silently ignoring the override
    would hide a protocol-corrupting mistake.

    Raises:
        ValueError: If a safety option was overridden or the logger removed.
    """
    unsafe = {
        key: opts.get(key)
        for key in ("quiet", "noprogress", "logtostderr")
        if opts.get(key) is not True
    }
    if unsafe:
        raise ValueError(
            "yt-dlp stdout-safety options cannot be overridden: "
            f"{unsafe}. stdout carries the MCP JSON-RPC protocol; "
            "use the logger to surface yt-dlp output instead."
        )
    if opts.get("logger") is None:
        raise ValueError("yt-dlp must always be given a logger so nothing reaches stdout")


def download_progress_hook(
    sink: EventSink,
    *,
    phase: Phase = Phase.PREPARE,
    throttle_percent: float = 1.0,
) -> Callable[[Mapping[str, Any]], None]:
    """Build a yt-dlp progress hook that emits :class:`ProgressUpdated` events.

    yt-dlp calls the hook several times per second, which is far more often than
    a terminal or an MCP client needs, so updates are throttled to whole
    ``throttle_percent`` steps. Completion is always emitted.

    Args:
        sink: Receives the events.
        phase: Phase to attribute progress to. Defaults to ``PREPARE``.
        throttle_percent: Width of the percentage bucket. One event is emitted
            per bucket, so ``1.0`` caps a download at ~100 events.

    Returns:
        A callable suitable for ``YdlPolicy``/``build_ydl(progress_hook=...)``.
    """
    state = {"bucket": -1}

    def hook(payload: Mapping[str, Any]) -> None:
        status = payload.get("status")
        if status == "finished":
            state["bucket"] = -1
            sink(ProgressUpdated(phase=phase, percent=100.0, message="download finished"))
            return
        if status != "downloading":
            return

        total = payload.get("total_bytes") or payload.get("total_bytes_estimate")
        downloaded = payload.get("downloaded_bytes") or 0
        if not total:
            return

        percent = min(100.0, downloaded / total * 100.0)
        # Bucketing rather than a delta comparison: ``percent`` accumulates float
        # error, so ``percent - last`` drifts and the emission count becomes
        # input-dependent. A bucket is exact.
        bucket = int(percent // throttle_percent)
        if bucket == state["bucket"] and percent < 100.0:
            return
        state["bucket"] = bucket
        sink(
            ProgressUpdated(
                phase=phase,
                percent=percent,
                message=f"downloading {percent:.0f}%",
            )
        )

    return hook
