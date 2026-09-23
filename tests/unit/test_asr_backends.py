"""The four ASR backends: request shapes, error mapping, availability, cancel.

Two things are being pinned here. First, the v0.1 behaviour that must survive the
refactor — Whisper's SRT round-trip, Bcut's chunked upload and poll, Google's
silence slicing. Those tests are ports of ``tests/test_subtitle.py`` on ``main``,
with the patch targets moved from ``porter_skill.subtitle.controller`` to the
module that now owns each engine. Second, the behaviour the refactor exists to
add: expected failures raise :class:`AsrBackendError` (never a random exception),
``available()`` is total, and cancellation interrupts a poll loop immediately.

The Bcut and Google Web tests deliberately mock the *transport*, not the
endpoint: both endpoints are reverse-engineered and unverifiable offline, so the
tests assert the structure (envelope handling, error mapping, slicing) rather
than pretending the wire format is known good.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from porter.asr import bcut, google_web, videocaptioner, whisper_api
from porter.asr.base import AsrBackend, AsrBackendError
from porter.config import ASRConfig, PorterConfig
from porter.context import RunContext
from porter.errors import JobCancelled
from porter.models.request import JobOptions

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, **asr: Any) -> RunContext:
    """A RunContext whose config carries the requested ASR fields."""
    config = PorterConfig(asr=ASRConfig(**asr)) if asr else PorterConfig()
    return RunContext(
        job_id="test-job",
        options=JobOptions(output_dir=tmp_path / "out"),
        config=config,
    )


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"dummy wav data")
    return path


def _raise_import_error(*_args: Any, **_kwargs: Any) -> Any:
    raise ImportError("simulated missing dependency")


class _FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(
        self,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self._body = body
        self.headers = headers or {}
        self._error = error
        self._json_error = json_error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._body


class _FakeSession:
    """A ``requests.Session`` that pops queued POST responses and never dials."""

    def __init__(
        self,
        posts: list[_FakeResponse],
        *,
        put: _FakeResponse | None = None,
        get: Any = None,
    ) -> None:
        self._posts = list(posts)
        self._put = put
        self._get = get
        self.trust_env = True
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.put_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.post_calls.append((url, kwargs))
        return self._posts.pop(0)

    def put(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.put_calls.append((url, kwargs))
        assert self._put is not None
        return self._put

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.get_calls.append((url, kwargs))
        if callable(self._get):
            return self._get(len(self.get_calls))
        return self._get


def _bcut_upload_responses() -> tuple[_FakeResponse, _FakeResponse, _FakeResponse, _FakeResponse]:
    """The four fixed responses of Bcut's create/commit/task sequence."""
    create = _FakeResponse(
        {
            "data": {
                "in_boss_key": "k",
                "resource_id": "r",
                "upload_id": "u",
                "upload_urls": ["https://mock.upload/part1"],
                "per_size": 1024 * 1024,
            }
        }
    )
    commit = _FakeResponse({"data": {"download_url": "https://mock.dl/a.wav"}})
    task = _FakeResponse({"data": {"task_id": "task_123"}})
    put = _FakeResponse(None, headers={"Etag": "etag_123"})
    return create, commit, task, put


# ---------------------------------------------------------------------------
# Whisper API (tier 1 — documented endpoint)
# ---------------------------------------------------------------------------


class _FakeTranscriptions:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.audio = SimpleNamespace(transcriptions=_FakeTranscriptions(response))


class _FakeOpenAIFactory:
    """Stands in for the ``openai.OpenAI`` class itself."""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> _FakeClient:
        self.kwargs = kwargs
        return _FakeClient(self.response)


class TestWhisperApi:
    def test_transcribe_with_whisper_api(self, tmp_path: Path, audio: Path) -> None:
        """v0.1 port: the SRT body becomes cues, and the request shape is kept.

        Assertion change: v0.1 wrote ``out.srt`` and asserted on the file. The
        backend now returns ``AsrOutcome`` and file writing belongs to the chain,
        so the same intent is asserted on ``outcome.items``.
        """
        ctx = _ctx(tmp_path, whisper_api_key="sk-test-whisper")
        client = _FakeClient("1\n00:00:00,000 --> 00:00:02,000\nHello from Whisper API\n")
        backend = whisper_api.WhisperApiBackend(client=client)

        outcome = backend.transcribe(audio, ctx)

        assert outcome.used_asr is True
        assert outcome.origin == "whisper:whisper-1"
        assert [item.source_text for item in outcome.items] == ["Hello from Whisper API"]
        call = client.audio.transcriptions.calls[0]
        assert call["response_format"] == "srt"
        assert call["model"] == "whisper-1"

    def test_available_without_key(self, tmp_path: Path, audio: Path) -> None:
        ctx = _ctx(tmp_path)
        backend = whisper_api.WhisperApiBackend()
        assert backend.available(ctx) is False
        with pytest.raises(AsrBackendError, match="no Whisper API key"):
            backend.transcribe(audio, ctx)

    def test_key_and_base_url_resolution(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.1 order: asr key -> llm key -> OPENAI_API_KEY, and the default URL."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        factory = _FakeOpenAIFactory("1\n00:00:00,000 --> 00:00:01,000\nok\n")
        monkeypatch.setattr(whisper_api, "_load_openai", lambda: factory)

        outcome = whisper_api.WhisperApiBackend().transcribe(audio, _ctx(tmp_path))

        assert outcome.items
        assert factory.kwargs["api_key"] == "sk-from-env"
        assert factory.kwargs["base_url"] == whisper_api.DEFAULT_BASE_URL
        assert factory.kwargs["timeout"] == whisper_api.DEFAULT_TIMEOUT

    @pytest.mark.parametrize("message", ["429 quota", "500 server error"])
    def test_api_failure_raises_backend_error(
        self, tmp_path: Path, audio: Path, message: str
    ) -> None:
        client = _FakeClient(RuntimeError(message))
        backend = whisper_api.WhisperApiBackend(client=client)
        with pytest.raises(AsrBackendError, match="Whisper API request failed"):
            backend.transcribe(audio, _ctx(tmp_path))

    @pytest.mark.parametrize("response", ["", "this is not srt at all", "<html>502</html>"])
    def test_malformed_or_empty_body_raises(
        self, tmp_path: Path, audio: Path, response: str
    ) -> None:
        client = _FakeClient(response)
        backend = whisper_api.WhisperApiBackend(client=client)
        with pytest.raises(AsrBackendError, match="without parseable SRT"):
            backend.transcribe(audio, _ctx(tmp_path))

    def test_missing_llm_extra(self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing SDK makes the backend unavailable and fails cleanly."""
        monkeypatch.setattr(whisper_api, "_load_openai", _raise_import_error)
        ctx = _ctx(tmp_path, whisper_api_key="sk-test")
        backend = whisper_api.WhisperApiBackend()
        assert backend.available(ctx) is False
        with pytest.raises(AsrBackendError, match="llm"):
            backend.transcribe(audio, ctx)

    def test_available_never_raises_on_broken_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise RuntimeError("corrupt install")

        monkeypatch.setattr(whisper_api, "_load_openai", _boom)
        ctx = _ctx(tmp_path, whisper_api_key="sk-test")
        assert whisper_api.WhisperApiBackend().available(ctx) is False


# ---------------------------------------------------------------------------
# Bcut (tier 2 — UNVERIFIED endpoint)
# ---------------------------------------------------------------------------


class TestBcut:
    def test_transcribe_with_bcut_mock(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.1 port: create -> chunk PUT -> complete -> task -> poll state 4.

        Assertion change: v0.1 asserted the written SRT; the cues are now on the
        outcome. The upload/poll call shapes are additionally asserted.
        """
        monkeypatch.setattr(bcut, "POLL_INTERVAL", 0.0)
        create, commit, task, put = _bcut_upload_responses()
        query = _FakeResponse(
            {
                "data": {
                    "state": 4,
                    "result": json.dumps(
                        {
                            "utterances": [
                                {"start_time": 0, "end_time": 2000, "transcript": "Hello from Bcut ASR"}
                            ]
                        }
                    ),
                }
            }
        )
        session = _FakeSession([create, commit, task], put=put, get=query)
        monkeypatch.setattr(bcut.requests, "Session", lambda: session)

        outcome = bcut.BcutBackend().transcribe(audio, _ctx(tmp_path))

        assert outcome.used_asr is True
        assert outcome.origin == "bcut"
        assert [item.source_text for item in outcome.items] == ["Hello from Bcut ASR"]
        assert outcome.items[0].start_ms == 0
        assert outcome.items[0].end_ms == 2000
        # v0.1 disabled proxy trust for Bilibili's domestic storage.
        assert session.trust_env is False
        assert session.post_calls[0][0].endswith("/resource/create")
        assert session.post_calls[-1][0].endswith("/task")
        # The poll sends model_id 7 while every other step sends 8: preserved.
        assert session.get_calls[0][1]["params"] == {"model_id": 7, "task_id": "task_123"}
        assert session.put_calls[0][0] == "https://mock.upload/part1"

    @pytest.mark.parametrize("status", [429, 500])
    def test_http_error_raises_backend_error(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        session = _FakeSession([_FakeResponse(None, error=requests.HTTPError(str(status)))])
        monkeypatch.setattr(bcut.requests, "Session", lambda: session)
        with pytest.raises(AsrBackendError, match="Bcut request failed"):
            bcut.BcutBackend().transcribe(audio, _ctx(tmp_path))

    def test_malformed_envelope_raises(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession([_FakeResponse({"data": {}})])
        monkeypatch.setattr(bcut.requests, "Session", lambda: session)
        with pytest.raises(AsrBackendError, match="upload_urls"):
            bcut.BcutBackend().transcribe(audio, _ctx(tmp_path))

    def test_malformed_json_raises(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession([_FakeResponse(json_error=ValueError("not json"))])
        monkeypatch.setattr(bcut.requests, "Session", lambda: session)
        with pytest.raises(AsrBackendError, match="malformed JSON"):
            bcut.BcutBackend().transcribe(audio, _ctx(tmp_path))

    def test_non_object_body_raises(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession([_FakeResponse(["not", "an", "object"])])
        monkeypatch.setattr(bcut.requests, "Session", lambda: session)
        with pytest.raises(AsrBackendError, match="non-object JSON body"):
            bcut.BcutBackend().transcribe(audio, _ctx(tmp_path))

    def test_empty_utterances_raise_not_silent_success(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 200 with zero utterances is a quota answer, not a success."""
        monkeypatch.setattr(bcut, "POLL_INTERVAL", 0.0)
        create, commit, task, put = _bcut_upload_responses()
        query = _FakeResponse({"data": {"state": 4, "result": json.dumps({"utterances": []})}})
        session = _FakeSession([create, commit, task], put=put, get=query)
        monkeypatch.setattr(bcut.requests, "Session", lambda: session)
        with pytest.raises(AsrBackendError, match="no usable utterances"):
            bcut.BcutBackend().transcribe(audio, _ctx(tmp_path))

    def test_cancellation_stops_polling(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl-C must abort the 60-attempt poll, not wait it out."""
        monkeypatch.setattr(bcut, "POLL_INTERVAL", 0.0)
        create, commit, task, put = _bcut_upload_responses()
        ctx = _ctx(tmp_path)

        def _cancel_on_first_poll(_count: int) -> _FakeResponse:
            ctx.request_cancel()
            return _FakeResponse({"data": {"state": 3}})

        session = _FakeSession([create, commit, task], put=put, get=_cancel_on_first_poll)
        monkeypatch.setattr(bcut.requests, "Session", lambda: session)

        with pytest.raises(JobCancelled):
            bcut.BcutBackend().transcribe(audio, ctx)

        assert 0 < len(session.get_calls) < bcut.POLL_ATTEMPTS

    def test_missing_audio_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _FakeSession([])
        monkeypatch.setattr(bcut.requests, "Session", lambda: session)
        with pytest.raises(AsrBackendError, match="does not exist"):
            bcut.BcutBackend().transcribe(tmp_path / "nope.wav", _ctx(tmp_path))

    def test_non_numeric_timing_raises(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bcut, "POLL_INTERVAL", 0.0)
        create, commit, task, put = _bcut_upload_responses()
        query = _FakeResponse(
            {
                "data": {
                    "state": 4,
                    "result": json.dumps(
                        {
                            "utterances": [
                                {"start_time": "oops", "end_time": 2000, "transcript": "hi"}
                            ]
                        }
                    ),
                }
            }
        )
        session = _FakeSession([create, commit, task], put=put, get=query)
        monkeypatch.setattr(bcut.requests, "Session", lambda: session)
        with pytest.raises(AsrBackendError, match="non-numeric timing"):
            bcut.BcutBackend().transcribe(audio, _ctx(tmp_path))

    def test_available_true_and_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _ctx(tmp_path)
        assert bcut.BcutBackend().available(ctx) is True
        monkeypatch.setattr(bcut, "requests", None)
        assert bcut.BcutBackend().available(ctx) is False


# ---------------------------------------------------------------------------
# Google Web Speech (tier 2 — UNVERIFIED endpoint)
# ---------------------------------------------------------------------------


class _FakeAudioFile:
    """Context manager standing in for ``speech_recognition.AudioFile``."""

    def __init__(self, _path: str) -> None:
        pass

    def __enter__(self) -> _FakeAudioFile:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


class _FakeRecognizer:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.records: list[tuple[float, float]] = []
        self.recognitions: int = 0

    def record(self, _source: Any, offset: float = 0.0, duration: float = 0.0) -> Any:
        self.records.append((offset, duration))
        return object()

    def recognize_google(self, _data: Any, language: str = "en-US") -> str:
        self.recognitions += 1
        if self._error is not None:
            raise self._error
        return self._text


def _tool_result(*, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr)


class TestGoogleWeb:
    def test_transcribe_with_google_stt_mock(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.1 port: silencedetect + ffprobe, then recognise each slice.

        Assertion change: v0.1 asserted the written SRT existed; the cues are now
        on the outcome, so the recognised text is asserted instead.
        """
        sr = pytest.importorskip("speech_recognition")
        results = [
            _tool_result(
                stderr="[silencedetect @ 0x...] silence_start: 2.0\n"
                "[silencedetect @ 0x...] silence_end: 2.5"
            ),
            _tool_result(stdout="5.0\n"),
        ]
        monkeypatch.setattr(google_web.subprocess, "run", lambda *_a, **_k: results.pop(0))
        recognizer = _FakeRecognizer("Hello from Google STT")
        monkeypatch.setattr(sr, "Recognizer", lambda: recognizer)
        monkeypatch.setattr(sr, "AudioFile", _FakeAudioFile)

        outcome = google_web.GoogleWebBackend().transcribe(audio, _ctx(tmp_path))

        assert outcome.used_asr is True
        assert outcome.origin == "google-web"
        # One silence midpoint at 2.25s splits the 5s clip into two cues.
        assert [item.source_text for item in outcome.items] == ["Hello from Google STT"] * 2

    def test_transcribe_with_google_stt_long_dialogue_mock(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.1 port: a 24s no-silence segment is sub-sliced, not dropped."""
        sr = pytest.importorskip("speech_recognition")
        results = [_tool_result(stderr=""), _tool_result(stdout="24.0\n")]
        monkeypatch.setattr(google_web.subprocess, "run", lambda *_a, **_k: results.pop(0))
        recognizer = _FakeRecognizer("Continuous dialogue transcribed")
        monkeypatch.setattr(sr, "Recognizer", lambda: recognizer)
        monkeypatch.setattr(sr, "AudioFile", _FakeAudioFile)

        outcome = google_web.GoogleWebBackend().transcribe(audio, _ctx(tmp_path))

        assert outcome.items
        assert all(
            item.source_text == "Continuous dialogue transcribed" for item in outcome.items
        )
        # 24s / 8s ceiling -> at least 3 slices.
        assert recognizer.recognitions >= 3
        assert len(recognizer.records) >= 3

    def test_request_error_raises_backend_error(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sr = pytest.importorskip("speech_recognition")
        results = [_tool_result(stderr=""), _tool_result(stdout="5.0\n")]
        monkeypatch.setattr(google_web.subprocess, "run", lambda *_a, **_k: results.pop(0))
        recognizer = _FakeRecognizer(error=sr.RequestError("429"))
        monkeypatch.setattr(sr, "Recognizer", lambda: recognizer)
        monkeypatch.setattr(sr, "AudioFile", _FakeAudioFile)

        with pytest.raises(AsrBackendError, match="Google Web Speech request failed"):
            google_web.GoogleWebBackend().transcribe(audio, _ctx(tmp_path))

    def test_unknown_value_is_skipped_and_empty_result_raises(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-speech slices are skipped; a run with no cues is a failure."""
        sr = pytest.importorskip("speech_recognition")
        results = [_tool_result(stderr=""), _tool_result(stdout="5.0\n")]
        monkeypatch.setattr(google_web.subprocess, "run", lambda *_a, **_k: results.pop(0))
        recognizer = _FakeRecognizer(error=sr.UnknownValueError())
        monkeypatch.setattr(sr, "Recognizer", lambda: recognizer)
        monkeypatch.setattr(sr, "AudioFile", _FakeAudioFile)

        with pytest.raises(AsrBackendError, match="produced no cues"):
            google_web.GoogleWebBackend().transcribe(audio, _ctx(tmp_path))

    def test_malformed_duration_raises(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = [_tool_result(stderr=""), _tool_result(stdout="")]
        monkeypatch.setattr(google_web.subprocess, "run", lambda *_a, **_k: results.pop(0))
        with pytest.raises(AsrBackendError, match="no usable duration"):
            google_web.GoogleWebBackend().transcribe(audio, _ctx(tmp_path))

    def test_non_finite_duration_raises(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = [_tool_result(stderr=""), _tool_result(stdout="nan\n")]
        monkeypatch.setattr(google_web.subprocess, "run", lambda *_a, **_k: results.pop(0))
        with pytest.raises(AsrBackendError, match="no duration"):
            google_web.GoogleWebBackend().transcribe(audio, _ctx(tmp_path))

    def test_missing_ffmpeg_raises_backend_error(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _missing(*_args: Any, **_kwargs: Any) -> Any:
            raise FileNotFoundError("ffprobe")

        monkeypatch.setattr(google_web.subprocess, "run", _missing)
        with pytest.raises(AsrBackendError, match="not installed or not on PATH"):
            google_web.GoogleWebBackend().transcribe(audio, _ctx(tmp_path))

    def test_available_true_and_missing_dependency(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(tmp_path)
        assert google_web.GoogleWebBackend().available(ctx) is True
        monkeypatch.setattr(google_web, "_load_sr", _raise_import_error)
        assert google_web.GoogleWebBackend().available(ctx) is False
        with pytest.raises(AsrBackendError, match="stt"):
            google_web.GoogleWebBackend().transcribe(tmp_path / "nope.wav", ctx)

    def test_available_never_raises_on_broken_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise RuntimeError("corrupt install")

        monkeypatch.setattr(google_web, "_load_sr", _boom)
        assert google_web.GoogleWebBackend().available(_ctx(tmp_path)) is False


# ---------------------------------------------------------------------------
# VideoCaptioner CLI (tier 1 — external subprocess adapter)
# ---------------------------------------------------------------------------


_VC_SCRIPT = """#!/bin/sh
printf '%s\\n' "$@" > "$0.args"
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '1\\n00:00:00,000 --> 00:00:02,000\\nHello from VideoCaptioner CLI\\n' > "$out"
"""


_VC_GARBAGE_SCRIPT = """#!/bin/sh
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf 'not an srt' > "$out"
"""


def _write_script(tmp_path: Path, body: str, name: str = "videocaptioner") -> Path:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


class TestVideoCaptioner:
    def test_transcribe_runs_cli_and_parses_srt(
        self, tmp_path: Path, audio: Path
    ) -> None:
        script = _write_script(tmp_path, _VC_SCRIPT)
        backend = videocaptioner.VideoCaptionerBackend(binary=str(script))

        outcome = backend.transcribe(audio, _ctx(tmp_path))

        assert outcome.used_asr is True
        assert outcome.origin == "videocaptioner:jianying"
        assert [item.source_text for item in outcome.items] == ["Hello from VideoCaptioner CLI"]
        args = Path(f"{script}.args").read_text(encoding="utf-8").splitlines()
        assert args[0] == "transcribe"
        assert "--format" in args and "srt" in args
        assert "--asr" in args and "jianying" in args

    def test_configured_engine_and_language_reach_the_cli(
        self, tmp_path: Path, audio: Path
    ) -> None:
        script = _write_script(tmp_path, _VC_SCRIPT)
        backend = videocaptioner.VideoCaptionerBackend(binary=str(script))

        outcome = backend.transcribe(audio, _ctx(tmp_path, engine="bijian", language="zh"))

        assert outcome.origin == "videocaptioner:bijian"
        args = Path(f"{script}.args").read_text(encoding="utf-8").splitlines()
        assert "bijian" in args
        assert "--language" in args and "zh" in args

    def test_nonzero_exit_raises_backend_error(self, tmp_path: Path, audio: Path) -> None:
        script = _write_script(tmp_path, "#!/bin/sh\nexit 3\n")
        backend = videocaptioner.VideoCaptionerBackend(binary=str(script))
        with pytest.raises(AsrBackendError, match="every videocaptioner engine failed"):
            backend.transcribe(audio, _ctx(tmp_path))

    def test_unparseable_output_raises_backend_error(self, tmp_path: Path, audio: Path) -> None:
        script = _write_script(tmp_path, _VC_GARBAGE_SCRIPT)
        backend = videocaptioner.VideoCaptionerBackend(binary=str(script))
        with pytest.raises(AsrBackendError, match="every videocaptioner engine failed"):
            backend.transcribe(audio, _ctx(tmp_path))

    def test_missing_audio_raises(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, _VC_SCRIPT)
        backend = videocaptioner.VideoCaptionerBackend(binary=str(script))
        with pytest.raises(AsrBackendError, match="does not exist"):
            backend.transcribe(tmp_path / "nope.wav", _ctx(tmp_path))

    def test_cancellation_kills_the_child(
        self, tmp_path: Path, audio: Path
    ) -> None:
        script = _write_script(tmp_path, "#!/bin/sh\nexec sleep 30\n")
        backend = videocaptioner.VideoCaptionerBackend(binary=str(script))
        ctx = _ctx(tmp_path)
        timer = threading.Timer(0.3, ctx.request_cancel)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(JobCancelled):
                backend.transcribe(audio, ctx)
        finally:
            timer.cancel()
        assert time.monotonic() - started < 10.0

    def test_available_with_explicit_binary(self, tmp_path: Path) -> None:
        backend = videocaptioner.VideoCaptionerBackend(binary="/usr/local/bin/videocaptioner")
        assert backend.available(_ctx(tmp_path)) is True

    def test_available_false_without_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(videocaptioner.shutil, "which", lambda _name: None)
        monkeypatch.setattr(videocaptioner, "_user_bin_dir", lambda: tmp_path / "empty")
        assert videocaptioner.VideoCaptionerBackend().available(_ctx(tmp_path)) is False

    def test_available_never_raises_when_probe_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_name: str) -> Any:
            raise OSError("PATH exploded")

        monkeypatch.setattr(videocaptioner.shutil, "which", _boom)
        assert videocaptioner.VideoCaptionerBackend().available(_ctx(tmp_path)) is False

    def test_available_never_raises_on_unexpected_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_name: str) -> Any:
            raise RuntimeError("something odd")

        monkeypatch.setattr(videocaptioner.shutil, "which", _boom)
        assert videocaptioner.VideoCaptionerBackend().available(_ctx(tmp_path)) is False


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_backends_satisfy_the_protocol() -> None:
    backends = [
        whisper_api.WhisperApiBackend(),
        bcut.BcutBackend(),
        google_web.GoogleWebBackend(),
        videocaptioner.VideoCaptionerBackend(),
    ]
    for backend in backends:
        assert isinstance(backend, AsrBackend)
        assert backend.name


class TestTransportErrorsBecomeBackendErrors:
    """Every expected failure must be an ``AsrBackendError``.

    The chain catches exactly ``AsrBackendError`` and ``PorterError``. Anything
    else propagates and kills the process with a traceback -- which is what
    happened on a real run: ``speech_recognition`` reads its response with
    ``response.read()`` on a chunked body, and Google's Web STT endpoint returns a
    body with broken chunked framing, so the truncated read surfaced as
    ``http.client.IncompleteRead``. That is an ``HTTPException``: not an
    ``OSError``, not an ``sr.RequestError``, and caught by nothing.
    """

    @pytest.mark.parametrize(
        "error",
        [
            http.client.IncompleteRead(b"partial"),
            http.client.RemoteDisconnected("closed"),
            http.client.BadStatusLine("garbage"),
            ConnectionResetError("reset"),
        ],
    )
    def test_transport_failures_are_mapped(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        _install_google_web(monkeypatch, error)

        with pytest.raises(AsrBackendError) as excinfo:
            google_web.GoogleWebBackend().transcribe(audio, _ctx(tmp_path))

        assert excinfo.value.backend == "google-web"
        assert type(error).__name__ in str(excinfo.value)

    def test_the_mapped_error_names_the_exception_kind(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The kind rides in ``details`` so a log line can separate the causes."""
        _install_google_web(monkeypatch, http.client.IncompleteRead(b"x"))

        with pytest.raises(AsrBackendError) as excinfo:
            google_web.GoogleWebBackend().transcribe(audio, _ctx(tmp_path))

        assert excinfo.value.details["kind"] == "IncompleteRead"

    def test_a_missing_speech_extra_still_raises_backend_error(
        self, tmp_path: Path, audio: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ImportError path predates this fix and must keep working."""
        monkeypatch.setattr(
            google_web, "_load_sr", lambda: (_ for _ in ()).throw(ImportError("no sr"))
        )

        with pytest.raises(AsrBackendError):
            google_web.GoogleWebBackend().transcribe(audio, _ctx(tmp_path))


def _install_google_web(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Make the backend see one speech interval, then fail with ``error``.

    Patches the real ``speech_recognition`` module (as the other Google Web tests
    do) rather than substituting a fake, so the backend's ``except
    sr.RequestError`` clause is matched against the genuine class hierarchy.
    """
    sr = pytest.importorskip("speech_recognition")

    recognizer = _FakeRecognizer(error=error)
    monkeypatch.setattr(sr, "Recognizer", lambda: recognizer)
    monkeypatch.setattr(sr, "AudioFile", _FakeAudioFile)
    monkeypatch.setattr(
        google_web.GoogleWebBackend, "_duration", lambda self, a, c: 4.0
    )
    monkeypatch.setattr(
        google_web.GoogleWebBackend,
        "_slice_intervals",
        lambda self, starts, ends, total: [(0.0, 2.0)],
    )
    monkeypatch.setattr(
        google_web.GoogleWebBackend,
        "_detect_silences",
        lambda self, a, c: ([], []),
    )
