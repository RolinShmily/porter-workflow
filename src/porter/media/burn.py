"""Phase BURN: hard-sub the release videos into the master.

Ported from v0.1 ``synthesizer/burn.py``. Two release videos are produced from
the same master, differing only in which subtitle track is burned in::

    cooked/video_bilingual.mp4    subtitle_bilingual.ass
    cooked/video_zh.mp4           subtitle_zh.ass

## The escaping problem, and why this module does not solve it by escaping

v0.1 built a filtergraph argument containing the **absolute subtitle path** and
escaped the characters ffmpeg treats specially::

    -vf ass='/home/me/it\\'s_a_title/cooked/subtitle_zh.ass'

Measured against real ffmpeg, **this does not work**, and neither does any other
single-quote escape. The filtergraph parser treats ``'`` as a quote delimiter
and consumes it, so a path containing an apostrophe becomes a *different path*::

    subtitles='/tmp/esc/it's here/sub.srt'   ->  "Unable to open /tmp/esc/its here/sub.srt"

The apostrophe is silently dropped. Tried and failed: no escape, one backslash,
two, three, the shell's close-escape-reopen form, percent-encoding, bare (no
quoting), and ``filename=`` option syntax. One or two backslashes leave a literal
backslash in the name; the rest drop the quote.

This is reachable, not theoretical: :func:`~porter.utils.text.sanitize_filename`
**keeps apostrophes**, so a video titled "It's a Wonderful Life" produces the
task directory ``..._It's_a_Wonderful_Life/`` and every burn fails with
"Unable to open". Brackets survive sanitisation too.

So this module removes the problem instead of escaping it: the child process runs
with ``cwd`` set to the subtitle's directory and the filtergraph names the
subtitle by its **bare filename** -- ``subtitle_zh.ass``, a constant defined in
``porter.asr.chain`` with no special characters. The user's output directory
never enters the filtergraph at all.

:func:`escape_ffmpeg_filter_path` is still provided, because the contract is
documented and because it is the right thing for the characters it *can* handle
(a Windows drive-letter colon). It is applied to the filename as
defence-in-depth. It is explicitly **not** the mechanism that makes this work,
and its docstring says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from porter.config import FFmpegConfig
from porter.errors import RenderError
from porter.events import ArtifactKind, ArtifactReady, Phase, ProgressUpdated
from porter.logging import get_logger
from porter.media.encode import EncoderProfile, EncoderSelector, detect_encoder
from porter.media.ffmpeg import FFmpegRunner
from porter.media.probe import probe
from porter.models.materials import RawMaterials
from porter.models.request import BurnMode, BurnResult
from porter.models.subtitle import SubtitleSet

__all__ = [
    "BILINGUAL_NAME",
    "ZH_NAME",
    "FfmpegRenderer",
    "burn_hardsub",
    "escape_ffmpeg_filter_path",
    "render_release",
]

_logger = get_logger(__name__)

#: Release filenames. Fixed constants rather than derived from the title, which
#: is what lets the filtergraph reference them without escaping.
BILINGUAL_NAME = "video_bilingual.mp4"
ZH_NAME = "video_zh.mp4"

#: How long a burn may run before it is treated as hung.
#:
#: A 10-minute 1080p master takes roughly four minutes to re-encode on this
#: machine, so two hours is not a close call for any realistic input -- it only
#: catches a genuine hang (a stuck network mount, a wedged encoder). v0.1 passed
#: no timeout at all, so such a job never returned.
BURN_TIMEOUT_SECONDS = 7200.0

#: Substrings in ffmpeg stderr that mean "this build cannot do subtitles", which
#: needs different advice from "your subtitle file is broken".
_MISSING_FILTER_HINTS = (
    "no such filter",
    "filter not found",
    "unknown filter",
    "not built with",
)


def _escape_filter_text(text: str) -> str:
    """Escape filtergraph-special characters in ``text``. No path resolution.

    Kept separate from :func:`escape_ffmpeg_filter_path` because that function
    resolves the path, and resolving is exactly what must *not* happen for a bare
    filename: ``Path("subtitle_zh.ass").resolve()`` resolves against the calling
    process's working directory, not the child's ``cwd``, so the filtergraph ends
    up with an absolute path under the wrong directory. That bug was hit for real
    -- ffmpeg reported
    ``Could not create a libass track when reading file '/home/.../subtitle_zh.ass'``
    while the file was actually in the task directory.
    """
    return (
        text.replace("\\", "/")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("[", r"\[")
        .replace("]", r"\]")
    )


def escape_ffmpeg_filter_path(path: Path | str) -> str:
    """Escape a path for an ffmpeg filtergraph argument.

    Applies v0.1's cross-platform rules: resolve to absolute, backslashes to
    forward slashes, then escape ``:`` ``'`` ``[`` ``]``.

    ## What this cannot do

    **It cannot make a path containing an apostrophe work.** Measured against
    real ffmpeg, every available spelling of an escaped single quote is consumed
    by the filtergraph parser and the apostrophe disappears from the path. The
    same is true for ``[`` and ``]``, which the parser also treats as syntax.

    Do not rely on this to sanitise a user-supplied directory. Pass such paths via
    the child's working directory instead, as :func:`burn_hardsub` does. This
    function is correct only for the characters ffmpeg lets you escape, and is
    used here on a filename that is already known to be safe.
    """
    path_str = str(Path(path).resolve())
    return _escape_filter_text(path_str)


def _filter_arg(subtitle: Path) -> str:
    """The filtergraph expression for ``subtitle``, quoted for safety.

    Uses the **bare filename, unresolved**: the caller must run ffmpeg with
    ``cwd`` set to ``subtitle.parent``. See the module docstring.
    """
    name = _escape_filter_text(subtitle.name)
    filter_name = "ass" if subtitle.suffix.lower() == ".ass" else "subtitles"
    return f"{filter_name}='{name}'"


def _diagnose(stderr: str, subtitle: Path) -> str:
    """Turn an ffmpeg failure into a sentence that names the likely cause."""
    lowered = stderr.lower()
    if any(hint in lowered for hint in _MISSING_FILTER_HINTS):
        return (
            f"this ffmpeg cannot render subtitles ({subtitle.suffix} filter "
            f"unavailable); it was probably built without libass. "
            f"Run `porter doctor` for the full check."
        )
    if "no such file" in lowered or "unable to open" in lowered:
        return f"ffmpeg could not read {subtitle.name} or the master video"
    return "ffmpeg failed to burn the subtitles"


@dataclass(frozen=True)
class _Variant:
    """One release video: which subtitle track, and where it goes."""

    subtitle: Path
    output: Path
    label: str


def burn_hardsub(
    runner: FFmpegRunner,
    video_input: Path,
    subtitle: Path,
    video_output: Path,
    *,
    profile: EncoderProfile,
    timeout: float | None = BURN_TIMEOUT_SECONDS,
) -> Path:
    """Burn ``subtitle`` into ``video_input``, atomically, into ``video_output``.

    The result is written to a temporary file next to the destination and only
    renamed into place after it has been **probed and found readable**. v0.1
    renamed unconditionally, so a truncated encode was published as a finished
    release video; it validated the output only on the reuse path, which is the
    path that does not produce a file.

    Args:
        runner: ffmpeg runner.
        video_input: The standardised master.
        subtitle: The ``.ass`` (or ``.srt``) track to burn.
        video_output: Destination, normally under ``cooked/``.
        profile: Encoder settings from :func:`~porter.media.encode.detect_encoder`.
        timeout: Seconds, or ``None`` for no limit.

    Returns:
        ``video_output``.

    Raises:
        RenderError: ffmpeg failed, or produced an unreadable file.
        MediaError: ffmpeg is missing.
    """
    # Absolute from here on. The child runs with ``cwd=subtitle.parent`` (see the
    # filtergraph note below), so a relative path anywhere in the argument list
    # would be resolved against the subtitle's directory instead of ours --
    # ffmpeg would look for ``cooked/porter_output/<task>/raw/video.mp4`` and
    # report "could not read the master video" while the file sits right there.
    #
    # This survived the pixel-level verification because every test passes a
    # ``tmp_path``, which is absolute by construction: the relative case had no
    # test, so the cwd fix for apostrophe paths shipped with a hole in it. Found
    # on the first real CLI run, where the pipeline passes the output-relative
    # paths it reports to the user.
    video_input = video_input.resolve()
    subtitle = subtitle.resolve()
    video_output = video_output.resolve()

    video_output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = video_output.with_name(f".tmp_{video_output.name}")
    temp_output.unlink(missing_ok=True)

    args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_input),
        # No -map here on purpose: the master is produced by standardize_video
        # with exactly one video and one audio stream, and the subtitle filter
        # must apply to that video stream.
        "-vf",
        _filter_arg(subtitle),
        *profile.args(),
        # No -pix_fmt here: every EncoderProfile already carries one, and a
        # second copy produced `-pix_fmt yuv420p -pix_fmt yuv420p`. Letting the
        # profile own it is the reason EncoderProfile carries flags at all.
        # Copy, never re-encode: the audio is already AAC and re-encoding it
        # would be both lossy and slow.
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(temp_output),
    ]

    try:
        runner.run(
            args,
            what=f"burning {subtitle.name}",
            timeout=timeout,
            # The filtergraph names the subtitle by bare filename, so the child
            # must run where that filename resolves. This is what keeps a path
            # with an apostrophe (or brackets, or anything else ffmpeg's
            # filtergraph parser eats) out of the filter expression entirely.
            cwd=subtitle.parent,
        )
    except RenderError:
        raise
    except Exception as exc:
        temp_output.unlink(missing_ok=True)
        stderr = getattr(exc, "details", {}).get("stderr", "") or str(exc)
        raise RenderError(
            f"burning {subtitle.name} failed: {_diagnose(stderr, subtitle)}",
            subtitle=str(subtitle),
            output=str(video_output),
            stderr=stderr,
        ) from exc

    # Validate before publishing. This is the check v0.1 only performed on the
    # reuse path, i.e. never on a file it had just created.
    if not temp_output.is_file():
        raise RenderError(
            f"ffmpeg reported success but wrote no file for {subtitle.name}",
            subtitle=str(subtitle),
            output=str(video_output),
        )
    info = probe(runner, temp_output)
    if info is None or not info.has_video:
        temp_output.unlink(missing_ok=True)
        raise RenderError(
            f"the burned video is unreadable; refusing to publish it as {video_output.name}",
            subtitle=str(subtitle),
            output=str(video_output),
        )

    temp_output.replace(video_output)
    _logger.info(
        "burned %s -> %s (%.1f MB)",
        subtitle.name,
        video_output.name,
        video_output.stat().st_size / 1_048_576,
    )
    return video_output


def _is_reusable(
    runner: FFmpegRunner,
    output: Path,
    inputs: tuple[Path, ...],
) -> bool:
    """Whether an existing release video can be kept instead of re-encoded.

    Reused only when the file is readable and at least as new as every input, so
    a changed subtitle or master invalidates it. Re-encoding a 10-minute video
    takes minutes, and the common case -- re-running a job after a translation
    tweak -- has not changed the video.
    """
    if not output.is_file():
        return False
    info = probe(runner, output)
    if info is None or not info.has_video:
        return False
    out_mtime = output.stat().st_mtime
    return all(
        (source.stat().st_mtime if source.is_file() else 0.0) <= out_mtime
        for source in inputs
    )


def render_release(
    runner: FFmpegRunner,
    raw: RawMaterials,
    subtitles: SubtitleSet,
    mode: BurnMode,
    *,
    cooked_dir: Path,
    config: FFmpegConfig | None = None,
    force: bool = False,
    selector: EncoderProfile | None = None,
    timeout: float | None = BURN_TIMEOUT_SECONDS,
) -> BurnResult:
    """Render the release videos ``mode`` asks for.

    Args:
        runner: ffmpeg runner.
        raw: PREPARE output; ``raw.video`` is the master.
        subtitles: TRANSLATE output; supplies the ASS tracks.
        mode: Which variants to produce.
        cooked_dir: Where the release videos go.
        config: Encoding parameters, used only for the codec fallback.
        force: Re-encode even when an up-to-date release video exists.
        selector: Pre-computed encoder profile, so a caller burning both variants
            probes the hardware once. Defaults to probing here.
        timeout: Per-burn timeout in seconds.

    Returns:
        The paths that were produced. A variant not requested by ``mode`` stays
        ``None`` rather than being reported as an empty path.

    Raises:
        RenderError: A burn failed.
    """
    cooked_dir = Path(cooked_dir)
    cooked_dir.mkdir(parents=True, exist_ok=True)

    variants: list[_Variant] = []
    if mode.wants_bilingual:
        variants.append(
            _Variant(subtitles.subtitle_bilingual_ass, cooked_dir / BILINGUAL_NAME, "bilingual")
        )
    if mode.wants_zh:
        variants.append(_Variant(subtitles.subtitle_zh_ass, cooked_dir / ZH_NAME, "Chinese-only"))

    result = BurnResult()
    if not variants:
        _logger.info("burn skipped: mode is %s", mode.value)
        return result

    for variant in variants:
        if not variant.subtitle.is_file():
            raise RenderError(
                f"cannot burn the {variant.label} video: {variant.subtitle.name} was not written",
                subtitle=str(variant.subtitle),
            )

    # Probing the hardware costs a trial encode per candidate, so do it once for
    # both variants rather than once each.
    profile = selector or detect_encoder(runner)

    for variant in variants:
        if not force and _is_reusable(runner, variant.output, (raw.video, variant.subtitle)):
            _logger.info("reusing up-to-date %s", variant.output.name)
        else:
            burn_hardsub(
                runner,
                raw.video,
                variant.subtitle,
                variant.output,
                profile=profile,
                timeout=timeout,
            )
        if variant.output.name == ZH_NAME:
            result.video_zh = variant.output
        else:
            result.video_bilingual = variant.output

    return result


class FfmpegRenderer:
    """The :class:`~porter.ports.Renderer` implementation.

    Holds the runner and one :class:`~porter.media.encode.EncoderSelector`, so the
    hardware verdict is probed **once per process** rather than once per job. That
    matters for the MCP frontend, which runs many jobs in one process: each
    selector miss costs a real ffmpeg process per candidate encoder, and the whole
    point of the selector's cache is to avoid paying that repeatedly. ``doctor``
    consults the same machinery, which is why the cache lives on the selector
    rather than in module state.
    """

    name = "ffmpeg"

    def __init__(
        self,
        runner: FFmpegRunner | None = None,
        *,
        config: FFmpegConfig | None = None,
        timeout: float | None = BURN_TIMEOUT_SECONDS,
        selector: EncoderSelector | None = None,
    ) -> None:
        self.runner = runner or FFmpegRunner()
        self.config = config
        self.timeout = timeout
        self._selector = selector

    def render(
        self,
        raw: RawMaterials,
        subtitles: SubtitleSet,
        mode: BurnMode,
        ctx: object,
    ) -> BurnResult:
        """Burn each variant ``mode`` requests and announce the artifacts."""
        from porter.context import RunContext

        if not isinstance(ctx, RunContext):  # pragma: no cover - defensive
            raise TypeError("render() needs a RunContext")

        cooked_dir = raw.layout.task_dir / "cooked"
        result = render_release(
            self.runner,
            raw,
            subtitles,
            mode,
            cooked_dir=cooked_dir,
            config=self.config,
            force=ctx.options.force,
            timeout=self.timeout,
            # `select()` is cached on the selector, so the second job in this
            # process reuses the first job's verdict instead of re-probing.
            selector=self._encoder().select(),
        )

        for path in (result.video_bilingual, result.video_zh):
            if path is not None:
                ctx.emit(ArtifactReady(kind=ArtifactKind.VIDEO, path=path, phase=Phase.BURN))
        ctx.emit(ProgressUpdated(phase=Phase.BURN, percent=100.0, message="release videos ready"))
        return result

    def _encoder(self) -> EncoderSelector:
        """The process-wide encoder selector, created on first use."""
        if self._selector is None:
            self._selector = EncoderSelector(self.runner)
        return self._selector
