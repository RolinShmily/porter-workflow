"""OpenAI-compatible chat-completions translation backend.

This is the only backend that is not key-free, and the only one that can *repair*
the source while translating it: the prompt asks the model to fix ASR typos and
return idiomatic Chinese, which is why it sits first in the fallback chain when a
key is configured.

It is also the backend that v0.1's length bug hit hardest. v0.1 returned ``[]``
from the middle of a multi-batch run whenever one request failed or one response
was not a JSON array, so the caller got a *shorter* list than it passed in and
every later cue was written against the wrong translation. Here a failed batch
raises :class:`TranslationBackendError` and the chain moves to the next engine.

What is preserved from v0.1:

* The system/user prompt text, ``temperature=0.3``, batch size 20, and the
  30 s client timeout.
* ``json_repair`` is still used to salvage slightly malformed JSON, and the model
  is still asked for ``id``/``en``/``zh`` objects.
* The API key comes from ``config.llm.api_key`` (falling back to
  ``OPENAI_API_KEY``); the base URL from ``config.llm.api_base`` (falling back to
  ``OPENAI_BASE_URL``, then ``https://api.openai.com/v1``).

What changed, deliberately:

* The OpenAI client is injectable. Production passes nothing and a real
  ``openai.OpenAI`` is built lazily; tests pass a fake, so no test touches the
  network and the ``openai`` import is not even required to test this module.
* Responses are keyed by the input's position, not by the sentence id, because
  this backend works on plain strings. A reordered or partially repaired JSON
  array therefore cannot silently shift translations: each entry is looked up by
  its own id, and a missing id falls back to the original text in place.
* A request failure, a non-array response, or a missing ``json_repair`` raises
  :class:`TranslationBackendError` instead of returning a short list.
* Cancellation is checked before every batch.
"""

from __future__ import annotations

import json
import os
from typing import Any

from porter.context import RunContext
from porter.logging import get_logger
from porter.translate.base import TranslationBackendError, TranslationOutcome

__all__ = ["LLMTranslationBackend", "effective_llm_model"]

_logger = get_logger(__name__)

#: v0.1's batch size. The prompt asks for refined English *and* Chinese per
#: sentence, so a batch is heavier than the key-free backends' string lists.
_BATCH_SIZE = 20

_CLIENT_TIMEOUT_SECONDS = 30.0
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_SYSTEM_PROMPT = "You are a professional video transcript translator. Respond ONLY in valid JSON array."


def effective_llm_model(ctx: RunContext) -> str:
    """The LLM model to use: the per-job override, else the configured default.

    ``--llm-model`` / ``porter_job_start(llm_model=...)`` was another field both
    frontends accepted and no engine read, so the flag was a silent no-op and the
    only way to change the model was editing the config file. Two backends read
    this value -- the LLM translator and the ``videocaptioner-llm`` adapter, which
    passes it on as ``--model`` -- so the resolution lives here rather than being
    duplicated and drifting.
    """
    return ctx.options.llm_model or ctx.config.llm.model


def _load_openai() -> Any:
    """Return ``openai.OpenAI``, or ``None`` when the extra is not installed."""
    try:
        from openai import OpenAI
    except ImportError:
        return None
    return OpenAI


def _load_json_repair() -> Any:
    """Return the ``json_repair`` module, or ``None`` when it is not installed."""
    try:
        import json_repair
    except ImportError:
        return None
    return json_repair


def _openai_error_types() -> tuple[type[BaseException], ...]:
    """The base class of every ``openai`` SDK error, when the extra is present.

    ``openai.OpenAIError`` is what a 429/500/timeout surfaces as, and it is *not*
    an ``OSError``, so it has to be caught explicitly or the backend would let a
    rate limit escape as an unexpected exception instead of a
    :class:`TranslationBackendError` the chain can fall through on.
    """
    try:
        import openai
    except ImportError:
        return ()
    return (openai.OpenAIError,)


class LLMTranslationBackend:
    """Translate with any OpenAI-compatible chat-completions endpoint."""

    name = "llm"
    endpoint_verified = True

    def __init__(self, client: Any = None) -> None:
        #: ``None`` means "build a real client from the config on first use".
        self._client = client

    # -- probe --------------------------------------------------------------

    def available(self, ctx: RunContext) -> bool:
        """Whether a client (injected or buildable) exists. Never raises.

        An injected client is considered available unconditionally: it is a test
        seam, and asking it to prove an API key would make the seam useless.
        """
        try:
            if self._client is not None:
                return True
            if not self._api_key(ctx):
                return False
            return _load_openai() is not None
        except Exception:
            _logger.error("llm availability probe raised", exc_info=True)
            return False

    # -- translation --------------------------------------------------------

    def translate_texts(
        self,
        texts: list[str],
        target_lang: str,
        ctx: RunContext,
    ) -> TranslationOutcome:
        """Translate ``texts`` in batches, preserving order and length.

        Raises:
            TranslationBackendError: When the client cannot be built, a request
                fails, or a response is not a JSON array.
        """
        if not texts:
            return TranslationOutcome(texts=[], origin=self.name)

        client = self._client_for(ctx)
        model = effective_llm_model(ctx)

        translated: list[str] = []
        refined: list[str] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            ctx.check_cancelled()
            batch = texts[start : start + _BATCH_SIZE]
            batch_zh, batch_en = self._translate_batch(client, model, batch, start)
            translated.extend(batch_zh)
            refined.extend(batch_en)

        return TranslationOutcome(texts=translated, origin=self.name, sources=refined)

    # -- internals ----------------------------------------------------------

    def _api_key(self, ctx: RunContext) -> str | None:
        """v0.1's resolution order: config first, then the environment."""
        return ctx.config.llm.api_key or os.environ.get("OPENAI_API_KEY")

    def _client_for(self, ctx: RunContext) -> Any:
        """Return the injected client, or build one from the config."""
        if self._client is not None:
            return self._client

        openai_cls = _load_openai()
        if openai_cls is None:
            raise TranslationBackendError(
                self.name, "the openai package is not installed (install porter-workflow[llm])"
            )

        api_key = self._api_key(ctx)
        if not api_key:
            raise TranslationBackendError(self.name, "no LLM API key is configured")

        base_url = (
            ctx.config.llm.api_base
            or os.environ.get("OPENAI_BASE_URL")
            or _DEFAULT_BASE_URL
        )
        return openai_cls(api_key=api_key, base_url=base_url, timeout=_CLIENT_TIMEOUT_SECONDS)

    def _translate_batch(
        self,
        client: Any,
        model: str,
        batch: list[str],
        base_index: int,
    ) -> tuple[list[str], list[str]]:
        """One chat completion for the whole batch. Raises on any failure.

        Returns ``(chinese, refined_english)``, both positionally aligned with
        ``batch``. The refined English is not decoration: the prompt asks the model
        to fix ASR and punctuation errors, and v0.1 put that corrected text in the
        bilingual track, so discarding it would silently ship the raw ASR English
        next to a translation made from the corrected version.

        ``base_index`` is the batch's offset into the full input list; it is used
        as the JSON ``id`` so the mapping back is positional and unambiguous.
        """
        json_repair = _load_json_repair()
        if json_repair is None:
            raise TranslationBackendError(
                self.name, "the json-repair package is not installed (install porter-workflow[llm])"
            )

        batch_payload = [
            {"id": base_index + offset, "en": text} for offset, text in enumerate(batch)
        ]
        # The prompt is v0.1's, verbatim. It targets Simplified Chinese explicitly,
        # so ``target_lang`` is accepted for the interface but not interpolated —
        # changing the prompt would change output for the default case.
        prompt = (
            "You are a master bilingual subtitle translator and video localization expert.\n"
            "Below is a list of complete sentences extracted from a video transcript (which may include podcasts, interviews, dialogue, or speeches).\n"
            "Please translate each English sentence into natural, fluent, and concise Simplified Chinese (zh-Hans).\n"
            "Rules:\n"
            "1. Keep the Chinese translation idiomatic, colloquial, and synchronized with conversational dialogue pacing (preserve oral humor and conversational tone).\n"
            "2. Adapt slang, technical terms, and idioms appropriately for native Chinese viewers.\n"
            "3. Output MUST be a strict JSON array of objects with keys: 'id' (number), 'zh' (Chinese translation).\n\n"
            f"Input transcript sentences:\n{json.dumps(batch_payload, ensure_ascii=False)}"
        )

        # ``openai``'s errors are looked up dynamically because the extra is
        # optional; a 429/500 is an ``OpenAIError``, not an ``OSError``.
        client_errors: tuple[type[BaseException], ...] = (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            *_openai_error_types(),
        )
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            content = response.choices[0].message.content or ""
        except client_errors as exc:
            raise TranslationBackendError(
                self.name, "LLM request failed", reason=str(exc)
            ) from exc

        try:
            parsed = json_repair.loads(content)
        except Exception as exc:
            raise TranslationBackendError(
                self.name, "LLM response could not be parsed", reason=str(exc)
            ) from exc

        if not isinstance(parsed, list):
            raise TranslationBackendError(
                self.name,
                "LLM response was not a JSON array",
                kind=type(parsed).__name__,
            )

        by_id = self._index_translations(parsed)
        refined_by_id = self._index_refinements(parsed)

        chinese = [
            text if not text.strip() else by_id.get(base_index + offset, text)
            for offset, text in enumerate(batch)
        ]
        # Fall back to the input whenever the model omitted or blanked the entry,
        # so the caller never has to treat None as "use the original".
        refined = [
            refined_by_id.get(base_index + offset) or text
            for offset, text in enumerate(batch)
        ]
        return chinese, refined

    @staticmethod
    def _index_refinements(parsed: list[Any]) -> dict[int, str]:
        """Map ``id`` -> corrected English, ignoring unusable entries.

        Separate from :meth:`_index_translations` because the two fields fail
        independently: a model can return a perfectly good Chinese translation
        while echoing the English unchanged, or vice versa.
        """
        by_id: dict[int, str] = {}
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            key = entry.get("id")
            value = entry.get("en")
            if isinstance(key, int) and isinstance(value, str) and value.strip():
                by_id[key] = value.strip()
        return by_id

    @staticmethod
    def _index_translations(parsed: list[Any]) -> dict[int, str]:
        """Map ``id`` -> Chinese text, ignoring unusable entries.

        Keying by id rather than by list position is the guard against
        ``json_repair`` reordering or partially dropping entries: a dropped entry
        simply misses its key and the caller falls back to the original text.
        """
        by_id: dict[int, str] = {}
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            key = entry.get("id")
            value = entry.get("zh")
            if isinstance(key, int) and isinstance(value, str) and value.strip():
                by_id[key] = value.strip()
        return by_id
