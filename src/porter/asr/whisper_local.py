"""Local Whisper transcription via ``faster-whisper`` (the ``[asr-local]`` extra).

The only ASR backend in the chain that needs **no key, no network and no third
party service**: the model runs on this machine, so a transcription is
reproducible, unmetered and immune to an endpoint being withdrawn.

## Why not VideoCaptioner, which does the same thing

VideoCaptioner is GPL-3.0 and pins ``python<3.13``, so it can never be a
dependency of this MIT project; porter reaches it only as an external process
(``asr/videocaptioner.py``). This module implements the idea
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

import time
from pathlib import Path
from typing import Any

import platformdirs
import requests

from porter import mirrors
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

#: The files faster-whisper reads from a local model directory. Taken from a
#: working Hugging Face snapshot of ``Systran/faster-whisper-small``, which holds
#: exactly these four and nothing else -- no ``preprocessor_config.json``. The
#: ModelScope copy has the same names and the same byte counts.
_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")

#: ModelScope serves repository files from this path; ``master`` is its default
#: branch. Direct HTTP rather than the ``modelscope`` package, which would be a
#: large dependency for four downloads.
_MODELSCOPE_FILE_URL = "{endpoint}/api/v1/models/{repo}/repo?Revision=master&FilePath={path}"

#: One mebibyte: small enough that a cancellation is felt at once, large enough
#: that a 480 MB model is not 480,000 Python-level iterations.
_CHUNK_BYTES = 1 << 20

#: No timeout on the transfer itself -- a 480 MB file is slow by nature -- but a
#: hard one on connecting and on silence between chunks, so a dead mirror fails
#: in seconds instead of hanging the job.
_CONNECT_TIMEOUT_SECONDS = 15
_READ_TIMEOUT_SECONDS = 60


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


def _modelscope_root() -> Path:
    """Where ModelScope copies live: the same cache directory the registry uses."""
    return Path(platformdirs.user_cache_dir("porter", appauthor=False)) / "modelscope"


def modelscope_model_dir(model: str) -> Path | None:
    """Where a ModelScope copy of ``model`` belongs, or ``None`` if unmirrored.

    A path, not a decision. Whether to *use* it depends on
    :func:`porter.mirrors.use_china_mirrors`, which is the caller's question.
    """
    repo = mirrors.modelscope_whisper_repo(model)
    if repo is None:
        return None
    return _modelscope_root() / repo.replace("/", "--")


def _model_dir_is_complete(directory: Path) -> bool:
    """Whether every file faster-whisper needs is present and non-empty.

    Non-empty, not merely present: an interrupted download leaves a truncated
    ``model.bin``, and counting that as cached would surface much later as a
    loading error far from its cause. Same rule the Hugging Face path follows.
    """
    return all(
        (directory / name).is_file() and (directory / name).stat().st_size > 0
        for name in _MODEL_FILES
    )


def _download_file(url: str, destination: Path, ctx: RunContext) -> None:
    """Stream ``url`` to ``destination``, resuming a partial download.

    The bytes land in a ``.part`` file that is renamed only once complete, so a
    crash or a cancellation never leaves a short file where a whole one belongs.
    Resuming matters here because a 480 MB transfer over a link that drops is
    otherwise restarted from zero on every attempt.
    """
    partial = destination.with_name(f"{destination.name}.part")
    sent = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={sent}-"} if sent else {}

    with requests.get(
        url,
        stream=True,
        headers=headers,
        timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
    ) as response:
        if sent and response.status_code == 200:
            # The server ignored the range and is sending the whole file, so what
            # is already on disk is not a prefix of it: start over rather than
            # appending to bytes that do not line up.
            sent = 0
        response.raise_for_status()
        with partial.open("ab" if sent else "wb") as handle:
            for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                # Between chunks, so cancelling a 480 MB download is felt while
                # it is happening rather than after it finishes.
                ctx.check_cancelled()
                handle.write(chunk)

    partial.replace(destination)


def _fetch_model_files(repo: str, directory: Path, ctx: RunContext) -> None:
    """Fetch every missing file of ``repo`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in _MODEL_FILES:
        destination = directory / name
        if destination.is_file() and destination.stat().st_size > 0:
            continue
        url = _MODELSCOPE_FILE_URL.format(
            endpoint=mirrors.MODELSCOPE_ENDPOINT, repo=repo, path=name
        )
        _download_file(url, destination, ctx)


def download_from_modelscope(model: str, ctx: RunContext) -> Path | None:
    """Ensure the ModelScope copy of ``model`` is on disk; ``None`` to use HF.

    ``None`` means "carry on with the official source", and it is returned both
    for a size ModelScope does not mirror and for a download that failed. A
    failure is logged rather than raised: the mirror is a preference, and a
    university service being down must not be worse for the user than never
    having tried it. ``JobCancelled`` is the exception -- a cancel is the user's
    instruction, not a mirror problem.
    """
    directory = modelscope_model_dir(model)
    if directory is None:
        return None
    if _model_dir_is_complete(directory):
        return directory

    repo = mirrors.modelscope_whisper_repo(model)
    if repo is None:  # unreachable: modelscope_model_dir checked the same thing
        return None

    started = time.monotonic()
    try:
        _fetch_model_files(repo, directory, ctx)
    except JobCancelled:
        raise
    except (requests.RequestException, OSError) as exc:
        _logger.warning(
            "could not fetch model %r from ModelScope (%s); falling back to the "
            "official source",
            model,
            exc,
        )
        return None

    size = sum((directory / name).stat().st_size for name in _MODEL_FILES)
    _logger.info(
        "downloaded %s from ModelScope (%.0f MB in %.1fs)",
        repo,
        size / (1 << 20),
        time.monotonic() - started,
    )
    return directory


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
    # The ModelScope copy counts only when it is the copy a run would actually
    # use. With the mirrors switched off the same files sit on disk unused, and
    # answering "cached" for them would turn into a download the moment the job
    # started.
    if mirrors.use_china_mirrors():
        local = modelscope_model_dir(model)
        if local is not None and _model_dir_is_complete(local):
            return True

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

        # From China the weights come from ModelScope, a domestic mirror of the
        # same files; anywhere else -- or if that fails, or if a loader was
        # supplied and therefore owns model acquisition -- this stays the size
        # name and faster-whisper fetches it from Hugging Face exactly as before.
        #
        # ``is_installed`` fixes the order of the two failures: 480 MB of weights
        # must not be fetched only to discover afterwards that the extra is
        # missing. It also keeps the mirror a replacement for a download that was
        # going to happen anyway, never a new one.
        model_ref: str = model_name
        if self._loader is None and is_installed() and mirrors.use_china_mirrors():
            mirrored = download_from_modelscope(model_name, ctx)
            if mirrored is not None:
                model_ref = str(mirrored)

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
                model = self._build(model_ref, device, compute_type)
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
