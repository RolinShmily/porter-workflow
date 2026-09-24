"""Local Whisper transcription via ``faster-whisper`` (the ``[asr-local]`` extra).

The only ASR backend in the chain that needs **no key, no network and no third
party service**: the model runs on this machine, so a transcription is
reproducible, unmetered and immune to an endpoint being withdrawn. Every other
key-free backend porter has (``bcut``, ``google_web``) is a reverse-engineered
HTTP endpoint, and both were measured returning empty results on 2026-09-22.

## Why not VideoCaptioner, which does the same thing

VideoCaptioner is GPL-3.0 and pins ``python<3.13``, so it can never be a
dependency of this MIT project; porter reaches it only as an external process
(``asr/videocaptioner.py``). Of the engines that CLI offers, ``bijian`` and
``jianying`` are *online* reverse-engineered endpoints -- ``bijian`` is the very
service ``asr/bcut.py`` speaks, which is the one already measured dead -- and
only ``whisper-cpp`` is genuinely local. So this module implements the *idea*
(local Whisper, no key) on a permissively licensed stack rather than borrowing
GPL code: ``faster-whisper`` is MIT and its CTranslate2 backend needs no
PyTorch, which keeps the extra around 100 MB instead of ~2 GB.

## Why it runs first in the chain

``Pipeline._default_transcriber`` puts this backend ahead of the paid API. The
v0.1 ordering rationale (quality, speed) was written when the key-free endpoints
worked; they no longer do, so the first backend in the chain should be the one
most likely to *succeed*. A user who prefers the paid endpoint can say so
explicitly with ``--asr-engine whisper-api``, which is a better contract than an
implicit "if a key happens to be set, reorder everything".

## What ``endpoint_verified`` means here

The field asks whether the wire protocol was checked against the live service.
A local model has no wire protocol and no remote counterpart that can change
under it, so ``True`` is not a claim about a test run -- it is the structural
property that makes this backend worth having. It is what lets ``porter_plan``
finally report a feasible ASR route without a caveat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from porter.asr.base import AsrBackendError, AsrOutcome, coerce_items
from porter.context import RunContext
from porter.errors import JobCancelled
from porter.logging import get_logger
from porter.models.subtitle import SubtitleItem

__all__ = [
    "DEFAULT_COMPUTE_TYPE",
    "DEFAULT_DEVICE",
    "DEFAULT_MODEL",
    "NAME",
    "WhisperLocalBackend",
    "is_installed",
    "model_is_cached",
]

_logger = get_logger(__name__)

NAME = "whisper-local"

#: faster-whisper's default is "small" rather than "base": on the sizes measured
#: here the small model is a large quality step for roughly 3x the runtime, and a
#: subtitle track with wrong words is worse than one that took two minutes.
DEFAULT_MODEL = "small"

#: ``auto`` tries CUDA first and falls back to CPU. An explicit device is obeyed
#: exactly, with no silent fallback -- naming a device is a request.
DEFAULT_DEVICE = "auto"
DEFAULT_COMPUTE_TYPE = "auto"

#: Compute types for the two auto attempts. ``float16`` is the CUDA default and
#: ``int8`` the CPU one; both come from faster-whisper's own guidance.
_CUDA_COMPUTE_TYPE = "float16"
_CPU_COMPUTE_TYPE = "int8"


def _load_faster_whisper() -> Any:
    """Import :class:`faster_whisper.WhisperModel` lazily.

    A module-level import would make ``import porter.asr.whisper_local`` fail on
    an installation without the ``[asr-local]`` extra, which would make the whole
    ASR package unimportable. Same reason as ``whisper_api``'s lazy ``openai``.
    """
    from faster_whisper import WhisperModel

    return WhisperModel


def _load_download_model() -> Any:
    """Import faster-whisper's own model resolver, lazily.

    Used instead of reaching into ``huggingface_hub`` directly: it is the same
    library that resolves the model at load time, so it knows the size-to-repo
    mapping, honours ``HF_HUB_CACHE`` and treats a partial download as absent.
    Rolling that by hand would be a second, drift-prone copy of the rule -- and it
    would pull ``huggingface_hub``'s stubs (and through them numpy's 3.12-only
    ones) into the mypy graph for nothing.
    """
    from faster_whisper.utils import download_model

    return download_model


def is_installed() -> bool:
    """Whether the ``[asr-local]`` extra is importable. Never raises.

    Separate from :meth:`WhisperLocalBackend.available` because ``porter doctor``
    asks this as a *configuration* question and deliberately does not build a
    :class:`~porter.context.RunContext` to ask it.
    """
    try:
        _load_faster_whisper()
    except ImportError:
        return False
    except Exception:  # a probe must never raise
        _logger.error("could not import faster-whisper", exc_info=True)
        return False
    return True


def model_is_cached(model: str) -> bool:
    """Whether ``model`` is already on disk. Never raises, never networks.

    ``local_files_only=True`` is the point of this function: it answers "could
    this run with the network unplugged?" without triggering the download that
    the answer is supposed to describe. ``porter doctor`` uses it so the first
    run's cost is stated up front instead of discovered.

    A partial download counts as absent, which is the behaviour that matters:
    ``faster-whisper-medium`` was found on the machine this was written on with
    its ``config.json`` but no ``model.bin``, and treating that as cached would
    promise an offline run that cannot happen.
    """
    try:
        download_model = _load_download_model()
    except ImportError:
        return False
    except Exception:  # a probe must never raise
        _logger.debug("could not import faster-whisper's model resolver", exc_info=True)
        return False
    try:
        download_model(model, local_files_only=True)
    except Exception:  # not cached, unreadable cache, unknown size -- all a miss
        _logger.debug("model %r is not in the local cache", model, exc_info=True)
        return False
    return True


class WhisperLocalBackend:
    """Speech recognition with a local faster-whisper model.

    Args:
        model: An explicit model name or repo id, or ``None`` to take it from
            configuration. Injection exists so tests never load weights and so a
            host embedding porter can pin its own model.
        loader: The model factory, or ``None`` to import faster-whisper at call
            time. Tests pass a fake; production passes nothing.
    """

    name = NAME

    #: Local inference has no remote protocol to drift, so this is permanently
    #: true rather than "someone checked once". See the module docstring.
    endpoint_verified = True

    def __init__(self, model: str | None = None, loader: Any | None = None) -> None:
        self._model = model
        self._loader = loader

    # -- availability -------------------------------------------------------

    def available(self, ctx: RunContext) -> bool:
        """Whether faster-whisper is importable. Never raises, never networks.

        Deliberately *not* "the model is already downloaded". A first run that
        has the package but not the weights can still succeed by fetching them,
        and reporting unavailable there would make the chain skip the one backend
        that works and fall through to endpoints already measured dead -- the
        worst possible answer. The download is instead announced by
        :meth:`transcribe`, and :func:`model_is_cached` lets ``porter doctor``
        report it ahead of time.
        """
        if self._loader is not None:
            return True
        return is_installed()

    # -- transcription ------------------------------------------------------

    def transcribe(self, audio: Path, ctx: RunContext) -> AsrOutcome:
        """Recognise ``audio`` on this machine.

        The audio is handed over whole. ``CHUNK_SECONDS`` exists to keep uploads
        under a cloud endpoint's timeout; faster-whisper segments internally and
        slices on silence, so cutting the file first would only break sentences
        across chunk boundaries and put a seam in the timings.

        Raises:
            AsrBackendError: No package, no model, an unusable device, or a
                decode failure. The chain then tries the next backend.
        """
        ctx.check_cancelled()
        if not audio.exists():
            raise AsrBackendError(self.name, f"audio file does not exist: {audio}")

        model_name = self._model or ctx.config.asr.whisper_local_model or DEFAULT_MODEL
        language = ctx.config.asr.language
        attempts = self._attempts(ctx)
        failures: list[str] = []

        for device, compute_type in attempts:
            ctx.check_cancelled()
            if not self._loader and not model_is_cached(model_name):
                # Stated before the wait, not after: a multi-hundred-megabyte
                # download that looks like a hang is its own defect report.
                _logger.info(
                    "local Whisper model %r is not cached; downloading it now "
                    "(this happens once per model)",
                    model_name,
                )
            try:
                model = self._build(model_name, device, compute_type)
            except (AsrBackendError, JobCancelled):
                raise
            except Exception as exc:  # load failures are expected: a missing
                # cuDNN, an absent model, a corrupt cache. Mapped, not swallowed;
                # exc_info=True keeps the traceback and satisfies BLE001.
                _logger.warning(
                    "could not load local Whisper model %r on %s/%s: %s",
                    model_name,
                    device,
                    compute_type,
                    exc,
                    exc_info=True,
                )
                failures.append(f"{device}/{compute_type}: {exc}")
                continue

            try:
                items = self._recognise(model, audio, language, ctx)
            except (AsrBackendError, JobCancelled):
                raise
            except Exception as exc:  # inference failures are expected too:
                # a decode error, an OOM on the device, a corrupt cache.
                # exc_info=True keeps the traceback and satisfies BLE001.
                _logger.warning(
                    "local Whisper inference failed on %s/%s: %s",
                    device,
                    compute_type,
                    exc,
                    exc_info=True,
                )
                failures.append(f"{device}/{compute_type}: {exc}")
                continue

            if not items:
                failures.append(f"{device}/{compute_type}: no speech recognised")
                continue

            return AsrOutcome(
                items=items,
                used_asr=True,
                origin=f"faster-whisper:{model_name}:{device}",
            )

        raise AsrBackendError(
            self.name,
            f"local Whisper could not transcribe {audio.name} "
            f"(model={model_name}); install the model or set asr.whisper_local_model",
            failures=failures,
        )

    # -- internals ----------------------------------------------------------

    def _attempts(self, ctx: RunContext) -> list[tuple[str, str]]:
        """The ``(device, compute_type)`` pairs to try, in order.

        ``auto`` means CUDA then CPU: the CUDA path is much faster but needs
        cuDNN/cuBLAS, which are frequently absent (they are on the machine this
        was written on), and a device probe cannot see that reliably -- only
        actually constructing the model can. An explicitly configured device is
        tried once, alone, because a user who names a device does not want it
        silently swapped.
        """
        device = (ctx.config.asr.whisper_local_device or DEFAULT_DEVICE).strip().lower()
        compute_type = (
            ctx.config.asr.whisper_local_compute_type or DEFAULT_COMPUTE_TYPE
        ).strip().lower()

        if device == "auto":
            preferred = (
                (_CUDA_COMPUTE_TYPE,) if compute_type == "auto" else (compute_type,)
            )
            return [
                ("cuda", preferred[0]),
                ("cpu", _CPU_COMPUTE_TYPE),
            ]
        if compute_type == "auto":
            return [(device, _CUDA_COMPUTE_TYPE if device == "cuda" else _CPU_COMPUTE_TYPE)]
        return [(device, compute_type)]

    def _build(self, model_name: str, device: str, compute_type: str) -> Any:
        """Construct the model, importing faster-whisper if it was not injected."""
        loader = self._loader
        if loader is None:
            try:
                loader = _load_faster_whisper()
            except ImportError as exc:
                raise AsrBackendError(
                    self.name,
                    "the 'faster-whisper' package is required for local transcription; "
                    "install porter-workflow[asr-local]",
                ) from exc
        return loader(model_name, device=device, compute_type=compute_type)

    def _recognise(
        self,
        model: Any,
        audio: Path,
        language: str,
        ctx: RunContext,
    ) -> list[SubtitleItem]:
        """Run inference and turn segments into cues.

        ``vad_filter`` drops silence and music before decoding, which is what
        keeps a video with a long intro from producing a wall of hallucinated
        cues. The generator is consumed here rather than returned, so that a
        decode error surfaces inside this backend's error handling instead of
        inside whoever iterates it next.
        """
        wanted = None if not language or language == "auto" else language
        segments, _info = model.transcribe(
            str(audio),
            language=wanted,
            vad_filter=True,
        )

        items: list[SubtitleItem] = []
        for segment in segments:
            ctx.check_cancelled()
            text = str(getattr(segment, "text", "") or "").strip()
            if not text:
                continue
            start_ms = round(float(segment.start) * 1000)
            end_ms = round(float(segment.end) * 1000)
            items.append(
                SubtitleItem(
                    index=len(items) + 1,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    source_text=text,
                    target_text="",
                )
            )

        # coerce_items renumbers and drops degenerate cues. The renumbering
        # matters because the generators write ``index`` into the SRT.
        return coerce_items(items)
