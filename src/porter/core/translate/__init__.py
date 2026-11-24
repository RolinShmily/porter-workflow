"""Translation backends and execution wrapper.

``chain.py``
    :class:`TranslationChain` — implements :class:`~porter.core.ports.Translator`.
    Owns the **CJK self-check** that rejects an untranslated "fake hardsub".
    The workflow executor constructs single-capability chains with no runtime
    cross-capability fallback.
``llm.py``
    OpenAI-compatible chat completions (DeepSeek / GPT / Claude-compatible
    gateways). Requires the ``[llm]`` extra.
``bing.py`` / ``google.py``
    Key-free HTTP backends with their own anti-bot handling.
    **Unverified endpoints.**
``mymemory.py``
    Key-free HTTP backend over a *documented* API, unlike the two above.
``base.py``
    The backend protocol, the shared :class:`~porter.core.translate.base.TranslationOutcome`,
    and the alignment helper the chain applies.

"Unverified endpoint" is a claim about the *wire format*, not the structure.
``bing.py`` and ``google.py`` port v0.1's request/response handling for services
that were reverse-engineered rather than documented; they cannot be exercised in
an offline test suite and may already be dead. Their availability probes, error
mapping, timeouts, batching and cancellation handling are deliberate and tested.

## The one rule every backend obeys

``outcome.texts[i]`` is the translation of ``texts[i]``. Length and order are
preserved, and an untranslatable input is returned **in place** rather than
dropped. Dropping is the tempting shortcut and it silently misaligns every
subsequent cue, producing a file where each line is plausibly translated and
attached to the wrong moment of the video. The chain verifies the length and
rejects a backend that gets it wrong.
"""

from porter.core.translate.base import (
    MAX_TEXTS_PER_REQUEST,
    TranslationBackend,
    TranslationBackendError,
    TranslationOutcome,
)
from porter.core.translate.bing import BingTranslateBackend
from porter.core.translate.chain import TranslationChain
from porter.core.translate.google import GoogleTranslateBackend
from porter.core.translate.llm import LLMTranslationBackend
from porter.core.translate.mymemory import MyMemoryBackend

__all__ = [
    "MAX_TEXTS_PER_REQUEST",
    "BingTranslateBackend",
    "GoogleTranslateBackend",
    "LLMTranslationBackend",
    "MyMemoryBackend",
    "TranslationBackend",
    "TranslationBackendError",
    "TranslationChain",
    "TranslationOutcome",
]
