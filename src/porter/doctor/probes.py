"""Capability probing: **facts only**, never prose.

## The v0.1 problem this solves

``env_check.CheckResult`` carried a boolean, a message, *and* a multi-paragraph
Chinese installation guide, with ``guide=None`` meaning "nothing to say" and
``is_warning=True`` meaning "``status=False`` but do not stop". Three concerns in
one struct, and the prose made it unusable anywhere except a terminal.

That is not a cosmetic problem. The MCP frontend has to answer "can this machine
burn hardsubs?" over a protocol with no terminal, and a wall of Chinese
instructions is not an answer it can act on — it needs to know *which* capability
is missing and *how bad* that is.

## The split

:class:`Finding` is a measurement: a stable ``key``, whether it passed, and — if
it did not — a :class:`Severity` and a ``remediation_key``. No sentences telling
anyone what to type.

:mod:`porter.doctor.guides` holds the sentences, keyed by exactly those
``remediation_key`` values. The CLI renders them; the MCP frontend can expose
them as a resource and otherwise ignore them.

A missing guide is not an error, because the key is a stable identifier and the
prose is presentation. That direction of dependency is what lets the text change
without the fact model changing.

## Severity

===============================  ==================================================
``BLOCKER``                      The pipeline cannot produce its output at all.
``DEGRADED``                     Output is produced, at a real cost — a missing
                                 hardware encoder, a missing font, a missing JS
                                 runtime.
``INFO``                         Worth reporting, no action implied. Passing
                                 findings and plain facts (which encoder was
                                 chosen) both live here.
===============================  ==================================================

Two things ``v0.1`` did not check at all are now required, because both fail
*silently*: :func:`probe_libass` (without the ``ass`` filter, hardsubbing
degrades to soft subtitles) and :func:`probe_js_runtime` (without a JS runtime,
yt-dlp cannot solve YouTube's challenges and quietly returns fewer formats).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from porter.asr import whisper_local
from porter.config import PorterConfig
from porter.errors import PorterError
from porter.logging import get_logger
from porter.media.encode import EncoderSelector, HardwareTier
from porter.media.ffmpeg import FFmpegRunner, FFmpegTools

__all__ = [
    "CapabilityReport",
    "Finding",
    "ProbeContext",
    "Severity",
    "probe_all",
    "probe_asr_route",
    "probe_encoder",
    "probe_ffmpeg",
    "probe_font",
    "probe_js_runtime",
    "probe_libass",
    "probe_output_dir",
    "probe_python",
    "probe_translation_route",
    "probe_yt_dlp",
]

_logger = get_logger(__name__)

#: Timeout for the small helper processes this module spawns. Short: every one
#: of them answers a question that should take milliseconds, and a hung probe is
#: worse than a failed one because ``doctor`` is the command people run when
#: something is already wrong.
_PROBE_TIMEOUT = 10.0

#: JS runtimes yt-dlp can drive, best first. Deno is upstream's recommendation
#: and is the only one yt-dlp enables by default.
_JS_RUNTIMES = ("deno", "node", "bun", "quickjs")

#: Runtimes yt-dlp will accept are not all equally capable. Recorded so the
#: finding can say which one was found rather than only that one exists.
_JS_RUNTIME_NOTES = {
    "deno": "yt-dlp's recommended runtime",
    "node": "fully supported by yt-dlp",
    "bun": "supported by yt-dlp",
    "quickjs": "supported, but slower and incomplete for some challenges",
}


class Severity(str, Enum):
    """Impact of a finding that did **not** pass.

    ``(str, Enum)`` rather than :class:`enum.StrEnum`` for Python 3.10 support.
    """

    INFO = "info"
    DEGRADED = "degraded"
    BLOCKER = "blocker"

    @property
    def rank(self) -> int:
        return {Severity.INFO: 0, Severity.DEGRADED: 1, Severity.BLOCKER: 2}[self]


@dataclass(frozen=True)
class Finding:
    """One measurement. Facts, not advice.

    Construct through :meth:`passed`, :meth:`degraded`, :meth:`blocked` or
    :meth:`info` so the severity can never disagree with ``ok`` — the failure
    mode where a finding reports ``ok=False, severity=INFO`` and a caller that
    only checks severity concludes everything is fine.
    """

    key: str
    label: str
    ok: bool
    severity: Severity
    #: What was measured, e.g. ``"ffmpeg n9.0.1 at /usr/sbin/ffmpeg"``. A fact,
    #: never an instruction.
    detail: str = ""
    #: Key into :mod:`porter.doctor.guides`. ``None`` means nothing to do.
    remediation_key: str | None = None

    @classmethod
    def passed(cls, key: str, label: str, detail: str = "") -> Finding:
        return cls(key=key, label=label, ok=True, severity=Severity.INFO, detail=detail)

    @classmethod
    def info(cls, key: str, label: str, detail: str = "") -> Finding:
        """A fact worth reporting that is not a pass/fail question.

        ``ok=True`` because nothing is wrong. Used for "the encoder selected was
        X" — an operator needing to know why NVENC is not in use has to be able
        to see the answer without reading logs.
        """
        return cls(key=key, label=label, ok=True, severity=Severity.INFO, detail=detail)

    @classmethod
    def degraded(cls, key: str, label: str, detail: str, remediation_key: str) -> Finding:
        return cls(
            key=key,
            label=label,
            ok=False,
            severity=Severity.DEGRADED,
            detail=detail,
            remediation_key=remediation_key,
        )

    @classmethod
    def blocked(cls, key: str, label: str, detail: str, remediation_key: str) -> Finding:
        return cls(
            key=key,
            label=label,
            ok=False,
            severity=Severity.BLOCKER,
            detail=detail,
            remediation_key=remediation_key,
        )


@dataclass
class CapabilityReport:
    """The findings from one probe run, plus the questions callers really ask."""

    findings: list[Finding] = field(default_factory=list)
    #: Platform string, reported so a bug report is actionable.
    platform: str = field(default_factory=lambda: f"{platform.system()} {platform.release()}")

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKER and not f.ok]

    @property
    def degradations(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.DEGRADED and not f.ok]

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok]

    @property
    def ok(self) -> bool:
        """Whether the pipeline can run to completion.

        Degradations do not make this False. That distinction is the whole point
        of having severity: "no GPU" must not read the same as "no ffmpeg".
        """
        return not self.blockers

    def get(self, key: str) -> Finding | None:
        return next((f for f in self.findings if f.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, with severities as plain strings."""
        return {
            "platform": self.platform,
            "ok": self.ok,
            "blockers": [f.key for f in self.blockers],
            "degradations": [f.key for f in self.degradations],
            "findings": [
                {
                    "key": f.key,
                    "label": f.label,
                    "ok": f.ok,
                    "severity": f.severity.value,
                    "detail": f.detail,
                    "remediation_key": f.remediation_key,
                }
                for f in self.findings
            ],
        }


@dataclass
class ProbeContext:
    """Everything the probes need, injectable so each is unit-testable.

    Every collaborator is optional. A probe whose collaborator is missing
    reports what it can rather than skipping silently — a probe that disappears
    when its input is absent is indistinguishable from a probe that passed.
    """

    config: PorterConfig
    #: Resolved ffmpeg/ffprobe. Injectable so the "no ffmpeg" report can be
    #: exercised on a machine that has ffmpeg.
    tools: FFmpegTools | None = None
    runner: FFmpegRunner | None = None
    selector: EncoderSelector | None = None
    #: Tests pass a callable; production leaves it as ``shutil.which``.
    which: Any = shutil.which
    cpu_count: int | None = None

    def ffmpeg_runner(self) -> FFmpegRunner:
        if self.runner is None:
            self.runner = FFmpegRunner(self.tools or FFmpegTools.resolve())
        return self.runner


# ----------------------------------------------------------------------
# Probes: environment
# ----------------------------------------------------------------------


def probe_python(*, version_info: tuple[int, ...] | None = None) -> Finding:
    """Python 3.10+, which is what ``requires-python`` promises."""
    info = version_info or sys.version_info[:3]
    version = ".".join(str(part) for part in info)
    if info[:2] < (3, 10):
        return Finding.blocked(
            "python",
            "Python interpreter",
            f"Python {version} is below the supported floor",
            "python_version",
        )
    return Finding.passed("python", "Python interpreter", f"Python {version}")


def probe_ffmpeg(tools: FFmpegTools | None = None) -> Finding:
    """ffmpeg and ffprobe are on PATH. Blocks everything: no ffmpeg, no porter."""
    resolved = tools or FFmpegTools.resolve()
    absent = resolved.missing()
    if absent:
        # `missing()` reports the tool *kind* ("ffmpeg"), which is the stable
        # identifier but tells the operator nothing when the configured path was
        # a custom one. Add the path that was actually searched in that case.
        searched = ", ".join(str(getattr(resolved, kind)) for kind in absent)
        detail = f"not found on PATH: {', '.join(absent)}"
        if searched != ", ".join(absent):
            detail = f"{detail} (looked for: {searched})"
        return Finding.blocked("ffmpeg", "ffmpeg & ffprobe", detail, "ffmpeg")
    return Finding.passed("ffmpeg", "ffmpeg & ffprobe", "both binaries found on PATH")


def probe_ffmpeg_version(runner: FFmpegRunner) -> Finding:
    """Report the version, which is the first thing a bug report needs."""
    try:
        version = runner.version()
    except PorterError as exc:
        return Finding.degraded(
            "ffmpeg_version",
            "ffmpeg version",
            f"could not be read: {exc.message}",
            "ffmpeg",
        )
    return Finding.info("ffmpeg_version", "ffmpeg version", version or "unknown")


def probe_libass(runner: FFmpegRunner) -> Finding:
    """The ``ass`` filter exists, so hardsubbing will not silently degrade.

    v0.1 had a version of this check that searched ffmpeg's ``-filters`` output
    for the substring ``ass`` — which matches ``pass``, ``bass``, ``classes`` and
    ``atadenoise``. It reported success on builds with no libass at all. This
    asks ffmpeg for the filter by name.
    """
    try:
        present = runner.has_filter("ass")
    except PorterError as exc:
        return Finding.degraded(
            "libass",
            "libass (hardsub)",
            f"could not be determined: {exc.message}",
            "ffmpeg_libass",
        )
    if not present:
        return Finding.degraded(
            "libass",
            "libass (hardsub)",
            "this ffmpeg has no 'ass' filter, so subtitles cannot be burned in",
            "ffmpeg_libass",
        )
    return Finding.passed("libass", "libass (hardsub)", "the 'ass' filter is available")


def probe_font(config: PorterConfig, *, matcher: Any = None) -> Finding:
    """The configured Chinese font actually resolves.

    v0.1 checked nothing here. A missing CJK font does not fail — it renders
    every character as a tofu box, so the job completes and the video is
    unusable. That is worse than a crash, which is why this is checked.
    """
    family = config.style.zh_font
    match = matcher or _fc_match
    resolved = match(family)

    if resolved is None:
        return Finding.info(
            "font",
            "Chinese subtitle font",
            f"fontconfig not available; cannot verify '{family}'",
        )

    families = [part.strip() for part in resolved.split(",")]
    if any(family.lower() in candidate.lower() for candidate in families):
        return Finding.passed("font", "Chinese subtitle font", f"'{family}' resolves")

    return Finding.degraded(
        "font",
        "Chinese subtitle font",
        f"'{family}' is not installed; fontconfig substitutes '{families[0]}', "
        "which may render Chinese text as empty boxes",
        "subtitle_font",
    )


def probe_js_runtime(*, which: Any = None) -> Finding:
    """A JS runtime yt-dlp can drive.

    Required since yt-dlp 2025.11.12 for full YouTube extraction. Without one,
    extraction does not fail — it returns fewer formats, so the failure is
    invisible until a download is refused.
    """
    lookup = which or shutil.which
    for name in _JS_RUNTIMES:
        path = lookup(name)
        if path:
            note = _JS_RUNTIME_NOTES.get(name, "supported by yt-dlp")
            return Finding.passed("js_runtime", "JavaScript runtime", f"{name} at {path} ({note})")

    return Finding.degraded(
        "js_runtime",
        "JavaScript runtime",
        "none of deno/node/bun/quickjs found; YouTube extraction will silently "
        "yield fewer formats",
        "js_runtime",
    )


def probe_yt_dlp() -> Finding:
    """yt-dlp is importable, since that is how the engine calls it."""
    try:
        from yt_dlp import version
    except ImportError:
        return Finding.blocked(
            "yt_dlp",
            "yt-dlp",
            "the yt_dlp module is not importable",
            "yt_dlp",
        )
    return Finding.passed("yt_dlp", "yt-dlp", f"yt-dlp {version.__version__}")


def probe_output_dir(config: PorterConfig) -> Finding:
    """The output directory can actually be written to.

    Checked by writing, not by inspecting permission bits: POSIX mode bits do not
    account for read-only mounts, ACLs, a full disk, or WSL2's ``/mnt/c``
    translation, all of which reject a write that the mode bits allowed.

    The directory is **never created**. ``porter doctor`` is a read-only health
    check, and creating the output directory made it leave one behind in whatever
    the working directory happened to be: running the test suite created
    ``./porter_output`` in the repository root, 347 MB of unrelated earlier job
    output and all. When the directory does not exist yet, the nearest existing
    ancestor is probed instead -- that is the directory whose writability decides
    whether the output directory can be created at all, so the answer is the same
    and nothing is touched.
    """
    target = Path(config.output_dir)

    if target.is_dir():
        existing, note = target, ""
    else:
        ancestor = _nearest_existing(target)
        if ancestor is None:
            return Finding.blocked(
                "output_dir",
                "Output directory",
                f"neither {target} nor any parent directory exists",
                "output_dir",
            )
        if not ancestor.is_dir():
            return Finding.blocked(
                "output_dir",
                "Output directory",
                f"{ancestor} is not a directory, so {target} cannot be created",
                "output_dir",
            )
        existing, note = ancestor, f" (so {target} can be created)"

    probe_file = existing / ".porter-write-probe"
    try:
        probe_file.write_text("", encoding="utf-8")
    except OSError as exc:
        return Finding.blocked(
            "output_dir",
            "Output directory",
            f"{existing} is not writable: {exc.strerror or exc}",
            "output_dir",
        )
    finally:
        probe_file.unlink(missing_ok=True)

    return Finding.passed(
        "output_dir",
        "Output directory",
        f"{existing} is writable{note}",
    )


def _nearest_existing(path: Path) -> Path | None:
    """The closest of ``path`` and its parents that exists, or ``None``.

    Used instead of creating the missing directories, so a diagnostic never
    mutates the filesystem. Returns a file as readily as a directory: a caller
    that needs a directory has to say so, and the message it can then give
    ("X is not a directory") is more useful than a bare ``NotADirectoryError``.
    """
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def probe_encoder(selector: EncoderSelector) -> Finding:
    """Which encoder will be used, and why not the others.

    A software fallback is **INFO**, not a degradation: it works. Calling it
    degraded would be crying wolf, and would put "no GPU" in the same bucket as
    "no ffmpeg".
    """
    chosen = selector.select()
    if chosen.tier is HardwareTier.HARDWARE:
        return Finding.passed("encoder", "Video encoder", f"{chosen.label} (hardware)")

    rejected = [
        f"{profile.label}: {reason}"
        for profile, usable, reason in selector.report()
        if not usable
    ]
    detail = f"{chosen.label}; no hardware encoder available"
    if rejected:
        detail = f"{detail} ({'; '.join(rejected)})"
    return Finding.info("encoder", "Video encoder", detail)


def probe_asr_route(config: PorterConfig, *, which: Any = None) -> Finding:
    """Which speech-to-text path this configuration will take.

    A *configuration* probe, not a chain probe, and deliberately so: it answers
    the question an operator actually has — "will this spend money and how
    reliable is the path?" — without constructing a chain or a run context.

    The order mirrors :func:`porter.pipeline._default_transcriber`, because the
    route it names has to be the route a run takes:

    1. **Local Whisper** (``[asr-local]``) — no key, no network, no third party.
    2. **Whisper API** — keyed and documented.
    3. **VideoCaptioner CLI** — external GPL-3.0 process, local engines only.
    4. **Bcut / Google Web** — key-free and reverse-engineered. Both were
       measured returning empty results on 2026-09-22, so this is the
       branch that means "transcription will fail".

    Before the local backend existed there was no branch 1, and the wording here said the key-free
    endpoints were the only free option and that they do not transcribe — true
    then, and false once a local model is installed. A doctor that reports a
    stale fact is worse than one that reports nothing.
    """
    lookup = which or shutil.which

    if whisper_local.is_installed():
        model = config.asr.whisper_local_model or whisper_local.DEFAULT_MODEL
        cached = whisper_local.model_is_cached(model)
        detail = (
            f"local Whisper ({model}, "
            + ("already downloaded" if cached else "downloads on first use")
            + ") — no key, no network"
        )
        if config.asr.whisper_api_key or config.llm.api_key:
            detail += f", falling back to the Whisper API ({config.asr.whisper_model})"
        return Finding.passed("asr_route", "Speech-to-text route", detail)

    if config.asr.whisper_api_key or config.llm.api_key:
        return Finding.passed(
            "asr_route",
            "Speech-to-text route",
            f"Whisper API (model {config.asr.whisper_model})",
        )

    if lookup("videocaptioner") or (
        Path("~/.local/bin/videocaptioner").expanduser().is_file()
    ):
        return Finding.info(
            "asr_route",
            "Speech-to-text route",
            "the VideoCaptioner CLI, then the key-free Bcut / Google Web endpoints",
        )

    return Finding.info(
        "asr_route",
        "Speech-to-text route",
        "the key-free Bcut / Google Web endpoints, which are reverse-engineered "
        "and, as measured on 2026-09-22, do not transcribe at all (Google Web "
        "returns an empty result for every request). Set an LLM or Whisper API "
        "key, or install porter-workflow[asr-local] for offline transcription "
        "with no key, or transcription will fail",
    )


def probe_translation_route(config: PorterConfig) -> Finding:
    """Which translation path this configuration will take.

    Without an LLM key the chain uses Bing, Google and MyMemory — all key-free,
    all reverse-engineered. Reporting that as a plain pass would hide a real
    reliability difference, so it is reported as the fact it is.
    """
    if config.llm.api_key:
        return Finding.passed(
            "translation_route",
            "Translation route",
            f"LLM ({config.llm.model})",
        )

    return Finding.info(
        "translation_route",
        "Translation route",
        "no LLM key set; the key-free endpoints (Bing, Google, MyMemory) are "
        "reverse-engineered and unverified, with the VideoCaptioner CLI last",
    )


def probe_all(
    config: PorterConfig,
    *,
    context: ProbeContext | None = None,
) -> CapabilityReport:
    """Run every environment probe.

    Ordering is dependency order, not severity order: ffmpeg is probed before
    anything that needs to invoke it, so a machine with no ffmpeg gets one clear
    blocker instead of five confusing ones.
    """
    ctx = context or ProbeContext(config=config)

    ffmpeg = probe_ffmpeg(ctx.tools)
    findings: list[Finding] = [
        probe_python(),
        probe_yt_dlp(),
        ffmpeg,
        probe_output_dir(config),
    ]

    # Everything below needs ffmpeg to answer, and each would report a second,
    # misleading failure if ffmpeg were simply absent.
    if ffmpeg.ok:
        try:
            runner = ctx.ffmpeg_runner()
        except PorterError as exc:
            findings.append(
                Finding.blocked("ffmpeg", "ffmpeg & ffprobe", exc.message, "ffmpeg")
            )
        else:
            findings.append(probe_ffmpeg_version(runner))
            findings.append(probe_libass(runner))
            selector = ctx.selector or EncoderSelector(runner, cpu_count=ctx.cpu_count)
            findings.append(probe_encoder(selector))

    findings.append(probe_js_runtime(which=ctx.which))
    findings.append(probe_font(config))
    findings.append(probe_asr_route(config, which=ctx.which))
    findings.append(probe_translation_route(config))

    return CapabilityReport(findings=findings)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _fc_match(family: str) -> str | None:
    """Ask fontconfig which family it would use for ``family``.

    Returns the resolved family list, or ``None`` when fontconfig is unavailable.

    ``fc-match`` never fails: asked for a font it does not have, it silently
    returns a substitute. So the caller has to compare the answer against the
    request rather than trust the exit code.
    """
    if not shutil.which("fc-match"):
        return None

    result = _run_probe(["fc-match", "-f", "%{family}", family])
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _run_probe(
    cmd: list[str], timeout: float = _PROBE_TIMEOUT
) -> subprocess.CompletedProcess[str] | None:
    """Run a small helper binary. Returns ``None`` rather than raising.

    Probes must not be able to fail the report: their entire purpose is to
    describe a broken machine, and a probe that raises on a broken machine is
    useless exactly when it is needed.
    """
    try:
        return subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            # No probe reads stdin. Handing them the parent's stdin would let a
            # helper consume the MCP frontend's JSON-RPC channel.
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.debug("probe %s failed: %s", cmd[0], exc)
        return None
