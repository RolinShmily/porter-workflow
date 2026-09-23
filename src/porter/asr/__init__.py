"""Speech-to-text backends and the fallback chain.

``chain.py``
    :class:`AsrChain` — implements :class:`~porter.ports.Transcriber`. Takes the
    platform's own subtitle track when PREPARE fetched one, and otherwise walks
    the engine chain, skipping backends whose ``available()`` is ``False``.
``platform_subs.py``
    Reads that fetched track. Deliberately **not** a backend: it is a file, with
    no engine to probe and no ordering question.
``whisper_api.py``
    OpenAI-compatible Whisper endpoint. Requires the ``[llm]`` extra.
``whisper_local.py``
    ★ Local Whisper through ``faster-whisper`` (MIT, no PyTorch). Requires the
    ``[asr-local]`` extra. The only backend that needs no key, no network and no
    third-party service, and the only one whose ``endpoint_verified`` is true by
    construction rather than by measurement.
``bcut.py``
    Bilibili Bcut. Key-free, chunked upload, its own session so a configured proxy
    cannot stall the upload. **Unverified endpoint.**
``google_web.py``
    Google Web STT with recursive VAD slicing, so long dialogue does not lose its
    tail. Requires the ``[stt]`` extra. **Unverified endpoint.**
``videocaptioner.py``
    ★ Optional adapter around the **external** ``videocaptioner`` CLI.
    GPL-3.0 and ``python<3.13``, therefore never a declared dependency and only
    ever reached through ``subprocess``. An absent binary drops the backend from
    the chain without a user-visible warning.
``base.py``
    The backend protocol, the shared :class:`~porter.asr.base.AsrOutcome`, and the
    cue helpers every backend needs.

"Unverified endpoint" is a claim about the *wire format*, not the structure. Those
two modules port v0.1's request/response handling for services that were
reverse-engineered rather than documented; they cannot be exercised in an offline
test suite and may already be dead. Their availability probes, error mapping,
timeouts and cancellation handling are deliberate and tested.
"""

from porter.asr.base import AsrBackend, AsrBackendError, AsrOutcome
from porter.asr.bcut import BcutBackend
from porter.asr.chain import AsrChain
from porter.asr.google_web import GoogleWebBackend
from porter.asr.platform_subs import load_platform_subtitles
from porter.asr.videocaptioner import VideoCaptionerBackend
from porter.asr.whisper_api import WhisperApiBackend
from porter.asr.whisper_local import WhisperLocalBackend

__all__ = [
    "AsrBackend",
    "AsrBackendError",
    "AsrChain",
    "AsrOutcome",
    "BcutBackend",
    "GoogleWebBackend",
    "VideoCaptionerBackend",
    "WhisperApiBackend",
    "WhisperLocalBackend",
    "load_platform_subtitles",
]
