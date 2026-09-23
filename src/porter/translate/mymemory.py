"""MyMemory — the documented, key-free translation backend.

Unlike :mod:`porter.translate.bing` and :mod:`porter.translate.google`, this one
is a real, documented public API (``api.mymemory.translated.net/get``), so its
wire format is stable and it can be tested offline by patching the HTTP client.
It is also the weakest of the three: it is a translation *memory*, not a
translator, so it does best on short, common phrases and answers with a warning
string once the anonymous daily quota is used up.

What is preserved from v0.1, byte for byte:

* The URL, the ``q``/``langpair`` query parameters, the browser User-Agent, and
  the 8 s timeout.
* The ``en|<target>`` language-pair direction and the treatment of a
  ``MYMEMORY WARNING`` reply as "not translated".

What changed, deliberately:

* Cancellation is checked before every request. v0.1 translated a 40-minute video
  string by string with no way to stop it.
* A non-200 response or a body that is not the documented JSON shape raises
  :class:`TranslationBackendError`. v0.1 swallowed it and returned the English
  input as the translation, which is indistinguishable from success downstream.
* Empty and whitespace-only inputs are returned unchanged without a request, so
  the output length always equals the input length.
"""

from __future__ import annotations

from typing import Any

from porter.context import RunContext
from porter.logging import get_logger
from porter.translate.base import TranslationBackendError, TranslationOutcome

__all__ = ["MyMemoryBackend"]

_logger = get_logger(__name__)

_TRANSLATE_URL = "https://api.mymemory.translated.net/get"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT_SECONDS = 8.0

#: MyMemory answers HTTP 200 with this prefix once the anonymous quota is spent.
_QUOTA_WARNING_PREFIX = "MYMEMORY WARNING"


def _load_requests() -> Any:
    """Import ``requests`` lazily so ``available()`` can report a missing dep."""
    import requests

    return requests


def _extract_translation(data: Any) -> str | None:
    """Return the translated text, or ``None`` when the body is not usable.

    ``None`` covers both "the documented shape is missing" (a failure the caller
    raises on) and "the API returned a quota warning" (which the caller treats as
    an untranslatable input). The distinction is made by :func:`_is_warning`.
    """
    if not isinstance(data, dict):
        return None
    response_data = data.get("responseData")
    if not isinstance(response_data, dict):
        return None
    translated = response_data.get("translatedText")
    if not isinstance(translated, str):
        return None
    return translated


def _is_warning(translated: str) -> bool:
    return translated.startswith(_QUOTA_WARNING_PREFIX)


class MyMemoryBackend:
    """The documented MyMemory GET endpoint, one request per string."""

    name = "mymemory"
    endpoint_verified = True

    # -- probe --------------------------------------------------------------

    def available(self, ctx: RunContext) -> bool:
        """Whether ``requests`` can be imported. Never raises."""
        try:
            return _load_requests() is not None
        except ImportError:
            return False
        except Exception:
            _logger.error("mymemory availability probe raised", exc_info=True)
            return False

    # -- translation --------------------------------------------------------

    def translate_texts(
        self,
        texts: list[str],
        target_lang: str,
        ctx: RunContext,
    ) -> TranslationOutcome:
        """Translate ``texts`` one at a time, preserving order and length.

        Raises:
            TranslationBackendError: When a request fails or its body is not the
                documented shape.
        """
        if not texts:
            return TranslationOutcome(texts=[], origin=self.name)

        requests = _load_requests()
        if requests is None:
            raise TranslationBackendError(self.name, "the requests package is not installed")

        translated: list[str] = []
        for text in texts:
            ctx.check_cancelled()
            if not text.strip():
                translated.append(text)
                continue

            params = {"q": text, "langpair": f"en|{target_lang}"}
            try:
                response = requests.get(
                    _TRANSLATE_URL,
                    params=params,
                    headers={"User-Agent": _USER_AGENT},
                    timeout=_TIMEOUT_SECONDS,
                )
            except (OSError, ValueError, TypeError) as exc:
                raise TranslationBackendError(
                    self.name, "mymemory request failed", reason=str(exc)
                ) from exc

            if response.status_code != 200:
                raise TranslationBackendError(
                    self.name, "mymemory request failed", status=response.status_code
                )

            try:
                data = response.json()
            except (ValueError, TypeError) as exc:
                raise TranslationBackendError(
                    self.name, "mymemory response was not JSON", reason=str(exc)
                ) from exc

            if not isinstance(data, dict) or "responseData" not in data:
                raise TranslationBackendError(
                    self.name, "mymemory response was not the documented shape"
                )

            result = _extract_translation(data)
            if result is None or _is_warning(result) or not result.strip():
                # Not a transport failure: the API answered, it just has nothing
                # useful. Return the input unchanged so alignment is preserved.
                translated.append(text)
            else:
                translated.append(result.strip())

        return TranslationOutcome(texts=translated, origin=self.name)
