"""Translation backends and the fallback chain.

``chain.py``
    :class:`TranslationChain` — implements :class:`~porter.ports.Translator`.
    Walks LLM -> Bing -> Google -> MyMemory -> ``videocaptioner`` and owns the
    **CJK self-check** that rejects an untranslated "fake hardsub".
``llm.py``
    OpenAI-compatible chat completions (DeepSeek / GPT / Claude-compatible
    gateways). Requires the ``[llm]`` extra.
``bing.py`` / ``google.py``
    Key-free HTTP backends with their own anti-bot handling.
    **Unverified endpoints.**
``mymemory.py``
    Key-free HTTP backend over a *documented* API, unlike the two above.
``videocaptioner.py``
    ★ Optional adapter around the **external** ``videocaptioner`` CLI.
    Same GPL-3.0 / ``python<3.13`` constraints as the ASR adapter: subprocess
    only, never a declared dependency.
``base.py``
    The backend protocol, the shared :class:`~porter.translate.base.TranslationOutcome`,
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

from porter.translate.base import (
    MAX_TEXTS_PER_REQUEST,
    TranslationBackend,
    TranslationBackendError,
    TranslationOutcome,
)
from porter.translate.bing import BingTranslateBackend
from porter.translate.chain import TranslationChain
from porter.translate.google import GoogleTranslateBackend
from porter.translate.llm import LLMTranslationBackend
from porter.translate.mymemory import MyMemoryBackend
from porter.translate.videocaptioner import (
    VideocaptionerBackend,
    VideocaptionerLLMBackend,
)

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
    "VideocaptionerBackend",
    "VideocaptionerLLMBackend",
]
