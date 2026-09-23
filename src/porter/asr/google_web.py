"""UNVERIFIED ENDPOINT. This ports v0.1's request/response handling for a
key-free endpoint that was reverse-engineered, not documented. It cannot be
exercised in an offline test suite and may already be dead. The structure
(availability probe, error mapping, cancellation, timeout) is deliberate and
tested; the wire format is best-effort and unvalidated.

Google's Web Speech endpoint via the ``speech_recognition`` package (the ``[stt]``
extra). The interesting part is not the HTTP call — it is the slicing.

v0.1 found that a single 40-minute request silently drops the tail, so it built a
soft VAD: ``ffmpeg silencedetect`` finds the quiet stretches, cuts at their
midpoints, and recursively sub-divides any resulting interval longer than eight
seconds. The cut boundaries are then padded by 150ms so the first and last
phonemes of a phrase are not clipped. That whole pipeline is preserved here
because it is the reason this backend is usable at all, not because the endpoint
is trusted.

The audio is never discarded: every second between 0 and the duration lands in
exactly one interval, even where no silence was detected.
"""

from __future__ import annotations

import http.client
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from porter.asr.base import AsrBackendError, AsrOutcome, coerce_items
from porter.context import RunContext
from porter.logging import get_logger
from porter.models.subtitle import SubtitleItem

__all__ = [
    "MAX_CHUNK_SECONDS",
    "PADDING_SECONDS",
    "SILENCE_FILTER",
    "GoogleWebBackend",
]

_logger = get_logger(__name__)

NAME = "google-web"

#: v0.1's VAD parameters, unchanged.
SILENCE_FILTER = "silencedetect=noise=-30dB:d=0.35"
#: Recursive slice ceiling. Longer than this and the endpoint truncates.
MAX_CHUNK_SECONDS = 8.0
#: Acoustic padding on each cut boundary, so phonemes are not clipped.
PADDING_SECONDS = 0.15
#: Intervals shorter than this are not worth a request.
MIN_INTERVAL_SECONDS = 0.2
#: ``Recognizer.record`` needs a positive duration; v0.1's floor.
MIN_CLIP_SECONDS = 0.5
#: ffmpeg/ffprobe are local and fast, but a stuck process must not hang a job.
TOOL_TIMEOUT = 300.0

_SILENCE_START = re.compile(r"silence_start:\s*([\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([\d.]+)")


def _load_sr() -> Any:
    """Import ``speech_recognition`` lazily.

    It lives in the ``[stt]`` extra; importing it at module scope would make the
    whole ASR package unimportable on a minimal install.
    """
    import speech_recognition as sr

    return sr


class GoogleWebBackend:
    """Google Web Speech with soft-VAD slicing."""

    name = NAME
    endpoint_verified = False

    def available(self, ctx: RunContext) -> bool:
        """Whether ``speech_recognition`` is importable. Never raises."""
        try:
            _load_sr()
        except ImportError:
            _logger.debug("speech_recognition is not installed; Google Web unavailable")
            return False
        except Exception:  # available() must never raise; log and degrade.
            _logger.error(
                "could not import speech_recognition; treating Google Web as unavailable",
                exc_info=True,
            )
            return False
        return True

    def transcribe(self, audio: Path, ctx: RunContext) -> AsrOutcome:
        """Slice ``audio`` at silences and recognise each slice.

        Raises:
            AsrBackendError: Missing SDK, unreadable audio, a failing ffmpeg
                probe, an endpoint error, or a run that yields no cues.
        """
        ctx.check_cancelled()
        try:
            sr = _load_sr()
        except ImportError as exc:
            raise AsrBackendError(
                self.name,
                "the 'SpeechRecognition' package is required for the Google Web "
                "backend; install porter-workflow[stt]",
            ) from exc

        if not audio.exists():
            raise AsrBackendError(self.name, f"audio file does not exist: {audio}")

        # Silencedetect first, then ffprobe: v0.1's order.
        starts, ends = self._detect_silences(audio, ctx)
        total_dur = self._duration(audio, ctx)
        intervals = self._slice_intervals(starts, ends, total_dur)

        recognizer = sr.Recognizer()
        lang = "zh-CN" if ctx.config.asr.language in ("zh", "zh-CN") else "en-US"
        items: list[SubtitleItem] = []

        for start_s, end_s in intervals:
            ctx.check_cancelled()
            record_start = max(0.0, start_s - PADDING_SECONDS)
            record_end = min(total_dur, end_s + PADDING_SECONDS)
            duration = max(MIN_CLIP_SECONDS, record_end - record_start)

            with sr.AudioFile(str(audio)) as source:
                audio_data = recognizer.record(source, offset=record_start, duration=duration)
                try:
                    text = recognizer.recognize_google(audio_data, language=lang)
                except sr.UnknownValueError:
                    # No speech in this slice. Expected: the VAD cut on silence,
                    # so a chunk containing only breath is a normal outcome.
                    _logger.debug("no speech recognised in %.2f-%.2f", start_s, end_s)
                    continue
                except sr.RequestError as exc:
                    # The endpoint refused us (quota, 429, 500, offline). v0.1
                    # swallowed this and returned whatever partial cues it had;
                    # the chain needs the reason instead, so the fallback engine
                    # is tried with an accurate log line.
                    raise AsrBackendError(
                        self.name,
                        f"Google Web Speech request failed: {exc}",
                    ) from exc
                except (http.client.HTTPException, OSError) as exc:
                    # Transport-level failure that speech_recognition does NOT
                    # wrap. It reads the response with ``response.read()`` on a
                    # chunked body, so a truncated response surfaces as
                    # ``http.client.IncompleteRead`` -- an HTTPException, not an
                    # OSError and not a RequestError.
                    #
                    # Found by running the real pipeline: the exception escaped
                    # this backend, escaped the chain's ``except
                    # (AsrBackendError, PorterError)``, and killed the process
                    # with a traceback. A backend's contract (see
                    # porter.asr.base) is that EVERY expected failure becomes an
                    # AsrBackendError, so the chain can move on.
                    raise AsrBackendError(
                        self.name,
                        f"Google Web Speech transport failed: {exc}",
                        kind=type(exc).__name__,
                    ) from exc

            if text and text.strip():
                items.append(
                    SubtitleItem(
                        index=len(items) + 1,
                        start_ms=int(start_s * 1000),
                        end_ms=int(end_s * 1000),
                        source_text=text.strip(),
                        target_text="",
                    )
                )

        cleaned = coerce_items(items)
        if not cleaned:
            raise AsrBackendError(self.name, "Google Web Speech produced no cues")

        return AsrOutcome(items=cleaned, used_asr=True, origin="google-web")

    # -- slicing ------------------------------------------------------------

    def _duration(self, audio: Path, ctx: RunContext) -> float:
        """Read the audio duration with ffprobe. Raises on an unreadable file."""
        cmd = [
            ctx.config.ffmpeg.ffprobe_path or "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio),
        ]
        proc = self._run_tool(cmd, what="ffprobe duration")
        raw = proc.stdout.strip()
        try:
            total = float(raw)
        except ValueError as exc:
            raise AsrBackendError(
                self.name,
                f"ffprobe returned no usable duration for {audio}: {raw!r}",
            ) from exc
        if not math.isfinite(total) or total <= 0.0:
            raise AsrBackendError(self.name, f"audio has no duration: {audio}")
        return total

    def _detect_silences(self, audio: Path, ctx: RunContext) -> tuple[list[float], list[float]]:
        """Run silencedetect and return the ``(starts, ends)`` it reported."""
        cmd = [
            ctx.config.ffmpeg.ffmpeg_path or "ffmpeg",
            "-i",
            str(audio),
            "-af",
            SILENCE_FILTER,
            "-f",
            "null",
            "-",
        ]
        proc = self._run_tool(cmd, what="ffmpeg silencedetect")
        starts = [float(m) for m in _SILENCE_START.findall(proc.stderr)]
        ends = [float(m) for m in _SILENCE_END.findall(proc.stderr)]
        return starts, ends

    def _slice_intervals(
        self, starts: list[float], ends: list[float], total_dur: float
    ) -> list[tuple[float, float]]:
        """Cut at silence midpoints, then sub-slice long intervals.

        The algorithm is v0.1's verbatim, including the ``0.5``-second inset at
        both ends and the ``>= 0.2s`` minimum interval.
        """
        cut_points: list[float] = [0.0]
        for silence_start, silence_end in zip(starts, ends, strict=False):
            midpoint = (silence_start + silence_end) / 2.0
            if 0.5 < midpoint < (total_dur - 0.5):
                cut_points.append(midpoint)
        cut_points.append(total_dur)
        cut_points = sorted(set(cut_points))

        raw_intervals: list[tuple[float, float]] = []
        for position in range(len(cut_points) - 1):
            start = cut_points[position]
            end = cut_points[position + 1]
            if end - start >= MIN_INTERVAL_SECONDS:
                raw_intervals.append((start, end))
        if not raw_intervals:
            raw_intervals = [(0.0, total_dur)]

        fine_intervals: list[tuple[float, float]] = []
        for start, end in raw_intervals:
            duration = end - start
            if duration <= MAX_CHUNK_SECONDS:
                fine_intervals.append((start, end))
                continue
            pieces = math.ceil(duration / MAX_CHUNK_SECONDS)
            piece_length = duration / pieces
            for piece in range(pieces):
                sub_start = start + piece * piece_length
                sub_end = min(end, start + (piece + 1) * piece_length)
                fine_intervals.append((sub_start, sub_end))
        return fine_intervals

    def _run_tool(self, cmd: list[str], *, what: str) -> subprocess.CompletedProcess[str]:
        """Run one ffmpeg/ffprobe invocation with a hard timeout.

        v0.1 called these with no timeout, so a stuck ffmpeg hung the job with no
        diagnostic. Cancellation is checked before and after the call; the
        process itself is short-lived and local.
        """
        try:
            # argv is built here from config paths plus the audio path: no shell.
            return subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=TOOL_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise AsrBackendError(
                self.name,
                f"{Path(cmd[0]).name} is not installed or not on PATH (needed for {what})",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AsrBackendError(
                self.name,
                f"{what} timed out after {TOOL_TIMEOUT:.0f}s",
            ) from exc
