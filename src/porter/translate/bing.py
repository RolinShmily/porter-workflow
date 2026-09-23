"""UNVERIFIED ENDPOINT. This ports v0.1's request/response handling for a
key-free endpoint that was reverse-engineered, not documented. It cannot be
exercised in an offline test suite and may already be dead. The structure
(availability probe, error mapping, cancellation, timeout) is deliberate and
tested; the wire format is best-effort and unvalidated.

Bing's free web translator is the least stable of the key-free backends, because
it gates each translation behind a per-page CSRF pair that is *scraped out of the
HTML*: an ``IG``/``IID`` query pair plus a timestamped ``key``/``token`` from
``params_AbusePreventionHelper``. When Microsoft changes that page, the regexes
stop matching and every request fails. That is a normal, expected failure — the
chain moves on — which is why it raises :class:`TranslationBackendError` rather
than crashing.

What is preserved from v0.1, byte for byte:

* The two URLs (including the double ``&&`` in the ``ttranslatev3`` query), the
  Edge User-Agent, the ``Referer``, and the four regexes.
* The ``fromLang``/``text``/``to``/``key``/``token`` payload keys.
* Batch size 15 and the ``"\\n\\n"`` delimiter used to pack several cues into one
  request and split them apart again.
* Timeouts: 10 s to fetch the token page, 15 s for a batch, 10 s for a single
  string.

What changed, deliberately:

* Cancellation is checked before the token fetch and before every request.
* A failed token scrape, a non-200 response, or a malformed body raises
  :class:`TranslationBackendError`. v0.1 returned an empty list (which the chain
  read as "this backend produced nothing") and, inside a batch, silently fell back
  to returning the English input as the translation.
* Every request has an explicit timeout.
"""

from __future__ import annotations

import re
from typing import Any

from porter.context import RunContext
from porter.logging import get_logger
from porter.translate.base import (
    MAX_ATTEMPTS,
    TranslationBackendError,
    TranslationOutcome,
    retry_after_seconds,
    retry_delay,
    retryable_status,
    wait_for_retry,
)

__all__ = ["BingTranslateBackend"]

_logger = get_logger(__name__)

_TRANSLATOR_PAGE_URL = "https://www.bing.com/translator"
_TRANSLATE_URL_TEMPLATE = "https://www.bing.com/ttranslatev3?isVertical=1&&IG={ig}&IID={iid}"
_REFERER = "https://www.bing.com/translator"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

_BATCH_SIZE = 8

#: Measured 2026-09-23, after a real job failed with "response was not recognised":
#:
#: ============  ==========  ================================================
#: cues          chars       result
#: ============  ==========  ================================================
#: 1             77          translated
#: 2             161         translated
#: 4             329         translated
#: 8             665         translated
#: 15            1259        **HTTP 200 with ``{"statusCode": 400}`` inside**
#: ============  ==========  ================================================
#:
#: So the shared ``MAX_TEXTS_PER_REQUEST`` (15, chosen for Google's truncation)
#: is above Bing's real ceiling, and §13.48's failure was not a throttle that
#: would have cleared -- it was a size limit, hit on every 15-cue batch. 8 is the
#: largest size measured to work.
#:
#: It is a *hint*, not the fix: a refusal degrades to one request per cue, so a
#: ceiling that moves (and it will) costs speed rather than the backend.
_BATCH_SIZE = 8

#: What separates cues inside one batched request.
#:
#: This used to be ``"\n\n"``, and Bing **collapses runs of newlines**: a batch
#: sent as ``"hello world\n\ngood morning"`` came back with the separator
#: collapsed to one ``\n``, so splitting on ``"\n\n"`` recovered
#: one segment from two inputs and the backend failed with "wrong number of
#: segments". Bing now answers with ``"usedLLM": true``, so it is a model
#: rewriting the blob and paragraph structure is not something it preserves.
#:
#: ``[[|]]`` is a token a translator has no reason to touch, and it survives: three
#: runs of a full 15-cue batch came back with exactly 15 segments each.
#:
#: It is still a guess about someone else's text processor, so the count is checked
#: and a mismatch degrades to one request per cue rather than failing the backend.
_DELIMITER = "\n[[|]]\n"

_PAGE_TIMEOUT_SECONDS = 10.0
_BATCH_TIMEOUT_SECONDS = 15.0
_SINGLE_TIMEOUT_SECONDS = 10.0

_IG_PATTERN = re.compile(r"IG:\"([A-Za-z0-9]+)\"")
_IID_PATTERN = re.compile(r"data-iid=\"([^\"]+)\"")
_TOKEN_PATTERN = re.compile(r"params_AbusePreventionHelper\s*=\s*\[([0-9]+),\"([^\"]+)\"")


def _load_requests() -> Any:
    """Import ``requests`` lazily so ``available()`` can report a missing dep."""
    import requests

    return requests


def _parse_token_page(html: str) -> tuple[str, str, str, str] | None:
    """Extract ``(ig, iid, key, token)`` from the translator page.

    Returns ``None`` when the page shape has changed, which the caller reports as
    a backend failure. The regexes are v0.1's, unchanged.
    """
    ig_match = _IG_PATTERN.search(html)
    iid_match = _IID_PATTERN.search(html)
    token_match = _TOKEN_PATTERN.search(html)
    if not (ig_match and iid_match and token_match):
        return None
    return ig_match.group(1), iid_match.group(1), token_match.group(1), token_match.group(2)


def _parse_translation(data: Any) -> str | None:
    """Pull the translated text out of Bing's ``ttranslatev3`` response body."""
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    translations = first.get("translations")
    if not isinstance(translations, list) or not translations:
        return None
    entry = translations[0]
    if not isinstance(entry, dict):
        return None
    text = entry.get("text")
    return text if isinstance(text, str) else None


def _refusal(data: Any) -> str | None:
    """A refusal object, if that is what this body is.

    Bing answers **HTTP 200** with a JSON object on refusal, so the status line
    says "fine" while the body says no. Detecting it separately from "unparseable"
    matters because the two need different responses: a refusal is a statement
    about *this request*, so a smaller request may work, whereas a shape we do not
    recognise will not improve by asking again per cue.
    """
    if not isinstance(data, dict):
        return None
    code = data.get("statusCode", data.get("status"))
    if code is None:
        return None
    return f"statusCode={code!r} message={data.get('message', data.get('error'))!r}"


def _describe_body(data: Any) -> str:
    """What the response actually was, for the error message.

    The old message was the bare string ``"bing batch response was not
    recognised"``, which discarded the only evidence there was. When that fired on
    a real run the body could not be recovered afterwards, and the only way to
    find out what Bing had said was to re-run a probe by hand -- which then
    succeeded, because the failure was transient. An unactionable message costs a
    whole investigation; this one costs a log line.
    """
    if data is None:
        return "body was empty or not JSON"
    if isinstance(data, dict):
        # Bing answers HTTP 200 with a JSON *object* when it refuses, so the
        # status code says "fine" while the body says no.
        code = data.get("statusCode", data.get("status"))
        message = data.get("message", data.get("error"))
        if code is not None or message is not None:
            return f"service refused: statusCode={code!r} message={message!r}"
        return f"JSON object with keys {sorted(data)[:8]}"
    if isinstance(data, list):
        return f"JSON list of {len(data)} entries, first={type(data[0]).__name__ if data else 'none'}"
    return f"{type(data).__name__}"


class BingTranslateBackend:
    """Key-free Microsoft Bing web-translator backend."""

    name = "bing"
    endpoint_verified = False

    # -- probe --------------------------------------------------------------

    def available(self, ctx: RunContext) -> bool:
        """Whether ``requests`` can be imported. Never raises."""
        try:
            return _load_requests() is not None
        except ImportError:
            return False
        except Exception:
            _logger.error("bing availability probe raised", exc_info=True)
            return False

    # -- translation --------------------------------------------------------

    def translate_texts(
        self,
        texts: list[str],
        target_lang: str,
        ctx: RunContext,
    ) -> TranslationOutcome:
        """Translate ``texts``, preserving order and length.

        Raises:
            TranslationBackendError: When the token page cannot be parsed, a
                request fails, or the response does not split back into exactly
                one segment per input.
        """
        if not texts:
            return TranslationOutcome(texts=[], origin=self.name)

        requests = _load_requests()
        if requests is None:
            raise TranslationBackendError(self.name, "the requests package is not installed")

        ctx.check_cancelled()
        session = requests.Session()
        ig, iid, key, token = self._open_session(session, ctx)
        url = _TRANSLATE_URL_TEMPLATE.format(ig=ig, iid=iid)
        headers = {"User-Agent": _USER_AGENT, "Referer": _REFERER}

        translated: list[str] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            ctx.check_cancelled()
            batch = texts[start : start + _BATCH_SIZE]

            # A blank cue contributes no delimiter, so packing it into a batch
            # makes the split ambiguous. Go one request per string instead.
            if any(not text.strip() for text in batch):
                translated.extend(
                    self._translate_each(session, url, headers, key, token, batch, target_lang, ctx)
                )
            else:
                translated.extend(
                    self._translate_batch(session, url, headers, key, token, batch, target_lang, ctx)
                )

        return TranslationOutcome(texts=translated, origin=self.name)

    # -- internals ----------------------------------------------------------

    def _open_session(self, session: Any, ctx: RunContext) -> tuple[str, str, str, str]:
        """Fetch the translator page and scrape the CSRF material out of it."""
        try:
            response = session.get(
                _TRANSLATOR_PAGE_URL,
                headers={"User-Agent": _USER_AGENT, "Referer": _REFERER},
                timeout=_PAGE_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError, TypeError) as exc:
            raise TranslationBackendError(
                self.name, "could not reach the bing translator page", reason=str(exc)
            ) from exc

        parsed = _parse_token_page(getattr(response, "text", "") or "")
        if parsed is None:
            raise TranslationBackendError(
                self.name,
                "the bing translator page no longer exposes IG/IID/AbusePreventionHelper",
                status=getattr(response, "status_code", None),
            )
        return parsed

    def _translate_batch(
        self,
        session: Any,
        url: str,
        headers: dict[str, str],
        key: str,
        token: str,
        batch: list[str],
        target_lang: str,
        ctx: RunContext,
    ) -> list[str]:
        """One POST for the whole batch, retrying a refusal. Raises on failure."""
        ctx.check_cancelled()
        data = {
            "fromLang": "auto-detect",
            "text": _DELIMITER.join(batch),
            "to": target_lang,
            "key": key,
            "token": token,
        }

        response = self._post_with_retry(
            session,
            url,
            data,
            headers,
            ctx,
            what="bing batch request",
            timeout=_BATCH_TIMEOUT_SECONDS,
        )

        try:
            body = response.json()
        except (ValueError, TypeError) as exc:
            raise TranslationBackendError(
                self.name, "bing batch response was not JSON", reason=str(exc)
            ) from exc

        parsed = _parse_translation(body)
        if parsed is None:
            refusal = _refusal(body)
            if refusal is not None:
                # The batch is an optimisation, not a contract (§13.40). Bing
                # refuses oversized payloads with an HTTP-200 refusal object, and
                # failing the backend over it hands the whole job to a worse
                # engine when one request per cue works fine -- measured: single
                # cues and 8-cue batches translate, 15-cue batches do not.
                _logger.warning(
                    "bing refused the %d-cue batch (%s); retrying one request per cue",
                    len(batch),
                    refusal,
                )
                return self._translate_each(
                    session, url, headers, key, token, batch, target_lang, ctx
                )
            raise TranslationBackendError(
                self.name,
                "bing batch response was not recognised",
                reason=_describe_body(body),
            )

        parts = [part.strip() for part in parsed.split(_DELIMITER) if part.strip()]
        if len(parts) != len(batch):
            # The batch is an optimisation, not a contract. Raising here failed the
            # whole backend and handed the cues to the next one in the chain -- for
            # a delimiter the translator had rewritten, on a service that still
            # works fine one cue at a time. Degrade instead: slower, still correct.
            _logger.warning(
                "bing returned %d segments for %d inputs; retrying one request per cue",
                len(parts),
                len(batch),
            )
            return self._translate_each(
                session, url, headers, key, token, batch, target_lang, ctx
            )

        return [part if part else batch[index] for index, part in enumerate(parts)]

    def _post_with_retry(
        self,
        session: Any,
        url: str,
        data: dict[str, str],
        headers: dict[str, str],
        ctx: RunContext,
        *,
        what: str,
        timeout: float,
    ) -> Any:
        """POST once, retrying a throttle instead of failing the backend on it.

        Bing rate-limits aggressively -- ``MAX_TEXTS_PER_REQUEST`` exists partly
        for that reason -- so a single attempt turns "slow down" into "this backend
        is broken", and the chain moves on to an endpoint that may be worse. Only
        429/5xx and transport errors are retried; a 403 is not going to improve.
        """
        last_reason = "no attempt was made"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            ctx.check_cancelled()
            try:
                response = session.post(
                    url, data=data, headers=headers, timeout=timeout
                )
            except (OSError, ValueError, TypeError) as exc:
                last_reason = str(exc)
                if attempt >= MAX_ATTEMPTS:
                    raise TranslationBackendError(
                        self.name, f"{what} failed", reason=last_reason
                    ) from exc
                _logger.warning("%s failed (%s); retrying", what, exc)
                wait_for_retry(ctx, retry_delay(attempt))
                continue

            if response.status_code == 200:
                return response

            if retryable_status(response.status_code) and attempt < MAX_ATTEMPTS:
                delay = retry_delay(attempt, retry_after=retry_after_seconds(response))
                _logger.warning(
                    "%s returned HTTP %s; retrying in %.1fs",
                    what,
                    response.status_code,
                    delay,
                )
                wait_for_retry(ctx, delay)
                continue

            raise TranslationBackendError(
                self.name, f"{what} failed", status=response.status_code
            )

        # Unreachable: the loop either returns, raises, or continues while
        # attempts remain. Kept so the signature has no implicit ``None``.
        raise TranslationBackendError(self.name, f"{what} failed", reason=last_reason)

    def _translate_each(
        self,
        session: Any,
        url: str,
        headers: dict[str, str],
        key: str,
        token: str,
        batch: list[str],
        target_lang: str,
        ctx: RunContext,
    ) -> list[str]:
        """Per-string requests for a batch that contains blank inputs."""
        results: list[str] = []
        for text in batch:
            ctx.check_cancelled()
            if not text.strip():
                results.append(text)
                continue

            data = {
                "fromLang": "auto-detect",
                "text": text,
                "to": target_lang,
                "key": key,
                "token": token,
            }
            response = self._post_with_retry(
                session,
                url,
                data,
                headers,
                ctx,
                what="bing single request",
                timeout=_SINGLE_TIMEOUT_SECONDS,
            )

            try:
                body = response.json()
            except (ValueError, TypeError) as exc:
                raise TranslationBackendError(
                    self.name, "bing single response was not JSON", reason=str(exc)
                ) from exc

            parsed = _parse_translation(body)
            if parsed is None:
                raise TranslationBackendError(
                    self.name,
                    "bing single response was not recognised",
                    reason=_describe_body(body),
                )
            results.append(parsed.strip() if parsed.strip() else text)
        return results
