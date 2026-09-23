"""UNVERIFIED ENDPOINT. This ports v0.1's request/response handling for a
key-free endpoint that was reverse-engineered, not documented. It cannot be
exercised in an offline test suite and may already be dead. The structure
(availability probe, error mapping, cancellation, timeout) is deliberate and
tested; the wire format is best-effort and unvalidated.

The endpoint is ``translate.googleapis.com/translate_a/single``, the same one the
Google Translate web widget calls. It is key-free, which is why it is here at all:
the alternative is a paid Cloud Translation key. The cost is that Google rotates
its anti-bot handling without notice, so a 200 response may carry a captcha page
or an empty ``data`` array rather than a translation.

What is preserved from v0.1, byte for byte:

* URL, browser User-Agent, ``client`` ids and their order (``gtx`` first, then
  ``dict-chrome-ex``), and the ``sl``/``tl``/``dt``/``q`` payload keys.
* Batch size 15 and the ``"\\n=====\\n"`` delimiter. The delimiter is load-bearing:
  one request carries many cues and the response is split back on it, so a change
  here silently misaligns every cue after the first.
* Timeouts: 12 s for a batch, 8 s for a single string.

What changed, deliberately:

* Cancellation is checked before every batch and before every single request.
  v0.1 had none, so a Ctrl-C during a 40-minute video's translation waited for
  every remaining HTTP round-trip.
* A batch that fails now raises :class:`TranslationBackendError`. v0.1 fell back
  to per-cue requests that swallowed every error and returned the *English input*
  as the "translation"; that is how a bot-blocked run produced a file full of
  non-translated cues and still reported success. The chain now sees the failure
  and tries the next engine.
* Every request has an explicit timeout.
"""

from __future__ import annotations

from typing import Any

from porter.context import RunContext
from porter.logging import get_logger
from porter.translate.base import (
    MAX_ATTEMPTS,
    MAX_TEXTS_PER_REQUEST,
    TranslationBackendError,
    TranslationOutcome,
    retry_after_seconds,
    retry_delay,
    retryable_status,
    wait_for_retry,
)

__all__ = ["GoogleTranslateBackend"]

_logger = get_logger(__name__)

#: Same endpoint and headers as v0.1.
_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

#: v0.1's batch ceiling. The key-free endpoint starts truncating above this.
_BATCH_SIZE = MAX_TEXTS_PER_REQUEST

#: v0.1's separator. Splitting the response on it is how one request's answer is
#: mapped back onto N cues.
_DELIMITER = "\n=====\n"

#: v0.1's client ids, tried in this order. Two attempts is v0.1's retry count.
_CLIENT_IDS = ("gtx", "dict-chrome-ex")

_BATCH_TIMEOUT_SECONDS = 12.0
_SINGLE_TIMEOUT_SECONDS = 8.0


def _load_requests() -> Any:
    """Import ``requests`` lazily.

    Lazy so :meth:`GoogleTranslateBackend.available` can report a missing
    dependency instead of the module failing to import.
    """
    import requests

    return requests


def _parse_payload(data: Any) -> list[str] | None:
    """Flatten Google's nested response into one string per segment.

    The payload is ``[[[translated, source, ...], ...], ...]``; the first element
    holds the segments and each segment's first element is the translated text.
    Returns ``None`` when the shape is not recognisable, which the caller treats
    as a failed request rather than guessing.
    """
    if not isinstance(data, list) or not data:
        return None
    segments = data[0]
    if not isinstance(segments, list):
        return None

    parts: list[str] = []
    for segment in segments:
        if isinstance(segment, list) and segment and isinstance(segment[0], str):
            parts.append(segment[0])
    if not parts:
        return None

    joined = "".join(parts)
    return [line.strip() for line in joined.split("=====")]


def _reason_of(exc: TranslationBackendError, client_id: str) -> str:
    """A client-tagged reason from a retry helper's error.

    ``_get_with_retry`` does not know which client it was asked about, so the
    client id is added here. Without it the failure report says only "a request
    failed", and the whole point of the two-client design is that they are
    throttled separately.
    """
    status = exc.details.get("status")
    if status is not None:
        return f"client={client_id} returned HTTP {status}"
    return f"client={client_id} {exc.details.get('reason', exc.message)}"


class GoogleTranslateBackend:
    """Key-free Google Translate HTTP backend.

    ``name`` is what the chain logs and what :class:`TranslationOutcome.origin`
    carries, so it must stay stable.
    """

    name = "google"
    endpoint_verified = False

    # -- probe --------------------------------------------------------------

    def available(self, ctx: RunContext) -> bool:
        """Whether ``requests`` can be imported. Never raises."""
        try:
            return _load_requests() is not None
        except ImportError:
            return False
        except Exception:
            _logger.error("google availability probe raised", exc_info=True)
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
            TranslationBackendError: When a request fails or its response cannot
                be split back into exactly one segment per input.
        """
        if not texts:
            return TranslationOutcome(texts=[], origin=self.name)

        requests = _load_requests()
        if requests is None:
            raise TranslationBackendError(self.name, "the requests package is not installed")

        translated: list[str] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            ctx.check_cancelled()
            batch = texts[start : start + _BATCH_SIZE]

            # A blank input has no delimiter of its own, so joining it into the
            # batch makes the response split ambiguous. Translate the whole batch
            # one string at a time instead, which keeps the mapping exact.
            if any(not text.strip() for text in batch):
                translated.extend(self._translate_each(requests, batch, target_lang, ctx))
            else:
                translated.extend(self._translate_batch(requests, batch, target_lang, ctx))

        return TranslationOutcome(texts=translated, origin=self.name)

    # -- internals ----------------------------------------------------------

    def _translate_batch(
        self,
        requests: Any,
        batch: list[str],
        target_lang: str,
        ctx: RunContext,
    ) -> list[str]:
        """One request for the whole batch, retrying a throttle. Raises on failure.

        Two independent knobs, because Google throttles in two ways: the
        ``Retry-After``-style 429 that clears if you wait, and the client id
        itself (``gtx`` and ``dict-chrome-ex`` are throttled separately). So each
        client is retried with backoff, and a client that keeps refusing is
        abandoned for the next one.
        """
        combined = _DELIMITER.join(batch)
        last_reason = "no attempt was made"

        for client_id in _CLIENT_IDS:
            params = {
                "client": client_id,
                "sl": "auto",
                "tl": target_lang,
                "dt": "t",
                "q": combined,
            }

            try:
                response = self._get_with_retry(
                    requests,
                    params,
                    ctx,
                    what=f"google batch request ({client_id})",
                    timeout=_BATCH_TIMEOUT_SECONDS,
                )
            except TranslationBackendError as exc:
                last_reason = _reason_of(exc, client_id)
                continue

            try:
                lines = _parse_payload(response.json())
            except (ValueError, TypeError) as exc:
                last_reason = f"client={client_id} returned malformed JSON: {exc}"
                _logger.warning("google batch response (%s) was not JSON: %s", client_id, exc)
                continue

            if lines is None:
                last_reason = f"client={client_id} returned an unrecognised payload"
                continue
            if len(lines) != len(batch):
                last_reason = (
                    f"client={client_id} returned {len(lines)} segments for {len(batch)} inputs"
                )
                continue

            return [
                line.strip() if line.strip() else batch[index]
                for index, line in enumerate(lines)
            ]

        raise TranslationBackendError(self.name, "google translation request failed", reason=last_reason)

    def _get_with_retry(
        self,
        requests: Any,
        params: dict[str, str],
        ctx: RunContext,
        *,
        what: str,
        timeout: float,
    ) -> Any:
        """GET once, retrying a throttle instead of failing the backend on it.

        Shared by the batch and per-cue paths deliberately. The first version of
        this fix put the retry only in ``_translate_batch``, which is the same
        mistake §13.47 made with ``_build_metadata``: a second call site with the
        same defect, found later. Both paths go through here now.
        """
        last_reason = "no attempt was made"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            ctx.check_cancelled()
            try:
                response = requests.get(
                    _TRANSLATE_URL,
                    params=params,
                    headers={"User-Agent": _USER_AGENT},
                    timeout=timeout,
                )
            except (OSError, ValueError, TypeError) as exc:
                last_reason = f"raised {type(exc).__name__}: {exc}"
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

        raise TranslationBackendError(self.name, f"{what} failed", reason=last_reason)

    def _translate_each(
        self,
        requests: Any,
        batch: list[str],
        target_lang: str,
        ctx: RunContext,
    ) -> list[str]:
        """Per-string requests for a batch that contains blank inputs.

        Unlike v0.1's fallback this does **not** swallow failures: a non-200 or a
        malformed body raises, so the chain can move to the next engine instead of
        accepting the English input as a translation.
        """
        results: list[str] = []
        for text in batch:
            ctx.check_cancelled()
            if not text.strip():
                results.append(text)
                continue

            params = {
                "client": _CLIENT_IDS[0],
                "sl": "auto",
                "tl": target_lang,
                "dt": "t",
                "q": text,
            }
            response = self._get_with_retry(
                requests,
                params,
                ctx,
                what=f"google single request ({_CLIENT_IDS[0]})",
                timeout=_SINGLE_TIMEOUT_SECONDS,
            )

            try:
                lines = _parse_payload(response.json())
            except (ValueError, TypeError) as exc:
                raise TranslationBackendError(
                    self.name, "google single response was not JSON", reason=str(exc)
                ) from exc

            if lines is None or not lines:
                raise TranslationBackendError(
                    self.name, "google single response was not recognised"
                )
            results.append(lines[0].strip() if lines[0].strip() else text)
        return results
