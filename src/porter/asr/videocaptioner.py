"""Adapter for the **external** ``videocaptioner`` CLI.

VideoCaptioner is GPL-3.0 and pins ``python<3.13``, so it can never be a declared
dependency of an MIT project that supports 3.13 — and importing it would drag its
licence into ours. It is therefore reached exclusively through ``subprocess``:
porter shells out to a binary the user installed, exactly as a user would.

Absent binary means the backend simply reports ``available() == False`` and the
chain skips it silently. That is deliberate: a missing optional tool is not a
warning-level event, and v0.1 printed a line about it on every single run.

The CLI writes an SRT file, so the adapter gives it a scratch path inside a
temporary directory, parses what it wrote, and returns cues. v0.1 wrote into the
caller's output path and returned a bare ``bool``; the parsed items are the
chain's actual contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from porter.asr.base import AsrBackendError, AsrOutcome, parse_srt_items
from porter.context import RunContext
from porter.logging import get_logger

__all__ = [
    "BINARY_NAME",
    "DEFAULT_ENGINE",
    "ENGINES",
    "TIMEOUT_SECONDS",
    "VideoCaptionerBackend",
]

_logger = get_logger(__name__)

NAME = "videocaptioner"
BINARY_NAME = "videocaptioner"

#: v0.1's engine names. ``bijian`` (BiJian) is only tried when explicitly
#: configured; the other two form the fallback order.
ENGINES = ("bijian", "jianying", "whisper-cpp")
DEFAULT_ENGINE = "jianying"
FALLBACK_ENGINES = ("jianying", "whisper-cpp")

#: v0.1's 180-second budget per engine invocation.
TIMEOUT_SECONDS = 180.0
#: Cancellation poll granularity. Small enough that Ctrl-C is felt immediately,
#: large enough not to spin.
CANCEL_POLL_SECONDS = 0.2


def _resolve_binary() -> str | None:
    """Find the binary on ``PATH`` or in ``~/.local/bin``.

    v0.1's two-step lookup, preserved: ``pipx`` and ``pip install --user`` both
    land in ``~/.local/bin``, which is frequently absent from a non-login
    shell's ``PATH``.
    """
    found = shutil.which(BINARY_NAME)
    if found:
        return found
    candidate = _user_bin_dir() / BINARY_NAME
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _user_bin_dir() -> Path:
    """The per-user bin directory. Indirection exists so tests can redirect it."""
    return Path.home() / ".local" / "bin"


class VideoCaptionerBackend:
    """Run the external ``videocaptioner transcribe`` CLI.

    Args:
        binary: Explicit binary path, or ``None`` to resolve one at call time.
            Injection keeps the tests free of a real installation.
    """

    name = NAME
    endpoint_verified = True

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary

    # -- availability -------------------------------------------------------

    def available(self, ctx: RunContext) -> bool:
        """Whether a binary is resolvable. Never raises."""
        if self._binary:
            return True
        try:
            return _resolve_binary() is not None
        except OSError as exc:
            _logger.warning("could not probe for the videocaptioner binary: %s", exc)
            return False
        except Exception:  # available() must never raise; log and degrade.
            _logger.error(
                "unexpected error probing for the videocaptioner binary",
                exc_info=True,
            )
            return False

    # -- transcription ------------------------------------------------------

    def transcribe(self, audio: Path, ctx: RunContext) -> AsrOutcome:
        """Invoke the CLI for each candidate engine until one yields cues.

        Raises:
            AsrBackendError: No binary, or every engine invocation failed.
        """
        ctx.check_cancelled()
        binary = self._binary or _resolve_binary()
        if not binary:
            raise AsrBackendError(
                self.name,
                "the videocaptioner binary was not found on PATH or in ~/.local/bin",
            )
        if not audio.exists():
            raise AsrBackendError(self.name, f"audio file does not exist: {audio}")

        failures: list[str] = []
        with tempfile.TemporaryDirectory(prefix="porter-videocaptioner-") as scratch:
            for engine in self._engine_order(ctx):
                ctx.check_cancelled()
                output = Path(scratch) / f"{engine}.srt"
                cmd = [
                    binary,
                    "transcribe",
                    str(audio),
                    "-o",
                    str(output),
                    "--format",
                    "srt",
                    "--asr",
                    engine,
                ]
                language = ctx.config.asr.language
                if language and language != "auto":
                    cmd.extend(["--language", language])

                returncode = self._run_cli(cmd, ctx, engine=engine)
                if returncode != 0:
                    failures.append(f"{engine}: exit {returncode}")
                    continue
                if not output.is_file() or output.stat().st_size == 0:
                    failures.append(f"{engine}: wrote no output")
                    continue

                items = parse_srt_items(output.read_text(encoding="utf-8", errors="replace"))
                if not items:
                    failures.append(f"{engine}: output was not parseable SRT")
                    continue

                return AsrOutcome(
                    items=items,
                    used_asr=True,
                    origin=f"videocaptioner:{engine}",
                )

        raise AsrBackendError(
            self.name,
            "every videocaptioner engine failed",
            failures=failures,
        )

    # -- internals ----------------------------------------------------------

    def _engine_order(self, ctx: RunContext) -> list[str]:
        """Configured engine first, then v0.1's fallback order."""
        configured = ctx.config.asr.engine
        primary = configured if configured in ENGINES else DEFAULT_ENGINE
        return [primary, *(engine for engine in FALLBACK_ENGINES if engine != primary)]

    def _run_cli(self, cmd: list[str], ctx: RunContext, *, engine: str) -> int:
        """Run one CLI invocation, cancelling promptly and enforcing the budget.

        v0.1 used ``subprocess.run(..., timeout=180)`` and could not be
        interrupted, so Ctrl-C waited for the engine. Polling in short ticks lets
        a cancel kill the child and raise :class:`~porter.errors.JobCancelled`
        within :data:`CANCEL_POLL_SECONDS`.
        """
        _logger.debug("running videocaptioner engine %s: %s", engine, " ".join(cmd))
        try:
            # argv is assembled here from a resolved binary path and internal
            # arguments: no shell, no user-supplied string.
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise AsrBackendError(
                self.name,
                f"the videocaptioner binary disappeared before it could run: {cmd[0]}",
            ) from exc
        except OSError as exc:
            raise AsrBackendError(
                self.name,
                f"videocaptioner could not be started: {exc}",
            ) from exc

        started = time.monotonic()
        try:
            while proc.poll() is None:
                if ctx.cancel.wait(CANCEL_POLL_SECONDS):
                    proc.kill()
                    proc.wait()
                    ctx.check_cancelled()
                if time.monotonic() - started > TIMEOUT_SECONDS:
                    proc.kill()
                    proc.wait()
                    raise AsrBackendError(
                        self.name,
                        f"videocaptioner engine '{engine}' timed out after "
                        f"{TIMEOUT_SECONDS:.0f}s",
                    )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        _, stderr = proc.communicate()
        if proc.returncode != 0:
            _logger.warning(
                "videocaptioner engine %s exited %d: %s",
                engine,
                proc.returncode,
                stderr.strip()[:500],
            )
        return proc.returncode if proc.returncode is not None else -1
