"""OpenAI-compatible Whisper transcription (the ``[llm]`` extra).

This is the highest-quality backend: a documented, paid API with punctuation and
correct proper nouns. v0.1 called it from a 700-line controller and handed back a
bare ``bool``; here it is one engine among the chain's candidates.

Key resolution follows v0.1 exactly and in this order, because operators have
configured all three over the years:

1. ``config.asr.whisper_api_key``
2. ``config.llm.api_key`` (a single key shared with the translation backend)
3. the ``OPENAI_API_KEY`` environment variable

The OpenAI SDK is imported lazily: it lives in the ``[llm]`` extra, and the
engine must load without it so that ``available()`` can report ``False`` instead
of tearing down the process. The client itself is injectable so tests exercise
the request shape with no network.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from porter.asr.base import AsrBackendError, AsrOutcome, parse_srt_items
from porter.config import PorterConfig
from porter.context import RunContext
from porter.errors import JobCancelled
from porter.logging import get_logger

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "WhisperApiBackend",
]

_logger = get_logger(__name__)

#: v0.1's defaults. Kept verbatim; changing them would silently move every
#: operator onto a different model or endpoint.
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "whisper-1"
DEFAULT_TIMEOUT = 120.0

NAME = "whisper-api"


def _load_openai() -> Any:
    """Import the OpenAI SDK lazily.

    A module-level import would make ``import porter.asr.whisper_api`` fail on an
    installation without the ``[llm]`` extra, which in turn would make the whole
    ASR package unimportable. The engine prefers structural absence over a
    hard dependency here.
    """
    from openai import OpenAI

    return OpenAI


def _api_key(config: PorterConfig) -> str | None:
    """Resolve the key with v0.1's precedence."""
    return (
        config.asr.whisper_api_key
        or config.llm.api_key
        or os.environ.get("OPENAI_API_KEY")
    )


def _base_url(config: PorterConfig) -> str:
    """Resolve the endpoint with v0.1's precedence."""
    return (
        config.asr.whisper_api_base
        or config.llm.api_base
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )


class WhisperApiBackend:
    """Whisper via any OpenAI-compatible ``/audio/transcriptions`` endpoint.

    Args:
        client: An already-constructed SDK client, or ``None`` to build one from
            configuration. Injection exists so tests never touch the network and
            so a host embedding porter can supply its own transport.
    """

    name = NAME
    endpoint_verified = True

    def __init__(self, client: object | None = None) -> None:
        self._client = client

    # -- availability -------------------------------------------------------

    def available(self, ctx: RunContext) -> bool:
        """Whether a key is resolvable and the SDK importable. Never raises.

        An injected client short-circuits both checks: the caller has already
        decided the transport is usable.
        """
        if self._client is not None:
            return True
        if not _api_key(ctx.config):
            return False
        try:
            _load_openai()
        except ImportError:
            _logger.debug("openai is not installed; Whisper API backend is unavailable")
            return False
        except Exception:  # available() must never raise; log and degrade.
            # A broken install must report "unavailable", not abort the chain.
            _logger.error(
                "could not import the openai SDK; treating Whisper API as unavailable",
                exc_info=True,
            )
            return False
        return True

    # -- transcription ------------------------------------------------------

    def transcribe(self, audio: Path, ctx: RunContext) -> AsrOutcome:
        """POST ``audio`` to ``/audio/transcriptions`` and parse the SRT reply.

        Raises:
            AsrBackendError: No key, missing SDK, or an API/transport failure.
        """
        ctx.check_cancelled()
        config = ctx.config
        model = config.asr.whisper_model or DEFAULT_MODEL
        # Any: the injectable client is duck-typed (the SDK is optional), so its
        # concrete type is unknowable here.
        client: Any = self._client

        if client is None:
            api_key = _api_key(config)
            if not api_key:
                raise AsrBackendError(
                    self.name,
                    "no Whisper API key configured "
                    "(set asr.whisper_api_key, llm.api_key, or OPENAI_API_KEY)",
                )
            try:
                openai_cls = _load_openai()
            except ImportError as exc:
                raise AsrBackendError(
                    self.name,
                    "the 'openai' package is required for the Whisper API backend; "
                    "install porter-workflow[llm]",
                ) from exc
            client = openai_cls(
                api_key=api_key,
                base_url=_base_url(config),
                timeout=DEFAULT_TIMEOUT,
            )

        ctx.check_cancelled()
        try:
            with audio.open("rb") as audio_file:
                # response_format="srt" is deliberate: it makes the endpoint do
                # the segmentation, which v0.1 relied on.
                response = client.audio.transcriptions.create(
                    model=model,
                    file=audio_file,
                    response_format="srt",
                )
        except (AsrBackendError, JobCancelled):
            raise
        except Exception as exc:  # the SDK's exception classes differ across
            # major versions and the client is injectable, so the concrete type
            # cannot be named. The failure is expected (429, quota, bad key) and
            # is mapped, not swallowed; exc_info=True satisfies BLE001.
            _logger.error("Whisper API request failed: %s", exc, exc_info=True)
            raise AsrBackendError(
                self.name,
                f"Whisper API request failed: {exc}",
                model=model,
            ) from exc

        text = str(response) if response else ""
        items = parse_srt_items(text)
        if not items:
            raise AsrBackendError(
                self.name,
                "Whisper API answered without parseable SRT",
                model=model,
            )

        ctx.check_cancelled()
        return AsrOutcome(items=items, used_asr=True, origin=f"whisper:{model}")
