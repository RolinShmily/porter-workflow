"""Media handling: the engine's ffmpeg boundary.

``ffmpeg.py``
    :class:`~porter.media.ffmpeg.FFmpegRunner` — the only place a subprocess is
    spawned, with ``-nostdin`` and stderr-tail error reporting enforced.
``probe.py``
    :class:`~porter.media.probe.MediaInfo` — a typed view over ffprobe, where
    unknown dimensions stay ``None`` instead of becoming ``1920x1080``.
``standardize.py``
    Downloaded file -> ``raw/video.mp4`` and ``raw/audio.wav``.
``enhance.py``
    ``raw/audio.wav`` -> ``raw/audio_enhanced.wav`` for ASR only.

``encode.py``
    Encoder selection by *test encoding one frame* rather than by reading
    ``-encoders`` or looking for a device node. Implemented in P3.1; see the
    module docstring for the measurements behind it.

``burn.py``
    libass hardsub rendering from the master into ``cooked/``. Runs ffmpeg with
    ``cwd`` set to the subtitle's directory so an arbitrary user output path
    never enters a filtergraph argument; see that module's docstring for the
    measurements showing why escaping cannot do the job.

Nothing here prints; see :mod:`porter.media.ffmpeg`.
"""

from porter.media.burn import (
    FfmpegRenderer,
    burn_hardsub,
    escape_ffmpeg_filter_path,
    render_release,
)
from porter.media.encode import (
    HARDWARE_PROFILES,
    EncoderProfile,
    EncoderSelector,
    HardwareTier,
    detect_encoder,
    software_profile_for,
)
from porter.media.enhance import ASR_FILTER_CHAIN, enhance_for_asr
from porter.media.ffmpeg import FFmpegRunner, FFmpegTools
from porter.media.probe import (
    MediaInfo,
    dimensions,
    find_downloaded_video,
    is_valid_video,
    probe,
)
from porter.media.standardize import extract_audio, standardize_video

__all__ = [
    "ASR_FILTER_CHAIN",
    "HARDWARE_PROFILES",
    "EncoderProfile",
    "EncoderSelector",
    "FFmpegRunner",
    "FFmpegTools",
    "FfmpegRenderer",
    "HardwareTier",
    "MediaInfo",
    "burn_hardsub",
    "detect_encoder",
    "dimensions",
    "enhance_for_asr",
    "escape_ffmpeg_filter_path",
    "extract_audio",
    "find_downloaded_video",
    "is_valid_video",
    "probe",
    "render_release",
    "software_profile_for",
    "standardize_video",
]
