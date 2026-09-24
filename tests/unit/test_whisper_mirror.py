"""Tests for the ModelScope model path in :mod:`porter.asr.whisper_local`.

Why the path exists: from China, fetching ~480 MB of weights from Hugging Face is
slow at best and usually impossible, and ModelScope mirrors the identical
CTranslate2 files. Two properties are load-bearing, and both are pinned here.

* **It is the same model.** Verified rather than assumed: the two ``config.json``
  files have the same SHA-256, and ModelScope's four files match a working
  Hugging Face snapshot byte for byte in size. A "mirror" that were a lookalike
  would quietly change transcription quality.
* **It replaces a download; it never adds one.** The mirror is a preference, so
  it must not be able to make a job fail. A missing ``[asr-local]`` extra, a size
  with no mirror, and a failed transfer all fall through to the official source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from porter import mirrors
from porter.asr import whisper_local
from porter.asr.base import AsrBackendError
from porter.config import ASRConfig, PorterConfig
from porter.context import RunContext
from porter.errors import JobCancelled
from porter.models.request import JobOptions

_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        job_id="mirror-test",
        options=JobOptions(output_dir=tmp_path / "out"),
        config=PorterConfig(asr=ASRConfig()),
    )


def _audio(tmp_path: Path) -> Path:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"dummy wav data")
    return path


def _mirror_dir(root: Path, model: str = "small") -> Path:
    """The directory ``modelscope_model_dir`` resolves to for ``model``."""
    return root / f"pengzhendong--faster-whisper-{model}"


def _fill(directory: Path, *, skip: str | None = None, empty: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in _MODEL_FILES:
        if name == skip:
            continue
        (directory / name).write_bytes(b"" if name == empty else b"x" * 16)
    return directory


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private ModelScope cache, with the mirrors switched on."""
    root = tmp_path / "modelscope"
    monkeypatch.setattr(whisper_local, "_modelscope_root", lambda: root)
    monkeypatch.setattr(mirrors, "use_china_mirrors", lambda: True)
    return root


@pytest.fixture
def mirrors_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mirrors, "use_china_mirrors", lambda: False)


class _Response:
    """A streaming ``requests.Response`` stand-in."""

    def __init__(self, chunks: list[bytes], status_code: int = 200) -> None:
        self._chunks = chunks
        self.status_code = status_code

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int) -> Any:
        assert chunk_size > 0
        yield from self._chunks


class _Recorder:
    """Captures the requests made, and answers with a canned body."""

    def __init__(
        self,
        body: bytes = b"weights",
        status_code: int = 200,
        chunks: list[bytes] | None = None,
    ) -> None:
        # ``chunks`` exists for tests that need the cancel to land mid-transfer.
        self.chunks = chunks if chunks is not None else [body]
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return _Response(self.chunks, self.status_code)


class _Loader:
    """Stands in for ``faster_whisper.WhisperModel``; records what it was given."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, model: str, *, device: str, compute_type: str) -> Any:
        del device, compute_type
        self.calls.append(model)
        return _Model()


class _Segment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class _Model:
    def transcribe(self, _audio: str, **_kwargs: Any) -> Any:
        # One real cue: an empty segment list is the "no speech recognised"
        # error path, which is a different test's subject.
        return iter([_Segment(0.0, 1.0, "hello there")]), None


def _raise_import_error(*_args: Any, **_kwargs: Any) -> Any:
    raise ImportError("simulated missing optional dependency")


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------


class TestCompleteness:
    def test_a_full_directory_is_complete(self, tmp_path: Path) -> None:
        assert whisper_local._model_dir_is_complete(_fill(tmp_path / "m")) is True

    def test_a_missing_file_is_not(self, tmp_path: Path) -> None:
        # The real case behind this rule: faster-whisper-medium on the machine
        # this was written on had config/tokenizer/vocabulary and no weights.
        assert whisper_local._model_dir_is_complete(_fill(tmp_path / "m", skip="model.bin")) is False

    def test_an_empty_file_is_not(self, tmp_path: Path) -> None:
        # An interrupted download leaves a zero-length file; counting that as
        # cached would surface much later as an unreadable-model error.
        assert whisper_local._model_dir_is_complete(_fill(tmp_path / "m", empty="model.bin")) is False

    def test_a_directory_that_is_not_there_is_not(self, tmp_path: Path) -> None:
        assert whisper_local._model_dir_is_complete(tmp_path / "absent") is False


class TestModelScopeModelDir:
    def test_a_mirrored_size_resolves(self, cache: Path) -> None:
        assert whisper_local.modelscope_model_dir("small") == _mirror_dir(cache)

    def test_an_unmirrored_size_has_no_directory(self, cache: Path) -> None:
        assert whisper_local.modelscope_model_dir("distil-large-v3") is None

    def test_the_repo_name_becomes_one_path_component(self, cache: Path) -> None:
        assert "/" not in _mirror_dir(cache).name
        assert whisper_local.modelscope_model_dir("small").parent == cache  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class TestDownloadFile:
    def test_bytes_are_written_and_renamed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _Recorder(body=b"payload")
        monkeypatch.setattr(whisper_local.requests, "get", recorder.get)
        destination = tmp_path / "model.bin"

        whisper_local._download_file("https://example/model.bin", destination, _ctx(tmp_path))

        assert destination.read_bytes() == b"payload"
        assert not (tmp_path / "model.bin.part").exists(), "the part file is not left behind"

    def test_a_fresh_download_sends_no_range(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _Recorder()
        monkeypatch.setattr(whisper_local.requests, "get", recorder.get)

        whisper_local._download_file("https://example/model.bin", tmp_path / "model.bin", _ctx(tmp_path))

        assert "Range" not in recorder.calls[0]["headers"]

    def test_a_partial_file_is_resumed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 480 MB over a link that drops should not restart from zero each time.
        (tmp_path / "model.bin.part").write_bytes(b"a" * 100)
        # 206 Partial Content is what a real resume answers; a plain 200 means the
        # server ignored the range, which the next test covers.
        recorder = _Recorder(body=b"b" * 50, status_code=206)
        monkeypatch.setattr(whisper_local.requests, "get", recorder.get)

        whisper_local._download_file("https://example/model.bin", tmp_path / "model.bin", _ctx(tmp_path))

        assert recorder.calls[0]["headers"]["Range"] == "bytes=100-"
        assert (tmp_path / "model.bin").read_bytes() == b"a" * 100 + b"b" * 50

    def test_a_server_ignoring_the_range_starts_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 200 means the whole file is coming, so the bytes on disk are not a
        # prefix of it. Appending would produce a corrupt model.
        (tmp_path / "model.bin.part").write_bytes(b"a" * 100)
        recorder = _Recorder(body=b"fresh", status_code=200)
        monkeypatch.setattr(whisper_local.requests, "get", recorder.get)

        whisper_local._download_file("https://example/model.bin", tmp_path / "model.bin", _ctx(tmp_path))

        assert (tmp_path / "model.bin").read_bytes() == b"fresh"

    def test_an_http_error_is_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _Recorder(status_code=404)
        monkeypatch.setattr(whisper_local.requests, "get", recorder.get)

        with pytest.raises(requests.HTTPError):
            whisper_local._download_file("https://example/model.bin", tmp_path / "m.bin", _ctx(tmp_path))

    def test_a_failed_transfer_leaves_no_destination(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _get(_url: str, **_kwargs: Any) -> _Response:
            return _Response([b"half"], status_code=500)

        monkeypatch.setattr(whisper_local.requests, "get", _get)
        destination = tmp_path / "model.bin"

        with pytest.raises(requests.HTTPError):
            whisper_local._download_file("https://example/model.bin", destination, _ctx(tmp_path))

        assert not destination.exists(), "a failure must not look like a cached model"


class TestCancellation:
    def test_a_cancel_is_seen_between_chunks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 480 MB download must be interruptible *while* it runs."""
        monkeypatch.setattr(
            whisper_local.requests, "get", _Recorder(body=b"x").get
        )
        ctx = _ctx(tmp_path)
        monkeypatch.setattr(
            type(ctx), "check_cancelled", lambda _self: (_ for _ in ()).throw(JobCancelled("cancelled"))
        )

        with pytest.raises(JobCancelled):
            whisper_local._download_file("https://example/m.bin", tmp_path / "m.bin", ctx)

    def test_the_part_file_survives_so_the_next_run_resumes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two chunks, with the cancel landing on the second check: bytes already
        # written stay put. Losing them would mean re-fetching the whole file.
        recorder = _Recorder(chunks=[b"x" * 32, b"y" * 32])
        monkeypatch.setattr(whisper_local.requests, "get", recorder.get)

        ctx = _ctx(tmp_path)
        checks = {"n": 0}

        def _cancel(_self: Any) -> None:
            checks["n"] += 1
            if checks["n"] > 1:
                raise JobCancelled("cancelled")

        monkeypatch.setattr(type(ctx), "check_cancelled", _cancel)

        with pytest.raises(JobCancelled):
            whisper_local._download_file("https://example/m.bin", tmp_path / "m.bin", ctx)

        assert (tmp_path / "m.bin.part").read_bytes() == b"x" * 32
        assert not (tmp_path / "m.bin").exists(), "a cancelled transfer is never renamed"


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


class TestDownloadFromModelScope:
    def test_an_unmirrored_size_is_left_to_hugging_face(self, tmp_path: Path) -> None:
        assert whisper_local.download_from_modelscope("distil-large-v3", _ctx(tmp_path)) is None

    def test_a_complete_copy_is_returned_without_networking(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = _fill(_mirror_dir(cache))

        def _explode(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("a complete copy must not be re-fetched")

        monkeypatch.setattr(whisper_local.requests, "get", _explode)
        assert whisper_local.download_from_modelscope("small", _ctx(tmp_path)) == directory

    def test_every_file_is_fetched(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _Recorder(body=b"w" * 4)
        monkeypatch.setattr(whisper_local.requests, "get", recorder.get)

        whisper_local.download_from_modelscope("small", _ctx(tmp_path))

        fetched = [Path(call["url"].split("FilePath=")[1]).name for call in recorder.calls]
        assert sorted(fetched) == sorted(_MODEL_FILES)
        assert all("pengzhendong/faster-whisper-small" in c["url"] for c in recorder.calls)
        assert all(c["url"].startswith(mirrors.MODELSCOPE_ENDPOINT) for c in recorder.calls)

    def test_missing_files_are_fetched_and_present_ones_are_not(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fill(_mirror_dir(cache), skip="model.bin")
        recorder = _Recorder()
        monkeypatch.setattr(whisper_local.requests, "get", recorder.get)

        whisper_local.download_from_modelscope("small", _ctx(tmp_path))

        assert len(recorder.calls) == 1
        assert recorder.calls[0]["url"].endswith("FilePath=model.bin")

    def test_a_request_failure_falls_back_instead_of_raising(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A university mirror being down must not be worse than not trying it."""

        def _fail(*_args: Any, **_kwargs: Any) -> Any:
            raise requests.ConnectionError("mirror is down")

        monkeypatch.setattr(whisper_local.requests, "get", _fail)
        assert whisper_local.download_from_modelscope("small", _ctx(tmp_path)) is None

    def test_a_disk_error_falls_back_too(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("no space left on device")

        monkeypatch.setattr(whisper_local.requests, "get", _fail)
        assert whisper_local.download_from_modelscope("small", _ctx(tmp_path)) is None

    def test_a_cancel_is_not_a_mirror_problem(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A cancel is the user's instruction and must not be downgraded into a
        # fallback that carries on fetching from somewhere else.
        def _fail(*_args: Any, **_kwargs: Any) -> Any:
            raise JobCancelled("cancelled")

        monkeypatch.setattr(whisper_local.requests, "get", _fail)

        with pytest.raises(JobCancelled):
            whisper_local.download_from_modelscope("small", _ctx(tmp_path))


class TestModelIsCached:
    def test_a_complete_mirror_copy_counts(self, tmp_path: Path, cache: Path) -> None:
        _fill(_mirror_dir(cache))
        assert whisper_local.model_is_cached("small") is True

    def test_it_does_not_count_with_the_mirrors_off(
        self, tmp_path: Path, cache: Path, mirrors_off: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The files are on disk but nothing would use them, so answering "cached"
        # would promise an offline run that then starts a Hugging Face download.
        _fill(_mirror_dir(cache))
        monkeypatch.setattr(whisper_local, "_load_download_model", _raise_import_error)
        assert whisper_local.model_is_cached("small") is False

    def test_a_partial_mirror_copy_does_not_count(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fill(_mirror_dir(cache), skip="model.bin")
        monkeypatch.setattr(whisper_local, "_load_download_model", _raise_import_error)
        assert whisper_local.model_is_cached("small") is False


# ---------------------------------------------------------------------------
# The backend's use of it
# ---------------------------------------------------------------------------


class TestTranscribe:
    def test_a_missing_package_is_reported_before_any_download(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """480 MB must not be fetched only to learn the extra is not installed.

        The mirror path first shipped in the wrong order: the download ran first,
        so a machine without ``[asr-local]`` paid for a whole model and *then*
        got the "install the extra" error.
        """
        monkeypatch.setattr(whisper_local, "_load_faster_whisper", _raise_import_error)
        fetched: list[str] = []
        monkeypatch.setattr(
            whisper_local,
            "download_from_modelscope",
            lambda model, ctx: fetched.append(model) or None,
        )

        with pytest.raises(AsrBackendError, match="asr-local"):
            whisper_local.WhisperLocalBackend().transcribe(_audio(tmp_path), _ctx(tmp_path))

        assert fetched == [], "no bytes may be fetched before the package check"

    def test_the_mirrored_directory_is_handed_to_the_loader(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = _fill(_mirror_dir(cache))
        loader = _Loader()
        monkeypatch.setattr(whisper_local, "_load_faster_whisper", lambda: loader)

        # No injected loader: that is what makes the backend responsible for
        # obtaining the model, which is the path under test.
        whisper_local.WhisperLocalBackend(model="small").transcribe(
            _audio(tmp_path), _ctx(tmp_path)
        )

        assert loader.calls == [str(directory)], "the local directory replaces the size name"

    def test_with_the_mirrors_off_the_size_name_is_unchanged(
        self, tmp_path: Path, cache: Path, mirrors_off: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fill(_mirror_dir(cache))
        loader = _Loader()
        monkeypatch.setattr(whisper_local, "_load_faster_whisper", lambda: loader)

        whisper_local.WhisperLocalBackend(model="small").transcribe(
            _audio(tmp_path), _ctx(tmp_path)
        )

        assert loader.calls == ["small"], "unchanged from before this feature existed"

    def test_a_failed_mirror_download_still_transcribes(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End to end: the mirror fails, the official path is used, the job runs.
        def _fail(*_args: Any, **_kwargs: Any) -> Any:
            raise requests.ConnectionError("mirror is down")

        loader = _Loader()
        monkeypatch.setattr(whisper_local.requests, "get", _fail)
        monkeypatch.setattr(whisper_local, "_load_faster_whisper", lambda: loader)

        outcome = whisper_local.WhisperLocalBackend(model="small").transcribe(
            _audio(tmp_path), _ctx(tmp_path)
        )

        assert loader.calls == ["small"]
        assert outcome.origin.startswith("faster-whisper:small")

    def test_an_injected_loader_is_never_second_guessed(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A supplied loader owns model acquisition, so the mirror is not consulted.

        Also what keeps the rest of the ASR suite off the network.
        """
        _fill(_mirror_dir(cache))

        def _explode(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("a supplied loader must not trigger a download")

        monkeypatch.setattr(whisper_local, "download_from_modelscope", _explode)
        loader = _Loader()

        whisper_local.WhisperLocalBackend(model="small", loader=loader).transcribe(
            _audio(tmp_path), _ctx(tmp_path)
        )

        assert loader.calls == ["small"]
