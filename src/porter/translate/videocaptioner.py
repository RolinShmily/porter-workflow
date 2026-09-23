"""Adapter around the external ``videocaptioner`` CLI.

``videocaptioner`` is a third-party command-line tool, not a Python library. It
is GPL-3.0 and pins ``python<3.13``, so it can never be a declared dependency of
this MIT project — the only way to use it is as a subprocess the user installed
themselves. :meth:`VideocaptionerBackend.available` is therefore the honest
probe it looks like: "is the binary on this machine?", nothing more.

Two variants exist because v0.1 had two code paths:

* :class:`VideocaptionerBackend` drives the tool's *free* translators (``bing``
  or ``google``) and needs no credentials.
* :class:`VideocaptionerLLMBackend` drives the tool's ``llm`` translator and
  forwards the configured API key, base URL and model.

The tool's interface is file-based (it takes an input SRT and writes an output
SRT), while the backend contract is a list of strings. :meth:`translate_texts`
bridges that: it writes the cues to a temporary SRT, runs the CLI, parses the
result, and — critically — checks that the output has exactly as many cues as
the input. A mismatch raises :class:`TranslationBackendError` instead of
returning a shorter list that would misalign every later cue.

What is preserved from v0.1: the subcommand and flags, ``--target-language
zh-Hans`` and ``--layout target-above`` (both hardcoded there, kept here), the
``--no-optimize --no-split`` flags, the 60 s timeout, and the binary search order
(``PATH`` first, then ``~/.local/bin``).

What changed: cancellation is checked before the run, the binary path is
injectable for tests, and a non-zero exit or a cue-count mismatch is reported as
a backend failure rather than a silent ``False``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from porter.context import RunContext
from porter.logging import get_logger
from porter.subtitles.srt import parse_srt
from porter.translate.base import TranslationBackendError, TranslationOutcome
from porter.utils.time import ms_to_srt_time

__all__ = ["VideocaptionerBackend", "VideocaptionerLLMBackend"]

_logger = get_logger(__name__)

_BINARY_NAME = "videocaptioner"
_TIMEOUT_SECONDS = 60.0

#: Milliseconds between the synthetic cues written to the temporary SRT. The CLI
#: only uses the text, but it still requires a well-formed SRT to read.
_CUE_STEP_MS = 2000
_CUE_DURATION_MS = 1800


def _get_videocaptioner_bin() -> str | None:
    """Resolve the ``videocaptioner`` binary, mirroring v0.1's search order."""
    found = shutil.which(_BINARY_NAME)
    if found:
        return found
    user_bin = Path.home() / ".local" / "bin" / _BINARY_NAME
    if user_bin.is_file() and os.access(user_bin, os.X_OK):
        return str(user_bin)
    return None


def _srt_for(texts: list[str]) -> str:
    """Render ``texts`` as a single-language SRT with synthetic timings.

    Newlines inside a cue are collapsed, because a multi-line cue is ambiguous
    when it is read back: :func:`~porter.subtitles.srt.parse_srt` cannot tell a
    wrapped line from a second language.
    """
    blocks: list[str] = []
    for position, text in enumerate(texts, start=1):
        start_ms = (position - 1) * _CUE_STEP_MS
        end_ms = start_ms + _CUE_DURATION_MS
        single_line = " ".join(text.split())
        blocks.append(
            f"{position}\n{ms_to_srt_time(start_ms)} --> {ms_to_srt_time(end_ms)}\n{single_line}"
        )
    return "\n\n".join(blocks) + "\n"


class _VideocaptionerBase:
    """Shared subprocess plumbing for the two CLI variants."""

    name = "videocaptioner"
    endpoint_verified = True

    def __init__(self, binary: str | None = None) -> None:
        #: An injected path bypasses the PATH lookup entirely (tests use this).
        self._binary = binary

    # -- probe --------------------------------------------------------------

    def available(self, ctx: RunContext) -> bool:
        """Whether the binary can be resolved. Never raises."""
        try:
            return self._resolve_binary() is not None
        except Exception:
            _logger.error("videocaptioner availability probe raised", exc_info=True)
            return False

    # -- translation --------------------------------------------------------

    def translate_texts(
        self,
        texts: list[str],
        target_lang: str,
        ctx: RunContext,
    ) -> TranslationOutcome:
        """Translate ``texts`` through the CLI, preserving order and length.

        Raises:
            TranslationBackendError: When the binary is missing, the CLI fails,
                or its output has a different number of cues than the input.
        """
        if not texts:
            return TranslationOutcome(texts=[], origin=self.name)

        ctx.check_cancelled()
        binary = self._resolve_binary()
        if binary is None:
            raise TranslationBackendError(
                self.name, "the videocaptioner binary was not found on PATH or in ~/.local/bin"
            )

        active = [index for index, text in enumerate(texts) if text.strip()]
        if not active:
            return TranslationOutcome(texts=list(texts), origin=self.name)

        with tempfile.TemporaryDirectory(prefix="porter-videocaptioner-") as workdir:
            input_srt = Path(workdir) / "in.srt"
            output_srt = Path(workdir) / "out.srt"
            input_srt.write_text(_srt_for([texts[index] for index in active]), encoding="utf-8")

            if not self.translate_file(input_srt, output_srt, ctx):
                raise TranslationBackendError(
                    self.name, "videocaptioner did not produce a usable output SRT"
                )

            try:
                output_text = output_srt.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise TranslationBackendError(
                    self.name, "videocaptioner output could not be read", reason=str(exc)
                ) from exc

        parsed = parse_srt(output_text)
        if len(parsed) != len(active):
            raise TranslationBackendError(
                self.name,
                "videocaptioner returned the wrong number of cues",
                expected=len(active),
                got=len(parsed),
            )

        results = list(texts)
        for index, item in zip(active, parsed, strict=True):
            results[index] = item.target_text.strip() or texts[index]
        return TranslationOutcome(texts=results, origin=self.name)

    def translate_file(self, input_srt: Path, output_srt: Path, ctx: RunContext) -> bool:
        """Translate an SRT file, v0.1's unit of work.

        Kept public because it is the shape the CLI actually has, and because the
        chain may one day hand it a file directly. Returns ``False`` for an
        expected failure (missing binary, non-zero exit, empty output); the
        string-level wrapper turns that into :class:`TranslationBackendError`.
        """
        binary = self._resolve_binary()
        if binary is None:
            return False
        return self._run(binary, input_srt, output_srt, ctx)

    # -- internals ----------------------------------------------------------

    def _resolve_binary(self) -> str | None:
        if self._binary is not None:
            return self._binary
        return _get_videocaptioner_bin()

    def _extra_args(self, ctx: RunContext) -> list[str]:
        """Translator-specific flags; overridden by the LLM variant."""
        raise NotImplementedError

    def _run(self, binary: str, input_srt: Path, output_srt: Path, ctx: RunContext) -> bool:
        command = [
            binary,
            "subtitle",
            str(input_srt),
            "-o",
            str(output_srt),
            "--format",
            "srt",
            "--target-language",
            "zh-Hans",
            "--layout",
            "target-above",
            *self._extra_args(ctx),
        ]

        try:
            completed = subprocess.run(  # noqa: S603 - argv list, no shell
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            _logger.warning("videocaptioner timed out after %.0fs: %s", _TIMEOUT_SECONDS, exc)
            return False
        except OSError as exc:
            _logger.warning("videocaptioner could not be executed: %s", exc)
            return False

        if completed.returncode != 0:
            _logger.warning(
                "videocaptioner exited with %d: %s",
                completed.returncode,
                (completed.stderr or "").strip(),
            )
            return False

        return output_srt.exists() and output_srt.stat().st_size > 0


class VideocaptionerBackend(_VideocaptionerBase):
    """Drive the CLI's free translators (``bing`` or ``google``)."""

    name = "videocaptioner"
    endpoint_verified = True

    def __init__(self, binary: str | None = None, engine: str = "bing") -> None:
        super().__init__(binary)
        self._engine = engine

    def _extra_args(self, ctx: RunContext) -> list[str]:
        return ["--translator", self._engine, "--no-optimize", "--no-split"]


class VideocaptionerLLMBackend(_VideocaptionerBase):
    """Drive the CLI's ``llm`` translator, forwarding the configured credentials."""

    name = "videocaptioner-llm"
    endpoint_verified = True

    def available(self, ctx: RunContext) -> bool:
        """Binary present *and* an API key configured. Never raises."""
        try:
            if not ctx.config.llm.api_key:
                return False
            return super().available(ctx)
        except Exception:
            _logger.error("videocaptioner-llm availability probe raised", exc_info=True)
            return False

    def _extra_args(self, ctx: RunContext) -> list[str]:
        args = ["--translator", "llm", "--api-key", ctx.config.llm.api_key or ""]
        if ctx.config.llm.api_base:
            args.extend(["--api-base", ctx.config.llm.api_base])
        if ctx.config.llm.model:
            args.extend(["--model", ctx.config.llm.model])
        return args
