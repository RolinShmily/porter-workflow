"""Vocal enhancement for ASR only.

The filter chain is the three-in-one broadcast-style treatment ``v0.1`` used,
kept byte-identical because it was tuned by ear against real ASR failure cases:

``highpass=f=80,lowpass=f=8000``
    Band-limit to the speech band. Removes mains rumble, handling noise and mic
    pops below 80 Hz, and hiss above 8 kHz — neither carries phonemes, and both
    cost recognition accuracy.
``afftdn=nf=-25``
    Adaptive FFT denoise. No model files, no external dependency, and it tracks
    stationary noise (fans, air conditioning) rather than assuming it.
``dynaudnorm=f=150:g=15:p=0.95``
    Dynamic normalisation. ASR backends are trained on consistent levels, so the
    quiet half of a video where someone speaks away from the mic needs lifting
    until it sits near the loud half.

The enhanced WAV feeds **only** ASR. The released video keeps the original audio
by stream copy, so this processing never reaches a viewer's ears — which is what
makes an aggressive denoise acceptable here.
"""

from __future__ import annotations

from pathlib import Path

from porter.logging import get_logger
from porter.media.ffmpeg import FFmpegRunner

__all__ = ["ASR_FILTER_CHAIN", "enhance_for_asr"]

_logger = get_logger(__name__)

#: Kept verbatim from v0.1; see the module docstring for what each stage does.
ASR_FILTER_CHAIN = "highpass=f=80,lowpass=f=8000,afftdn=nf=-25,dynaudnorm=f=150:g=15:p=0.95"

#: Output format for the enhanced WAV: same as the input, so a later stage can
#: swap the two files without caring which one it holds.
_SAMPLE_RATE = 16000


def enhance_for_asr(
    runner: FFmpegRunner,
    source: Path,
    dest: Path,
) -> Path | None:
    """Write an ASR-optimised copy of ``source`` to ``dest``.

    Returns ``dest`` on success and ``None`` on failure. Failure is deliberately
    non-fatal: the enhancement is an accuracy *optimisation*, and the unenhanced
    16 kHz WAV is always a valid ASR input. Aborting a job because a denoise
    filter was unavailable would trade a working transcript for none.

    Args:
        runner: ffmpeg runner.
        source: ``raw/audio.wav``.
        dest: ``raw/audio_enhanced.wav``.

    Returns:
        ``dest`` if it was produced with content, else ``None``.
    """
    if not source.is_file() or source.stat().st_size == 0:
        _logger.debug("skipping enhancement: %s is missing or empty", source)
        return None

    proc = runner.run(
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-af",
            ASR_FILTER_CHAIN,
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(_SAMPLE_RATE),
            "-ac",
            "1",
            str(dest),
        ],
        what="enhancing the audio for ASR",
        # Non-fatal by design: see the docstring.
        check=False,
    )

    if proc.returncode != 0:
        _logger.warning(
            "audio enhancement failed; ASR will use the unenhanced WAV "
            "(this costs accuracy, not correctness)"
        )
        return None

    if not dest.is_file() or dest.stat().st_size == 0:
        _logger.warning("audio enhancement produced an empty file; ignoring it")
        return None

    _logger.debug("ASR audio ready: %s", dest.name)
    return dest
