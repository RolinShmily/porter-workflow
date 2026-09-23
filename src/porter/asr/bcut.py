"""UNVERIFIED ENDPOINT. This ports v0.1's request/response handling for a
key-free endpoint that was reverse-engineered, not documented. It cannot be
exercised in an offline test suite and may already be dead. The structure
(availability probe, error mapping, cancellation, timeout) is deliberate and
tested; the wire format is best-effort and unvalidated.

Measured 2026-09-22: the API host answers (``resource/create`` returns HTTP 200),
but a real 10-minute transcription produced no usable utterances, so this path is
not working either. Unlike Google Web, the failure here is ambiguous -- it could
be a quota, a changed field, or a rejected upload -- so this module keeps its
"unverified" label rather than claiming the endpoint is dead.

Bilibili's Bcut speech-to-text. It requires no key and no account, which is why
it sits in the free end of the fallback chain, but the ``rubick-interface`` API
is undocumented: the field names (``in_boss_key``, ``ResourceFileType``,
``Etags``) are Bilibili's own capitalisation, and the polling ``model_id`` is
``7`` while every other step sends ``8``. Both quirks are preserved verbatim
because "fixing" an undocumented protocol is how you get an endpoint that
returns 200 and does nothing.

The session deliberately sets ``trust_env = False``. Bilibili's object storage
is domestic (China), and a developer's corporate or VPN proxy turns a 3-second
chunk upload into a stall that looks like an endpoint outage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from porter.asr.base import AsrBackendError, AsrOutcome, coerce_items
from porter.context import RunContext
from porter.logging import get_logger
from porter.models.subtitle import SubtitleItem

__all__ = [
    "API_BASE",
    "MODEL_ID",
    "POLL_ATTEMPTS",
    "POLL_INTERVAL",
    "BcutBackend",
]

_logger = get_logger(__name__)

NAME = "bcut"

#: v0.1's endpoint, model id, retry budget and interval, unchanged.
API_BASE = "https://member.bilibili.com/x/bcut/rubick-interface"
MODEL_ID = "8"
POLL_ATTEMPTS = 60
POLL_INTERVAL = 2.0

#: v0.1's per-request timeouts, unchanged: the small control calls get 15-20s
#: and the chunk upload gets 60s.
TIMEOUT_CREATE = 15.0
TIMEOUT_UPLOAD = 60.0
TIMEOUT_COMMIT = 20.0
TIMEOUT_TASK = 20.0
TIMEOUT_RESULT = 15.0

_HEADERS = {
    "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
    "Content-Type": "application/json",
}


def _data_object(response: requests.Response, what: str) -> dict[str, Any]:
    """Extract the ``data`` object from a Bilibili envelope.

    Bilibili answers ``{"code": 0, "data": {...}}`` and encodes failures inside
    ``code`` even at HTTP 200, so a missing ``data`` is a real failure rather
    than a reason to read a default.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise AsrBackendError(NAME, f"{what} returned malformed JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise AsrBackendError(NAME, f"{what} returned a non-object JSON body")
    data = body.get("data")
    if not isinstance(data, dict):
        raise AsrBackendError(NAME, f"{what} response is missing the data object")
    return data


class BcutBackend:
    """Bilibili Bcut. Chunked upload, then poll for the transcript."""

    name = NAME
    endpoint_verified = False

    def available(self, ctx: RunContext) -> bool:
        """Whether ``requests`` is importable. Never raises.

        Bcut needs no key, so there is nothing else to probe. The check is
        written against the module attribute rather than an import statement so
        a host (or a test) that has torn the dependency out degrades to
        ``False`` instead of raising.
        """
        try:
            return bool(getattr(requests, "Session", None))
        except Exception:  # available() must never raise; log and degrade.
            _logger.error("could not inspect the requests module; Bcut unavailable", exc_info=True)
            return False

    def transcribe(self, audio: Path, ctx: RunContext) -> AsrOutcome:
        """Upload ``audio`` to Bcut and return its utterances as cues.

        Raises:
            AsrBackendError: A missing/empty audio file, a transport or HTTP
                failure, a malformed envelope, or an exhausted poll budget.
        """
        ctx.check_cancelled()
        if getattr(requests, "Session", None) is None:
            raise AsrBackendError(
                self.name,
                "the 'requests' package is required for the Bcut backend",
            )

        if not audio.exists():
            raise AsrBackendError(self.name, f"audio file does not exist: {audio}")
        try:
            audio_data = audio.read_bytes()
        except OSError as exc:
            raise AsrBackendError(self.name, f"audio file could not be read: {exc}") from exc
        if not audio_data:
            raise AsrBackendError(self.name, "audio file is empty", path=str(audio))

        # trust_env=False: see the module docstring. A configured proxy stalls on
        # Bilibili's domestic object storage.
        session = requests.Session()
        session.trust_env = False

        try:
            download_url = self._upload(session, audio, audio_data)
            task_id = self._create_task(session, download_url)
            result = self._poll(session, task_id, ctx)
        except requests.RequestException as exc:
            raise AsrBackendError(self.name, f"Bcut request failed: {exc}") from exc
        except ValueError as exc:
            raise AsrBackendError(self.name, f"Bcut returned malformed JSON: {exc}") from exc

        return self._outcome(result)

    # -- steps --------------------------------------------------------------

    def _upload(self, session: Any, audio: Path, audio_data: bytes) -> str:
        """Steps 1-3: create the resource, PUT each chunk, commit the upload."""
        payload = json.dumps(
            {
                "type": 2,
                "name": audio.name,
                "size": len(audio_data),
                "ResourceFileType": audio.suffix.lstrip(".") or "wav",
                "model_id": MODEL_ID,
            }
        )
        resp = session.post(
            f"{API_BASE}/resource/create",
            data=payload,
            headers=_HEADERS,
            timeout=TIMEOUT_CREATE,
        )
        resp.raise_for_status()
        data = _data_object(resp, "resource/create")
        if "upload_urls" not in data:
            raise AsrBackendError(self.name, "resource/create did not return upload_urls")

        upload_urls = data["upload_urls"]
        per_size = data["per_size"]
        clips = len(upload_urls)

        etags: list[str] = []
        for clip in range(clips):
            start = clip * per_size
            end = (clip + 1) * per_size
            part_resp = session.put(
                upload_urls[clip],
                data=audio_data[start:end],
                headers=_HEADERS,
                timeout=TIMEOUT_UPLOAD,
            )
            part_resp.raise_for_status()
            etag = part_resp.headers.get("Etag")
            if etag:
                etags.append(etag)

        commit_data = json.dumps(
            {
                "InBossKey": data["in_boss_key"],
                "ResourceId": data["resource_id"],
                "Etags": ",".join(etags),
                "UploadId": data["upload_id"],
                "model_id": MODEL_ID,
            }
        )
        commit_resp = session.post(
            f"{API_BASE}/resource/create/complete",
            data=commit_data,
            headers=_HEADERS,
            timeout=TIMEOUT_COMMIT,
        )
        commit_resp.raise_for_status()
        download_url = _data_object(commit_resp, "resource/create/complete").get("download_url")
        if not download_url:
            raise AsrBackendError(self.name, "resource/create/complete did not return a download_url")
        return str(download_url)

    def _create_task(self, session: Any, download_url: str) -> str:
        """Step 4: submit the transcription task."""
        task_resp = session.post(
            f"{API_BASE}/task",
            data=json.dumps({"resource": download_url, "model_id": MODEL_ID}),
            headers=_HEADERS,
            timeout=TIMEOUT_TASK,
        )
        task_resp.raise_for_status()
        task_id = _data_object(task_resp, "task").get("task_id")
        if not task_id:
            raise AsrBackendError(self.name, "task creation did not return a task_id")
        return str(task_id)

    def _poll(self, session: Any, task_id: str, ctx: RunContext) -> dict[str, Any]:
        """Step 5: poll until ``state == 4``.

        ``ctx.cancel.wait`` replaces v0.1's bare ``time.sleep(2)`` so that Ctrl-C
        interrupts the 120-second budget on the next tick instead of after it.
        ``model_id`` is ``7`` here, matching v0.1 and the endpoint, not the
        ``8`` used everywhere else.
        """
        for _ in range(POLL_ATTEMPTS):
            if ctx.cancel.wait(POLL_INTERVAL):
                ctx.check_cancelled()
            ctx.check_cancelled()

            q_resp = session.get(
                f"{API_BASE}/task/result",
                params={"model_id": 7, "task_id": task_id},
                headers=_HEADERS,
                timeout=TIMEOUT_RESULT,
            )
            q_resp.raise_for_status()
            q_data = _data_object(q_resp, "task/result")
            if q_data.get("state") == 4:
                result = q_data.get("result", "{}")
                parsed = json.loads(result) if isinstance(result, str) else result
                if not isinstance(parsed, dict):
                    raise AsrBackendError(self.name, "task/result result is not a JSON object")
                return parsed

        raise AsrBackendError(
            self.name,
            f"Bcut did not finish within {POLL_ATTEMPTS * POLL_INTERVAL:.0f}s",
            task_id=task_id,
        )

    # -- result -------------------------------------------------------------

    def _outcome(self, result: dict[str, Any]) -> AsrOutcome:
        """Turn the ``utterances`` array into cues.

        v0.1 built an SRT string here and returned ``False`` for an empty result.
        Returning ``AsrOutcome`` with no items is not possible: the chain reads
        an empty list as "this backend produced nothing", which loses *why*, so
        an empty or malformed payload is an explicit failure.
        """
        utterances = result.get("utterances")
        if not isinstance(utterances, list):
            raise AsrBackendError(self.name, "Bcut result is missing the utterances array")

        items: list[SubtitleItem] = []
        for utterance in utterances:
            if not isinstance(utterance, dict):
                continue
            text = str(utterance.get("transcript", "")).strip()
            if not text:
                continue
            try:
                start_ms = int(utterance.get("start_time", 0))
                end_ms = int(utterance.get("end_time", 0))
            except (TypeError, ValueError) as exc:
                raise AsrBackendError(
                    self.name,
                    f"Bcut utterance has non-numeric timing: {exc}",
                ) from exc
            items.append(
                SubtitleItem(
                    index=len(items) + 1,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    source_text=text,
                    target_text="",
                )
            )

        cleaned = coerce_items(items)
        if not cleaned:
            raise AsrBackendError(self.name, "Bcut returned no usable utterances")

        return AsrOutcome(items=cleaned, used_asr=True, origin="bcut")
