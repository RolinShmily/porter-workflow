"""The only place in the engine that spawns ``ffmpeg`` or ``ffprobe``.

Two process-safety rules are enforced here rather than at every call site,
because both failures are silent, expensive, and were present in ``v0.1``.

Rule 1 — never let ffmpeg touch stdin
-------------------------------------
In the MCP frontend stdin carries the JSON-RPC transport, so a child that
consumed it would steal protocol bytes.

**Measured, not assumed:** ffmpeg gates interactive stdin on ``tcgetattr(0)``,
which fails for a pipe, so on Linux with piped stdin it does *not* read fd 0.
Feeding a sentinel down a pipe and running a real encode leaves the sentinel
intact even without ``-nostdin`` — see
``tests/integration/test_media_pipeline.py::TestStdinIsolation``.

That measurement is **not** a reason to omit the flag:

* the guard is POSIX-specific; Windows builds have no ``tcgetattr``,
* an MCP client may hand the server a pty rather than a pipe,
* ``-nostdin`` is what ffmpeg's own documentation tells non-interactive callers
  to pass, so correctness does not depend on that internal detail staying put.

So both belts are applied and the cost is one flag: ``-nostdin`` in the argv and
``stdin=subprocess.DEVNULL`` on the process. Note this is defence in depth, not
a live bug being fixed — the distinction matters when reading the commit.

Rule 2 — report the *end* of stderr, and always report it
--------------------------------------------------------
ffmpeg prints its banner and stream mapping first and the actual error last.
``v0.1`` did ``proc.stderr[:200]``, which truncated the useful part away and
surfaced the version string instead of the cause. Failures here log the tail and
raise :class:`~porter.errors.MediaError` with it attached, so the operator sees
``"Unknown encoder 'h264_nvenc'"`` rather than ``"ffmpeg version 9.0.1 ..."``.

Nothing in this module prints: stdout belongs to the caller (see
``porter_cli.render`` for the only writers).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from porter.errors import CapabilityMissingError, MediaError
from porter.logging import get_logger

__all__ = ["FFmpegRunner", "FFmpegTools"]

_logger = get_logger(__name__)

#: Seconds to wait for a probe. Probes are metadata reads; anything slower is a
#: broken file or a stuck network mount.
PROBE_TIMEOUT = 30.0

#: How many trailing stderr characters to keep in an error. Enough for the
#: message and the stream context above it, small enough for a log line.
_STDERR_TAIL = 2000


@dataclass(frozen=True)
class FFmpegTools:
    """Resolved ffmpeg/ffprobe executables."""

    ffmpeg: str
    ffprobe: str

    @classmethod
    def resolve(
        cls,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ) -> FFmpegTools:
        """Locate both tools on PATH, keeping the configured names in the error.

        A configured absolute path that exists is used as-is; otherwise PATH is
        searched. Falls back to the bare name so ``FileNotFoundError`` still
        names the tool rather than showing an empty string.
        """
        return cls(
            ffmpeg=_resolve_one(ffmpeg),
            ffprobe=_resolve_one(ffprobe),
        )

    def missing(self) -> tuple[str, ...]:
        """Which of the two tools are not runnable."""
        absent = []
        if shutil.which(self.ffmpeg) is None and not Path(self.ffmpeg).is_file():
            absent.append("ffmpeg")
        if shutil.which(self.ffprobe) is None and not Path(self.ffprobe).is_file():
            absent.append("ffprobe")
        return tuple(absent)

    def require(self) -> FFmpegTools:
        """Raise :class:`CapabilityMissingError` unless both tools are present.

        Called before a long job rather than after hours of downloading, because
        "ffmpeg is missing" is cheap to detect now and expensive to discover when
        the burn phase starts.
        """
        absent = self.missing()
        if absent:
            raise CapabilityMissingError(
                "ffmpeg",
                f"required binary not found on PATH: {', '.join(absent)}",
                tools=list(absent),
            )
        return self


def _resolve_one(name: str) -> str:
    if Path(name).is_file():
        return name
    return shutil.which(name) or name


class FFmpegRunner:
    """Runs ffmpeg/ffprobe with the engine's safety rules applied."""

    def __init__(self, tools: FFmpegTools | None = None) -> None:
        self.tools = tools or FFmpegTools.resolve()

    # ------------------------------------------------------------------
    # Generic invocation
    # ------------------------------------------------------------------

    def run(
        self,
        args: list[str],
        *,
        what: str,
        check: bool = True,
        timeout: float | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one ffmpeg invocation.

        Args:
            args: Arguments *after* the leading ``-nostdin``.
            what: Short description used in the error message, e.g.
                ``"audio extraction"``.
            check: Raise on a non-zero exit. Set False only when the caller
                treats failure as a normal outcome.
            timeout: Seconds. ``None`` means no limit, which is correct for
                encoding and wrong for probing.
            cwd: Working directory for the child. Set this to keep arbitrary
                user paths **out of filtergraph arguments** -- see
                :func:`porter.media.burn.escape_ffmpeg_filter_path` for why that
                matters. Relative paths in ``args`` resolve against it.

        Returns:
            The completed process with ``stdout``/``stderr`` captured as text.

        Raises:
            CapabilityMissingError: ffmpeg is not installed.
            MediaError: The command failed and ``check`` was True.
        """
        cmd = [self.tools.ffmpeg, "-nostdin", *args]
        return self._spawn(cmd, what=what, check=check, timeout=timeout, cwd=cwd)

    def _spawn(
        self,
        cmd: list[str],
        *,
        what: str,
        check: bool,
        timeout: float | None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        _logger.debug("running %s: %s", what, " ".join(cmd))

        try:
            # argv is assembled here from internal call sites plus an
            # FFmpegTools path: no shell, no user-supplied string.
            proc = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
                # Rule 1: see the module docstring. DEVNULL is the belt to
                # -nostdin's braces.
                stdin=subprocess.DEVNULL,
                cwd=str(cwd) if cwd is not None else None,
            )
        except FileNotFoundError as exc:
            tool = Path(cmd[0]).name
            raise CapabilityMissingError(
                tool,
                f"{tool} is not installed or not on PATH (needed for {what})",
                command=cmd[0],
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaError(
                f"{what} timed out after {timeout:.0f}s",
                what=what,
                timeout=timeout,
            ) from exc

        if check and proc.returncode != 0:
            raise MediaError(
                f"{what} failed (exit {proc.returncode}): {_tail(proc.stderr)}",
                what=what,
                exit_code=proc.returncode,
                stderr=_tail(proc.stderr),
                command=cmd,
                # Recorded because a failure inside a filtergraph is often about
                # where the relative path resolved, not about the path text.
                cwd=str(cwd) if cwd is not None else None,
            )

        # ffmpeg warns about recoverable oddities on successful runs too; keep
        # them at debug so a normal job's log stays readable.
        if proc.stderr:
            _logger.debug("%s stderr: %s", what, _tail(proc.stderr, 500))

        return proc

    # ------------------------------------------------------------------
    # Probing
    # ------------------------------------------------------------------

    def probe_json(self, path: Path, args: list[str]) -> dict[str, Any]:
        """Run ffprobe with ``-of json`` and parse the result.

        Returns an empty dict when the file is unreadable, because callers use
        probing to *decide* something (is this H.264? how tall is it?) and a
        failure is a legitimate answer there. Use
        :meth:`porter.media.probe.probe` for a typed view.
        """
        cmd = [
            self.tools.ffprobe,
            "-v",
            "error",
            "-of",
            "json",
            *args,
            str(path),
        ]
        proc = self._spawn(
            cmd,
            what=f"probing {path.name}",
            check=False,
            timeout=PROBE_TIMEOUT,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            _logger.debug("probe of %s produced nothing: %s", path, _tail(proc.stderr, 300))
            return {}

        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            _logger.warning("ffprobe returned malformed JSON for %s", path)
            return {}

        return parsed if isinstance(parsed, dict) else {}

    def probe_streams(self, path: Path) -> list[dict[str, Any]]:
        """All streams in ``path`` with codec and dimension fields."""
        data = self.probe_json(
            path,
            ["-show_entries", "stream=codec_type,codec_name,width,height,duration"],
        )
        streams = data.get("streams")
        return [s for s in streams if isinstance(s, dict)] if isinstance(streams, list) else []

    def probe_format(self, path: Path) -> dict[str, Any]:
        """Container-level fields (duration, format name, size)."""
        data = self.probe_json(
            path,
            ["-show_entries", "format=duration,format_name,size,bit_rate"],
        )
        fmt = data.get("format")
        return fmt if isinstance(fmt, dict) else {}

    # ------------------------------------------------------------------
    # Capability detection
    # ------------------------------------------------------------------

    def has_filter(self, name: str) -> bool:
        """Whether this build ships the named filter (``subtitles``, ``ass``)."""
        proc = self._spawn(
            [self.tools.ffmpeg, "-hide_banner", "-filters"],
            what="listing filters",
            check=False,
            timeout=PROBE_TIMEOUT,
        )
        return proc.returncode == 0 and any(
            f" {name} " in line for line in proc.stdout.splitlines()
        )

    def has_encoder(self, name: str) -> bool:
        """Whether the encoder is *compiled in*. Says nothing about runnability.

        On WSL2 every distro ffmpeg lists ``h264_nvenc`` while ``/dev/nvidia*``
        does not exist; the encoder only fails at encode time. Use
        :meth:`try_encoder` before committing to a multi-hour job.
        """
        proc = self._spawn(
            [self.tools.ffmpeg, "-hide_banner", "-encoders"],
            what="listing encoders",
            check=False,
            timeout=PROBE_TIMEOUT,
        )
        return proc.returncode == 0 and any(
            line.split()[1:2] == [name] for line in proc.stdout.splitlines() if line.strip()
        )

    def try_encoder(self, name: str, encoder_args: list[str]) -> bool:
        """Encode a single synthetic frame to prove the encoder really works.

        This is the only reliable test: it exercises the driver, not just the
        build configuration, and it costs about 100ms instead of failing at the
        end of an otherwise complete job.
        """
        proc = self.run(
            [
                "-hide_banner",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=256x144:d=0.04",
                "-frames:v",
                "1",
                "-c:v",
                name,
                *encoder_args,
                "-f",
                "null",
                "-",
            ],
            what=f"test-encoding with {name}",
            check=False,
            timeout=PROBE_TIMEOUT,
        )
        return proc.returncode == 0

    def version(self) -> str:
        """First line of ``ffmpeg -version``, or an empty string."""
        proc = self._spawn(
            [self.tools.ffmpeg, "-version"],
            what="reading the version",
            check=False,
            timeout=PROBE_TIMEOUT,
        )
        lines = proc.stdout.splitlines()
        return lines[0].strip() if lines else ""


def _tail(text: str | None, limit: int = _STDERR_TAIL) -> str:
    """Last ``limit`` characters of ``text``, stripped.

    Tail rather than head: ffmpeg puts the cause at the end. ``v0.1`` sliced from
    the front and consequently reported the version banner instead of the error.
    """
    if not text:
        return ""
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"...{cleaned[-limit:]}"
