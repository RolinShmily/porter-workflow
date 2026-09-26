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

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from porter import mirrors
from porter.asr import videocaptioner, whisper_api, whisper_local
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


def audio_file(tmp_path: Path) -> Path:
    """The same file as the ``audio`` fixture, for tests that also need ``tmp_path``.

    Exists because the local-Whisper tests assert on the *path handed to the
    model*, so they need ``tmp_path`` in the signature as well.
    """
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


def _executable(script: Path) -> str:
    """What to hand the backend as ``binary`` for ``script``.

    POSIX runs the shell script directly. Windows cannot: CreateProcess refuses a
    shebang script with ``ERROR_BAD_EXE_FORMAT``, so all five VideoCaptioner tests
    failed at "could not be started" instead of testing what they are about. A
    ``.cmd`` shim that hands the body to ``sh`` is the same arrangement a real
    install uses when its entry point is a shell script.

    The shim is kept beside the script rather than replacing it, because the fakes
    write their argv to ``"$0.args"`` and ``$0`` is the script's path.
    """
    if sys.platform != "win32":
        return str(script)
    shim = script.with_suffix(".cmd")
    # ``%*`` forwards the arguments; the path reaches ``sh`` in the POSIX form it
    # understands (``C:/...``), not the Windows one. The CRLF is deliberate: a
    # ``.cmd`` is read by the command processor, which wants it.
    shim.write_text(f'@sh "{script.as_posix()}" %*\r\n', encoding="ascii")
    return str(shim)


class TestVideoCaptioner:
    def test_transcribe_runs_cli_and_parses_srt(
        self, tmp_path: Path, audio: Path
    ) -> None:
        script = _write_script(tmp_path, _VC_SCRIPT)
        backend = videocaptioner.VideoCaptionerBackend(binary=_executable(script))

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
        backend = videocaptioner.VideoCaptionerBackend(binary=_executable(script))

        outcome = backend.transcribe(audio, _ctx(tmp_path, engine="bijian", language="zh"))

        assert outcome.origin == "videocaptioner:bijian"
        args = Path(f"{script}.args").read_text(encoding="utf-8").splitlines()
        assert "bijian" in args
        assert "--language" in args and "zh" in args

    def test_nonzero_exit_raises_backend_error(self, tmp_path: Path, audio: Path) -> None:
        script = _write_script(tmp_path, "#!/bin/sh\nexit 3\n")
        backend = videocaptioner.VideoCaptionerBackend(binary=_executable(script))
        with pytest.raises(AsrBackendError, match="every videocaptioner engine failed"):
            backend.transcribe(audio, _ctx(tmp_path))

    def test_unparseable_output_raises_backend_error(self, tmp_path: Path, audio: Path) -> None:
        script = _write_script(tmp_path, _VC_GARBAGE_SCRIPT)
        backend = videocaptioner.VideoCaptionerBackend(binary=_executable(script))
        with pytest.raises(AsrBackendError, match="every videocaptioner engine failed"):
            backend.transcribe(audio, _ctx(tmp_path))

    def test_missing_audio_raises(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, _VC_SCRIPT)
        backend = videocaptioner.VideoCaptionerBackend(binary=_executable(script))
        with pytest.raises(AsrBackendError, match="does not exist"):
            backend.transcribe(tmp_path / "nope.wav", _ctx(tmp_path))

    def test_cancellation_kills_the_child(
        self, tmp_path: Path, audio: Path
    ) -> None:
        script = _write_script(tmp_path, "#!/bin/sh\nexec sleep 30\n")
        backend = videocaptioner.VideoCaptionerBackend(binary=_executable(script))
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
# Local Whisper (no key, no network, no third-party service)
# ---------------------------------------------------------------------------


class _FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class _FakeLocalModel:
    """Records the transcribe call and replays canned segments."""

    def __init__(
        self,
        segments: list[_FakeSegment],
        error: BaseException | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self._segments = segments
        self._error = error
        self._on_call = on_call
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def transcribe(self, path: str, **kwargs: Any) -> Any:
        self.calls.append((path, kwargs))
        if self._on_call is not None:
            self._on_call()
        if self._error is not None:
            raise self._error
        return iter(self._segments), SimpleNamespace(language="en", duration=1.0)


class _FakeLoader:
    """Stands in for ``faster_whisper.WhisperModel``.

    Fails for whichever devices are named in ``fail_on``, so the CUDA-to-CPU
    fallback can be exercised without a GPU or a missing cuDNN.
    """

    def __init__(
        self,
        model: _FakeLocalModel | None = None,
        *,
        fail_on: set[str] | None = None,
    ) -> None:
        self.model = model or _FakeLocalModel([])
        self.fail_on = fail_on or set()
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, model: str, *, device: str, compute_type: str) -> _FakeLocalModel:
        self.calls.append((model, device, compute_type))
        if device in self.fail_on:
            raise RuntimeError(f"cannot load on {device}")
        return self.model


class TestWhisperLocal:
    """The local backend is the chain's only key-free engine that works.

    Everything here injects the model factory, so no weights are loaded, no model
    is downloaded and no network is touched. The behaviour under test is the
    *adapter's*: segment-to-cue conversion, device fallback, error mapping and the
    no-chunking rule.
    """

    def test_segments_become_cues_with_millisecond_timings(self, tmp_path: Path) -> None:
        model = _FakeLocalModel(
            [
                _FakeSegment(0.0, 1.25, "  hello there "),
                _FakeSegment(1.25, 2.5, "general kenobi"),
            ]
        )
        backend = whisper_local.WhisperLocalBackend(model="small", loader=_FakeLoader(model))

        outcome = backend.transcribe(audio_file(tmp_path), _ctx(tmp_path))

        assert [item.source_text for item in outcome.items] == ["hello there", "general kenobi"]
        assert [(item.start_ms, item.end_ms) for item in outcome.items] == [
            (0, 1250),
            (1250, 2500),
        ]
        assert [item.index for item in outcome.items] == [1, 2]
        assert outcome.used_asr is True
        # ``auto`` tries CUDA first, and the fake loader accepts it, so the
        # provenance names cuda. The CPU attempt is exercised by the fallback test.
        assert outcome.origin == "faster-whisper:small:cuda"

    def test_the_audio_is_handed_over_whole(self, tmp_path: Path) -> None:
        """No chunking: ``CHUNK_SECONDS`` is a cloud-upload limit, not a rule.

        Cutting the file first would break sentences across chunk boundaries and
        put a seam in the timings; faster-whisper segments internally instead.
        """
        model = _FakeLocalModel([_FakeSegment(0.0, 1.0, "one")])
        backend = whisper_local.WhisperLocalBackend(loader=_FakeLoader(model))
        path = audio_file(tmp_path)

        backend.transcribe(path, _ctx(tmp_path))

        assert len(model.calls) == 1
        called_path, kwargs = model.calls[0]
        assert called_path == str(path)
        assert kwargs["vad_filter"] is True

    def test_auto_language_is_passed_as_none(self, tmp_path: Path) -> None:
        model = _FakeLocalModel([_FakeSegment(0.0, 1.0, "x")])
        backend = whisper_local.WhisperLocalBackend(loader=_FakeLoader(model))

        backend.transcribe(audio_file(tmp_path), _ctx(tmp_path))

        assert model.calls[0][1]["language"] is None

    def test_a_configured_language_reaches_the_model(self, tmp_path: Path) -> None:
        model = _FakeLocalModel([_FakeSegment(0.0, 1.0, "x")])
        backend = whisper_local.WhisperLocalBackend(loader=_FakeLoader(model))

        backend.transcribe(audio_file(tmp_path), _ctx(tmp_path, language="en"))

        assert model.calls[0][1]["language"] == "en"

    def test_auto_device_falls_back_from_cuda_to_cpu(self, tmp_path: Path) -> None:
        """A missing cuDNN is the common case, and it only shows up on load."""
        model = _FakeLocalModel([_FakeSegment(0.0, 1.0, "x")])
        loader = _FakeLoader(model, fail_on={"cuda"})
        backend = whisper_local.WhisperLocalBackend(loader=loader)

        outcome = backend.transcribe(audio_file(tmp_path), _ctx(tmp_path))

        assert [call[1] for call in loader.calls] == ["cuda", "cpu"]
        assert loader.calls[1][2] == "int8"
        assert outcome.origin == "faster-whisper:small:cpu"

    def test_an_explicit_device_is_not_silently_swapped(self, tmp_path: Path) -> None:
        """Naming a device is a request; failing over would hide the mistake."""
        loader = _FakeLoader(fail_on={"cuda"})
        backend = whisper_local.WhisperLocalBackend(loader=loader)

        with pytest.raises(AsrBackendError) as excinfo:
            backend.transcribe(audio_file(tmp_path), _ctx(tmp_path, whisper_local_device="cuda"))

        assert [call[1] for call in loader.calls] == ["cuda"]
        assert "cuda" in str(excinfo.value)

    def test_every_attempt_failing_raises_backend_error(self, tmp_path: Path) -> None:
        loader = _FakeLoader(fail_on={"cuda", "cpu"})
        backend = whisper_local.WhisperLocalBackend(loader=loader)

        with pytest.raises(AsrBackendError) as excinfo:
            backend.transcribe(audio_file(tmp_path), _ctx(tmp_path))

        assert len(loader.calls) == 2
        assert "model=small" in str(excinfo.value)
        assert "cannot load on cuda" in str(excinfo.value)
        assert "cannot load on cpu" in str(excinfo.value)

    def test_silence_alone_raises_backend_error(self, tmp_path: Path) -> None:
        """No cues is a failure the chain must be able to act on.

        Returning an empty list would look like success and write an empty
        subtitle file, which is worse than falling through to the next backend.
        """
        backend = whisper_local.WhisperLocalBackend(loader=_FakeLoader(_FakeLocalModel([])))

        with pytest.raises(AsrBackendError) as excinfo:
            backend.transcribe(audio_file(tmp_path), _ctx(tmp_path))

        assert "no speech recognised" in str(excinfo.value)

    def test_blank_and_degenerate_segments_are_dropped(self, tmp_path: Path) -> None:
        model = _FakeLocalModel(
            [
                _FakeSegment(0.0, 1.0, "kept"),
                _FakeSegment(1.0, 2.0, "   "),
                _FakeSegment(3.0, 3.0, "zero length"),
                _FakeSegment(4.0, 5.0, "also kept"),
            ]
        )
        backend = whisper_local.WhisperLocalBackend(loader=_FakeLoader(model))

        outcome = backend.transcribe(audio_file(tmp_path), _ctx(tmp_path))

        assert [item.source_text for item in outcome.items] == ["kept", "also kept"]

    def test_missing_audio_raises(self, tmp_path: Path) -> None:
        backend = whisper_local.WhisperLocalBackend(loader=_FakeLoader())

        with pytest.raises(AsrBackendError):
            backend.transcribe(tmp_path / "nope.wav", _ctx(tmp_path))

    def test_cancellation_interrupts_iteration(self, tmp_path: Path) -> None:
        """A long file must be interruptible mid-decode, not only between devices."""
        ctx = _ctx(tmp_path)
        # Cancels once inference starts, so the per-segment check is what raises.
        model = _FakeLocalModel(
            [_FakeSegment(float(i), float(i) + 1.0, f"cue {i}") for i in range(50)],
            on_call=ctx.request_cancel,
        )
        backend = whisper_local.WhisperLocalBackend(loader=_FakeLoader(model))

        with pytest.raises(JobCancelled):
            backend.transcribe(audio_file(tmp_path), ctx)

    def test_local_inference_is_permanently_verified(self) -> None:
        """The field is a structural property here, not a one-off measurement.

        There is no remote protocol to drift, which is exactly what makes this
        backend able to let ``porter_plan`` report a feasible ASR route.
        """
        assert whisper_local.WhisperLocalBackend().endpoint_verified is True

    def test_available_true_without_a_loader_when_the_package_is_present(
        self, tmp_path: Path
    ) -> None:
        assert whisper_local.WhisperLocalBackend().available(_ctx(tmp_path)) is True

    def test_available_true_with_an_injected_loader(self, tmp_path: Path) -> None:
        assert whisper_local.WhisperLocalBackend(loader=_FakeLoader()).available(_ctx(tmp_path))

    def test_available_false_without_the_extra(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(whisper_local, "_load_faster_whisper", _raise_import_error)
        assert whisper_local.WhisperLocalBackend().available(_ctx(tmp_path)) is False

    def test_available_never_raises_on_a_broken_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("libctranslate2.so is missing")

        monkeypatch.setattr(whisper_local, "_load_faster_whisper", _boom)
        assert whisper_local.WhisperLocalBackend().available(_ctx(tmp_path)) is False

    def test_transcribe_without_the_extra_names_the_extra(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(whisper_local, "_load_faster_whisper", _raise_import_error)

        with pytest.raises(AsrBackendError) as excinfo:
            whisper_local.WhisperLocalBackend().transcribe(audio_file(tmp_path), _ctx(tmp_path))

        assert "asr-local" in str(excinfo.value)

    def test_a_model_that_is_not_cached_is_announced_before_loading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A silent multi-hundred-megabyte download reads as a hang."""
        monkeypatch.setattr(whisper_local, "model_is_cached", lambda _model: False)
        model = _FakeLocalModel([_FakeSegment(0.0, 1.0, "x")])
        backend = whisper_local.WhisperLocalBackend(model="large-v3", loader=_FakeLoader(model))

        outcome = backend.transcribe(
            audio_file(tmp_path), _ctx(tmp_path, whisper_local_device="cpu")
        )

        assert outcome.origin == "faster-whisper:large-v3:cpu"


class TestWhisperLocalModelCache:
    """``model_is_cached`` answers "could this run offline?" and never raises.

    The resolver is injected in every failure case: asserting on the real cache
    would make the suite depend on which models this machine happens to have
    downloaded, which is the host-dependence class of defect §13.45 was about.
    """

    @pytest.fixture(autouse=True)
    def _huggingface_path_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the mirror decision off: this class is about the HF resolver.

        Without it the answers below depend on the host twice over -- whether the
        machine looks Chinese, and whether a ModelScope copy happens to be on
        disk already -- reintroducing exactly the host-dependence this class was
        written to avoid. The mirror path has its own tests in
        ``test_whisper_mirror.py``.
        """
        monkeypatch.setattr(mirrors, "use_china_mirrors", lambda: False)

    def test_a_missing_model_reports_a_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _resolver(_model: str, *, local_files_only: bool) -> str:
            assert local_files_only is True, "the probe must not allow a download"
            raise FileNotFoundError("not in cache")

        monkeypatch.setattr(whisper_local, "_load_download_model", lambda: _resolver)
        assert whisper_local.model_is_cached("large-v3") is False

    def test_a_cached_model_reports_a_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _resolver(_model: str, *, local_files_only: bool) -> str:
            return "/cache/small"

        monkeypatch.setattr(whisper_local, "_load_download_model", lambda: _resolver)
        assert whisper_local.model_is_cached("small") is True

    def test_a_partial_download_reports_a_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``config.json`` without ``model.bin`` must not promise an offline run.

        Real case: ``faster-whisper-medium`` was found on this machine as 67 MB
        of config/tokenizer/vocabulary and no weights.
        """

        def _resolver(_model: str, *, local_files_only: bool) -> str:
            raise OSError("model.bin is not present in the snapshot")

        monkeypatch.setattr(whisper_local, "_load_download_model", lambda: _resolver)
        assert whisper_local.model_is_cached("medium") is False

    def test_missing_package_reports_a_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(whisper_local, "_load_download_model", _raise_import_error)
        assert whisper_local.model_is_cached("small") is False

    def test_a_broken_import_reports_a_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Any:
            raise OSError("libctranslate2.so is missing")

        monkeypatch.setattr(whisper_local, "_load_download_model", _boom)
        assert whisper_local.model_is_cached("small") is False


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_backends_satisfy_the_protocol() -> None:
    backends = [
        whisper_local.WhisperLocalBackend(),
        whisper_api.WhisperApiBackend(),
        videocaptioner.VideoCaptionerBackend(),
    ]
    for backend in backends:
        assert isinstance(backend, AsrBackend)
        assert backend.name
