"""Translation backends: v0.1 ports plus the guarantees the refactor adds.

Two groups of tests live here.

**Ported assertions.** ``tests/regression/`` holds v0.1's subtitle tests; the
five translation tests that could not move verbatim (their call form changed from
``SubtitleItem``/``TranscriptSentence`` lists to plain string lists) are ported
here with the same inputs, mocks and expected values. Each says which v0.1 test
it comes from and what changed.

**New coverage.** The refactor's stated bug was that a partial failure returned a
*shorter* list, silently misaligning every later cue, and that nothing could be
cancelled. So every backend is tested for:

* ``len(outcome.texts) == len(inputs)``, including blank inputs;
* ``TranslationBackendError`` — not an arbitrary exception — on 429/500/malformed
  JSON;
* an ``available()`` that returns ``False`` instead of raising when its
  dependency is missing;
* prompt ``JobCancelled`` on Ctrl-C.

No test touches the network, ffmpeg, or the ``videocaptioner`` binary.
"""

from __future__ import annotations

import builtins
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from porter.config import LLMConfig, PorterConfig
from porter.context import RunContext
from porter.errors import JobCancelled
from porter.models.request import JobOptions
from porter.subtitles.srt import parse_srt
from porter.translate import bing, google, llm, mymemory, videocaptioner
from porter.translate.base import TranslationBackendError

#: A translator page carrying the three scraped values Bing needs. Copied from
#: v0.1's ``test_translate_sentences_with_bing_http_mock``.
BING_PAGE = (
    'IG:"1234567890ABCDEF" data-iid="translator.5025" '
    'params_AbusePreventionHelper = [1788099292818,"mock_token_key",3600000];'
)


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeHTTP:
    """Stand-in for the ``requests`` module.

    Implements only ``get``/``post``/``Session`` — the surface the backends use —
    and records the calls so tests can assert on timeouts and payload keys.
    """

    def __init__(
        self,
        get_responses: list[FakeResponse] | None = None,
        post_responses: list[FakeResponse] | None = None,
        get_handler: Any = None,
    ) -> None:
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self._get_handler = get_handler

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        if self._get_handler is not None:
            return self._get_handler(url, kwargs)
        return self._pop(self.get_responses)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append((url, kwargs))
        return self._pop(self.post_responses)

    def Session(self) -> FakeHTTP:  # noqa: N802 - mirrors requests.Session
        # The backends only need one session; returning self keeps call recording
        # on a single object.
        return self

    @staticmethod
    def _pop(responses: list[FakeResponse]) -> FakeResponse:
        if not responses:
            raise AssertionError("unexpected HTTP request")
        return responses.pop(0)


def _install(monkeypatch: pytest.MonkeyPatch, module: Any, fake: FakeHTTP) -> None:
    """Point a backend module's lazy ``requests`` import at ``fake``.

    Patched on *our* module, never on the global ``requests`` package, so a bug
    in one test cannot leak into another module's import.
    """
    monkeypatch.setattr(module, "_load_requests", lambda: fake)


def _block_import(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``import <name>`` raise ``ImportError`` for the duration of a test."""
    real_import = builtins.__import__

    def _fake_import(module_name: str, *args: Any, **kwargs: Any) -> Any:
        if module_name == name or module_name.startswith(name + "."):
            raise ImportError(f"blocked import of {name}")
        return real_import(module_name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)


def _explode() -> Any:
    raise RuntimeError("probe exploded")


def _google_payload(lines: list[str]) -> list[Any]:
    """A Google response whose joined segments split back into ``lines``."""
    return [[["=====".join(lines), "source"]]]


def _bilingual_srt_text(targets: list[str], sources: list[str]) -> str:
    blocks = [
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{target}\n{source}"
        for index, (target, source) in enumerate(zip(targets, sources, strict=False), start=1)
    ]
    return "\n\n".join(blocks) + "\n"


def _install_fake_cli(
    monkeypatch: pytest.MonkeyPatch,
    translated: list[str],
    returncode: int = 0,
) -> SimpleNamespace:
    """Replace the ``videocaptioner`` subprocess with one that writes an SRT.

    The fake reads the input SRT the backend wrote and emits a bilingual output
    (target line above source line, matching ``--layout target-above``), so the
    real parsing path runs. ``calls`` records the argv of every invocation.
    """
    calls: list[list[str]] = []

    def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        input_path = Path(command[2])
        output = Path(command[command.index("-o") + 1])
        if returncode == 0:
            sources = [
                item.source_text
                for item in parse_srt(input_path.read_text(encoding="utf-8"))
            ]
            output.write_text(_bilingual_srt_text(translated, sources), encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr="boom")

    fake = SimpleNamespace(run=_run, TimeoutExpired=subprocess.TimeoutExpired, calls=calls)
    monkeypatch.setattr(videocaptioner, "subprocess", fake)
    return fake


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(job_id="test", options=JobOptions(output_dir=tmp_path / "out"))


@pytest.fixture
def llm_ctx(tmp_path: Path) -> RunContext:
    config = PorterConfig(llm=LLMConfig(api_key="sk-test-mock"))
    return RunContext(job_id="test", options=JobOptions(output_dir=tmp_path / "out"), config=config)


def _llm_client(content: str) -> MagicMock:
    client = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    client.chat.completions.create.return_value = response
    return client


# ----------------------------------------------------------------------
# Ported v0.1 assertions
# ----------------------------------------------------------------------


class TestPortedV01Assertions:
    """v0.1 ``tests/test_subtitle.py`` translation tests, ported.

    The only change is the call form: v0.1 passed ``SubtitleItem`` /
    ``TranscriptSentence`` lists and read ``.target_text`` / ``.zh_text``; the
    v0.2 contract takes ``list[str]`` and returns ``outcome.texts``. Assertions
    are otherwise unchanged.
    """

    def test_translate_with_google_http_mock(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """From ``test_translate_with_google_http_mock`` (line 268)."""
        fake = FakeHTTP(get_responses=[FakeResponse(200, [[["你好", "Hello"]]])])
        _install(monkeypatch, google, fake)

        outcome = google.GoogleTranslateBackend().translate_texts(["Hello"], "zh-CN", ctx)

        assert len(outcome.texts) == 1
        assert outcome.texts[0] == "你好"
        assert outcome.origin == "google"

    def test_translate_sentences_with_google_http_mock(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """From ``test_translate_sentences_with_google_http_mock`` (line 282)."""
        fake = FakeHTTP(get_responses=[FakeResponse(200, [[["你好世界", "Hello world."]]])])
        _install(monkeypatch, google, fake)

        outcome = google.GoogleTranslateBackend().translate_texts(["Hello world."], "zh-CN", ctx)

        assert len(outcome.texts) == 1
        assert outcome.texts[0] == "你好世界"

    def test_translate_sentences_with_direct_llm_mock(self, llm_ctx: RunContext) -> None:
        """From ``test_translate_sentences_with_direct_llm_mock`` (line 304).

        Changed assertion input: v0.1 keyed the JSON entry by
        ``TranscriptSentence.sentence_id``, which was ``1`` for its single
        sentence. The string-level contract has no sentence id, so the input's
        0-based index is used and the mocked entry says ``"id": 0``.
        """
        client = _llm_client('[{"id": 0, "en": "Hello world.", "zh": "你好，世界。"}]')  # noqa: RUF001

        outcome = llm.LLMTranslationBackend(client=client).translate_texts(
            ["Hello world."], "zh-Hans", llm_ctx
        )

        assert len(outcome.texts) == 1
        assert outcome.texts[0] == "你好，世界。"  # noqa: RUF001
        assert outcome.origin == "llm"

    def test_translate_with_mymemory_http(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """From ``test_translate_with_mymemory_http`` (line 448)."""
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, {"responseData": {"translatedText": "你好"}})]
        )
        _install(monkeypatch, mymemory, fake)

        outcome = mymemory.MyMemoryBackend().translate_texts(["Hello"], "zh-CN", ctx)

        assert len(outcome.texts) == 1
        assert outcome.texts[0] == "你好"
        assert outcome.origin == "mymemory"

    def test_translate_sentences_with_bing_http_mock(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """From ``test_translate_sentences_with_bing_http_mock`` (line 465).

        v0.1 also asserted the ``SubtitleItem`` wrapper produced the same text;
        that wrapper no longer exists, so only the string-level assertion is kept.
        """
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, text=BING_PAGE)],
            post_responses=[
                FakeResponse(200, [{"translations": [{"text": "我们需要快速行动。"}]}])
            ],
        )
        _install(monkeypatch, bing, fake)

        outcome = bing.BingTranslateBackend().translate_texts(
            ["We need to move fast."], "zh-Hans", ctx
        )

        assert len(outcome.texts) == 1
        assert outcome.texts[0] == "我们需要快速行动。"
        assert outcome.origin == "bing"


# ----------------------------------------------------------------------
# Alignment: length is preserved, always
# ----------------------------------------------------------------------


class TestAlignment:
    def test_google_keeps_blank_inputs_in_place(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(
            get_responses=[
                FakeResponse(200, [[["你好", "Hello"]]]),
                FakeResponse(200, [[["世界", "World"]]]),
            ]
        )
        _install(monkeypatch, google, fake)

        texts = ["Hello", "", "   ", "World"]
        outcome = google.GoogleTranslateBackend().translate_texts(texts, "zh-CN", ctx)

        assert outcome.texts == ["你好", "", "   ", "世界"]
        assert len(outcome.texts) == len(texts)

    def test_bing_keeps_blank_inputs_in_place(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, text=BING_PAGE)],
            post_responses=[
                FakeResponse(200, [{"translations": [{"text": "你好"}]}]),
                FakeResponse(200, [{"translations": [{"text": "世界"}]}]),
            ],
        )
        _install(monkeypatch, bing, fake)

        texts = ["Hello", "", "  ", "World"]
        outcome = bing.BingTranslateBackend().translate_texts(texts, "zh-Hans", ctx)

        assert outcome.texts == ["你好", "", "  ", "世界"]

    def test_mymemory_keeps_blank_inputs_in_place(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(
            get_responses=[
                FakeResponse(200, {"responseData": {"translatedText": "你好"}}),
                FakeResponse(200, {"responseData": {"translatedText": "世界"}}),
            ]
        )
        _install(monkeypatch, mymemory, fake)

        texts = ["Hello", "", "  ", "World"]
        outcome = mymemory.MyMemoryBackend().translate_texts(texts, "zh-CN", ctx)

        assert outcome.texts == ["你好", "", "  ", "世界"]

    def test_llm_keeps_blank_inputs_in_place(self, llm_ctx: RunContext) -> None:
        client = _llm_client(
            json.dumps(
                [
                    {"id": 0, "zh": "你好"},
                    {"id": 1, "zh": "（空）"},  # noqa: RUF001
                    {"id": 3, "zh": "世界"},
                ]
            )
        )

        texts = ["Hello", "", "  ", "World"]
        outcome = llm.LLMTranslationBackend(client=client).translate_texts(
            texts, "zh-Hans", llm_ctx
        )

        # Blank inputs keep their original bytes even when the model invents text
        # for them; the invariant is positional, not "whatever came back".
        assert outcome.texts == ["你好", "", "  ", "世界"]

    def test_videocaptioner_keeps_blank_inputs_in_place(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_cli(monkeypatch, ["你好", "世界"])

        texts = ["Hello", "", "  ", "World"]
        outcome = videocaptioner.VideocaptionerBackend(binary="/usr/bin/videocaptioner").translate_texts(
            texts, "zh-Hans", ctx
        )

        assert outcome.texts == ["你好", "", "  ", "世界"]

    def test_google_batch_failure_raises_instead_of_truncating(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.1 returned a short list here, misaligning every later cue."""
        # Batch 1 (15 cues) succeeds; batch 2 (the 16th) fails.
        good = FakeResponse(200, _google_payload([f"译文{index}" for index in range(15)]))
        fake = FakeHTTP(
            get_responses=[good, FakeResponse(500), FakeResponse(500)],
        )
        _install(monkeypatch, google, fake)

        texts = [f"line {index}" for index in range(16)]
        with pytest.raises(TranslationBackendError):
            google.GoogleTranslateBackend().translate_texts(texts, "zh-CN", ctx)

    def test_videocaptioner_cue_count_mismatch_raises(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_cli(monkeypatch, ["只有一句"])

        with pytest.raises(TranslationBackendError):
            videocaptioner.VideocaptionerBackend(binary="/usr/bin/videocaptioner").translate_texts(
                ["Hello", "World"], "zh-Hans", ctx
            )


# ----------------------------------------------------------------------
# Expected failures raise TranslationBackendError
# ----------------------------------------------------------------------


class TestExpectedFailures:
    @pytest.mark.parametrize("status", [429, 500])
    def test_google_raises_on_http_error(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        fake = FakeHTTP(get_responses=[FakeResponse(status), FakeResponse(status)])
        _install(monkeypatch, google, fake)

        with pytest.raises(TranslationBackendError):
            google.GoogleTranslateBackend().translate_texts(["Hello"], "zh-CN", ctx)

    def test_google_raises_on_malformed_json(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(
            get_responses=[
                FakeResponse(200, ValueError("not json")),
                FakeResponse(200, ValueError("not json")),
            ]
        )
        _install(monkeypatch, google, fake)

        with pytest.raises(TranslationBackendError):
            google.GoogleTranslateBackend().translate_texts(["Hello"], "zh-CN", ctx)

    def test_google_raises_on_unrecognised_payload(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, {"unexpected": True})] * 2,
        )
        _install(monkeypatch, google, fake)

        with pytest.raises(TranslationBackendError):
            google.GoogleTranslateBackend().translate_texts(["Hello"], "zh-CN", ctx)

    @pytest.mark.parametrize("status", [429, 500])
    def test_bing_raises_on_http_error(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, text=BING_PAGE)],
            post_responses=[FakeResponse(status)],
        )
        _install(monkeypatch, bing, fake)

        with pytest.raises(TranslationBackendError):
            bing.BingTranslateBackend().translate_texts(["Hello"], "zh-Hans", ctx)

    def test_bing_raises_when_the_page_no_longer_parses(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(get_responses=[FakeResponse(200, text="<html>changed</html>")])
        _install(monkeypatch, bing, fake)

        with pytest.raises(TranslationBackendError):
            bing.BingTranslateBackend().translate_texts(["Hello"], "zh-Hans", ctx)

    def test_bing_raises_on_malformed_json(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, text=BING_PAGE)],
            post_responses=[FakeResponse(200, ValueError("not json"))],
        )
        _install(monkeypatch, bing, fake)

        with pytest.raises(TranslationBackendError):
            bing.BingTranslateBackend().translate_texts(["Hello"], "zh-Hans", ctx)

    @pytest.mark.parametrize("status", [429, 500])
    def test_mymemory_raises_on_http_error(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        fake = FakeHTTP(get_responses=[FakeResponse(status)])
        _install(monkeypatch, mymemory, fake)

        with pytest.raises(TranslationBackendError):
            mymemory.MyMemoryBackend().translate_texts(["Hello"], "zh-CN", ctx)

    def test_mymemory_raises_on_malformed_json(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(get_responses=[FakeResponse(200, ValueError("not json"))])
        _install(monkeypatch, mymemory, fake)

        with pytest.raises(TranslationBackendError):
            mymemory.MyMemoryBackend().translate_texts(["Hello"], "zh-CN", ctx)

    def test_mymemory_raises_on_undocumented_shape(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(get_responses=[FakeResponse(200, {"unexpected": True})])
        _install(monkeypatch, mymemory, fake)

        with pytest.raises(TranslationBackendError):
            mymemory.MyMemoryBackend().translate_texts(["Hello"], "zh-CN", ctx)

    def test_mymemory_quota_warning_is_not_a_failure(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A quota warning is an answer, not a transport error: keep the input."""
        fake = FakeHTTP(
            get_responses=[
                FakeResponse(200, {"responseData": {"translatedText": "MYMEMORY WARNING: quota"}})
            ]
        )
        _install(monkeypatch, mymemory, fake)

        outcome = mymemory.MyMemoryBackend().translate_texts(["Hello"], "zh-CN", ctx)

        assert outcome.texts == ["Hello"]

    def test_llm_raises_on_request_error(self, llm_ctx: RunContext) -> None:
        client = MagicMock()
        client.chat.completions.create.side_effect = OSError("connection reset")

        with pytest.raises(TranslationBackendError):
            llm.LLMTranslationBackend(client=client).translate_texts(["Hello"], "zh-Hans", llm_ctx)

    def test_llm_maps_an_openai_status_error(
        self, llm_ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real 429/500 arrives as ``openai.OpenAIError``, not ``OSError``."""

        class FakeAPIStatusError(Exception):
            pass

        monkeypatch.setattr(llm, "_openai_error_types", lambda: (FakeAPIStatusError,))
        client = MagicMock()
        client.chat.completions.create.side_effect = FakeAPIStatusError("429 rate limited")

        with pytest.raises(TranslationBackendError):
            llm.LLMTranslationBackend(client=client).translate_texts(["Hello"], "zh-Hans", llm_ctx)

    def test_llm_raises_on_non_array_response(self, llm_ctx: RunContext) -> None:
        client = _llm_client("this is not json at all")

        with pytest.raises(TranslationBackendError):
            llm.LLMTranslationBackend(client=client).translate_texts(["Hello"], "zh-Hans", llm_ctx)

    def test_videocaptioner_raises_when_the_binary_is_missing(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(videocaptioner, "_get_videocaptioner_bin", lambda: None)

        with pytest.raises(TranslationBackendError):
            videocaptioner.VideocaptionerBackend().translate_texts(["Hello"], "zh-Hans", ctx)

    def test_videocaptioner_raises_on_non_zero_exit(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_cli(monkeypatch, [], returncode=1)

        with pytest.raises(TranslationBackendError):
            videocaptioner.VideocaptionerBackend(binary="/usr/bin/videocaptioner").translate_texts(
                ["Hello"], "zh-Hans", ctx
            )


# ----------------------------------------------------------------------
# LLM-specific alignment (json_repair must not reorder)
# ----------------------------------------------------------------------


class TestLLMAlignment:
    def test_reordered_json_is_mapped_by_id(self, llm_ctx: RunContext) -> None:
        client = _llm_client(
            json.dumps([{"id": 1, "zh": "世界"}, {"id": 0, "zh": "你好"}])
        )

        outcome = llm.LLMTranslationBackend(client=client).translate_texts(
            ["Hello", "World"], "zh-Hans", llm_ctx
        )

        assert outcome.texts == ["你好", "世界"]

    def test_missing_id_falls_back_to_the_input_in_place(self, llm_ctx: RunContext) -> None:
        client = _llm_client(json.dumps([{"id": 0, "zh": "你好"}]))

        outcome = llm.LLMTranslationBackend(client=client).translate_texts(
            ["Hello", "World"], "zh-Hans", llm_ctx
        )

        assert outcome.texts == ["你好", "World"]

    def test_duplicate_and_unusable_entries_are_ignored(self, llm_ctx: RunContext) -> None:
        client = _llm_client(
            json.dumps(
                [
                    {"id": 0, "zh": "你好"},
                    {"id": 0, "zh": "覆盖"},
                    {"id": "not-an-int", "zh": "忽略"},
                    "not an object",
                ]
            )
        )

        outcome = llm.LLMTranslationBackend(client=client).translate_texts(
            ["Hello", "World"], "zh-Hans", llm_ctx
        )

        assert len(outcome.texts) == 2
        assert outcome.texts == ["覆盖", "World"]

    def test_missing_json_repair_is_reported_cleanly(
        self, llm_ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llm, "_load_json_repair", lambda: None)
        client = _llm_client("[]")

        with pytest.raises(TranslationBackendError):
            llm.LLMTranslationBackend(client=client).translate_texts(["Hello"], "zh-Hans", llm_ctx)


# ----------------------------------------------------------------------
# available() never raises
# ----------------------------------------------------------------------


class TestAvailableNeverRaises:
    def test_google_false_when_the_import_is_blocked(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _block_import(monkeypatch, "requests")
        assert google.GoogleTranslateBackend().available(ctx) is False

    def test_bing_false_when_the_import_is_blocked(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _block_import(monkeypatch, "requests")
        assert bing.BingTranslateBackend().available(ctx) is False

    def test_mymemory_false_when_the_import_is_blocked(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _block_import(monkeypatch, "requests")
        assert mymemory.MyMemoryBackend().available(ctx) is False

    def test_llm_false_when_the_import_is_blocked(
        self, llm_ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _block_import(monkeypatch, "openai")
        assert llm.LLMTranslationBackend().available(llm_ctx) is False

    def test_llm_false_without_a_key(self, ctx: RunContext) -> None:
        assert llm.LLMTranslationBackend().available(ctx) is False

    def test_http_backends_survive_an_exploding_probe(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for module, backend in (
            (google, google.GoogleTranslateBackend()),
            (bing, bing.BingTranslateBackend()),
            (mymemory, mymemory.MyMemoryBackend()),
        ):
            monkeypatch.setattr(module, "_load_requests", _explode)
            assert backend.available(ctx) is False

    def test_llm_survives_an_exploding_probe(
        self, llm_ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llm, "_load_openai", _explode)
        assert llm.LLMTranslationBackend().available(llm_ctx) is False

    def test_videocaptioner_false_without_a_binary(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(videocaptioner, "_get_videocaptioner_bin", lambda: None)
        assert videocaptioner.VideocaptionerBackend().available(ctx) is False

    def test_videocaptioner_survives_an_exploding_probe(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(videocaptioner, "_get_videocaptioner_bin", _explode)
        assert videocaptioner.VideocaptionerBackend().available(ctx) is False

    def test_videocaptioner_llm_requires_a_key(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            videocaptioner, "_get_videocaptioner_bin", lambda: "/usr/bin/videocaptioner"
        )
        assert videocaptioner.VideocaptionerLLMBackend().available(ctx) is False

    def test_videocaptioner_llm_true_with_binary_and_key(
        self, llm_ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            videocaptioner, "_get_videocaptioner_bin", lambda: "/usr/bin/videocaptioner"
        )
        assert videocaptioner.VideocaptionerLLMBackend().available(llm_ctx) is True


# ----------------------------------------------------------------------
# Cancellation
# ----------------------------------------------------------------------


class TestCancellation:
    def test_mymemory_cancels_between_strings(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _handler(url: str, kwargs: dict[str, Any]) -> FakeResponse:
            ctx.cancel.set()
            return FakeResponse(200, {"responseData": {"translatedText": "你好"}})

        _install(monkeypatch, mymemory, FakeHTTP(get_handler=_handler))

        with pytest.raises(JobCancelled):
            mymemory.MyMemoryBackend().translate_texts(["one", "two", "three"], "zh-CN", ctx)

    def test_google_cancels_between_batches(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _handler(url: str, kwargs: dict[str, Any]) -> FakeResponse:
            ctx.cancel.set()
            return FakeResponse(200, _google_payload([f"译文{index}" for index in range(15)]))

        _install(monkeypatch, google, FakeHTTP(get_handler=_handler))

        with pytest.raises(JobCancelled):
            google.GoogleTranslateBackend().translate_texts(
                [f"line {index}" for index in range(16)], "zh-CN", ctx
            )

    def test_google_cancels_before_the_first_request(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(get_responses=[])
        _install(monkeypatch, google, fake)
        ctx.cancel.set()

        with pytest.raises(JobCancelled):
            google.GoogleTranslateBackend().translate_texts(["Hello"], "zh-CN", ctx)

        assert fake.get_calls == []

    def test_videocaptioner_cancels_before_running(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_cli(monkeypatch, ["你好"])
        ctx.cancel.set()

        with pytest.raises(JobCancelled):
            videocaptioner.VideocaptionerBackend(binary="/usr/bin/videocaptioner").translate_texts(
                ["Hello"], "zh-Hans", ctx
            )


# ----------------------------------------------------------------------
# Wire-format fidelity and the videocaptioner adapter
# ----------------------------------------------------------------------


class TestWireFormat:
    def test_google_sends_v01_parameters_and_timeout(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(get_responses=[FakeResponse(200, [[["你好", "Hello"]]])])
        _install(monkeypatch, google, fake)

        google.GoogleTranslateBackend().translate_texts(["Hello"], "zh-CN", ctx)

        url, kwargs = fake.get_calls[0]
        assert url == "https://translate.googleapis.com/translate_a/single"
        assert kwargs["params"]["client"] == "gtx"
        assert kwargs["params"]["tl"] == "zh-CN"
        assert kwargs["params"]["q"] == "Hello"
        assert kwargs["timeout"] == 12.0

    def test_bing_sends_v01_payload_and_timeout(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, text=BING_PAGE)],
            post_responses=[
                FakeResponse(200, [{"translations": [{"text": "你好"}]}])
            ],
        )
        _install(monkeypatch, bing, fake)

        bing.BingTranslateBackend().translate_texts(["Hello"], "zh-Hans", ctx)

        page_url, page_kwargs = fake.get_calls[0]
        assert page_url == "https://www.bing.com/translator"
        assert page_kwargs["timeout"] == 10.0

        post_url, post_kwargs = fake.post_calls[0]
        assert "ttranslatev3" in post_url
        assert post_kwargs["data"]["fromLang"] == "auto-detect"
        assert post_kwargs["data"]["key"] == "1788099292818"
        assert post_kwargs["data"]["token"] == "mock_token_key"  # noqa: S105 - fixture value
        assert post_kwargs["timeout"] == 15.0

    def test_mymemory_sends_v01_parameters_and_timeout(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, {"responseData": {"translatedText": "你好"}})]
        )
        _install(monkeypatch, mymemory, fake)

        mymemory.MyMemoryBackend().translate_texts(["Hello"], "zh-CN", ctx)

        url, kwargs = fake.get_calls[0]
        assert url == "https://api.mymemory.translated.net/get"
        assert kwargs["params"]["langpair"] == "en|zh-CN"
        assert kwargs["timeout"] == 8.0


class TestVideocaptionerAdapter:
    def test_free_engine_builds_the_v01_command(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_cli(monkeypatch, ["你好"])
        backend = videocaptioner.VideocaptionerBackend(
            binary="/usr/bin/videocaptioner", engine="google"
        )

        outcome = backend.translate_texts(["Hello"], "zh-Hans", ctx)

        assert outcome.texts == ["你好"]
        assert outcome.origin == "videocaptioner"

    def test_free_engine_command_flags(
        self, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install_fake_cli(monkeypatch, ["你好"])

        videocaptioner.VideocaptionerBackend(
            binary="/usr/bin/videocaptioner", engine="google"
        ).translate_texts(["Hello"], "zh-Hans", ctx)

        command = fake.calls[0]
        assert command[0] == "/usr/bin/videocaptioner"
        assert command[1] == "subtitle"
        assert "--target-language" in command
        assert "zh-Hans" in command
        assert "--layout" in command
        assert "target-above" in command
        assert "--translator" in command
        assert "google" in command
        assert "--no-optimize" in command
        assert "--no-split" in command

    def test_llm_engine_forwards_credentials(
        self, llm_ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install_fake_cli(monkeypatch, ["你好"])
        config = PorterConfig(
            llm=LLMConfig(
                api_key="sk-test-mock",
                api_base="https://example.invalid/v1",
                model="deepseek-chat",
            )
        )
        llm_ctx.config = config

        videocaptioner.VideocaptionerLLMBackend(
            binary="/usr/bin/videocaptioner"
        ).translate_texts(["Hello"], "zh-Hans", llm_ctx)

        command = fake.calls[0]
        assert "llm" in command
        assert "--api-key" in command
        assert "sk-test-mock" in command
        assert "--api-base" in command
        assert "https://example.invalid/v1" in command
        assert "--model" in command
        assert "deepseek-chat" in command


class TestBingBatching:
    """Bing's batch path, which was silently unusable.

    Bing now answers with ``"usedLLM": true`` -- it is a model rewriting the blob --
    and it **collapses runs of newlines**. The batch joined cues with ``"\\n\\n"``
    and split the answer on ``"\\n\\n"``, so a two-cue batch came back as one
    segment and the backend raised "wrong number of segments". Since ``bing`` sits
    ahead of ``google`` and ``mymemory`` in the chain, every job paid for a request
    that could not succeed.

    Verified against the live service: ``"\\n\\n"`` collapses, ``"[[|]]"`` survives,
    and three runs of a full 15-cue batch returned exactly 15 segments each.
    """

    def test_the_separator_is_not_a_bare_blank_line(self) -> None:
        """The exact regression: ``"\\n\\n"`` is what the translator normalises."""
        assert bing._DELIMITER != "\n\n"
        assert "[[" in bing._DELIMITER

    def test_a_batch_carries_the_separator(self, ctx: RunContext, monkeypatch) -> None:
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, text=BING_PAGE)],
            post_responses=[
                FakeResponse(
                    200,
                    [{"translations": [{"text": f"一{bing._DELIMITER}二{bing._DELIMITER}三"}]}],
                )
            ],
        )
        _install(monkeypatch, bing, fake)

        outcome = bing.BingTranslateBackend().translate_texts(["one", "two", "three"], "zh-Hans", ctx)

        assert outcome.texts == ["一", "二", "三"]
        assert bing._DELIMITER in fake.post_calls[0][1]["data"]["text"]
        assert len(fake.post_calls) == 1, "a healthy batch is one request"

    def test_a_collapsed_separator_degrades_to_one_request_per_cue(
        self, ctx: RunContext, monkeypatch
    ) -> None:
        """The fix's point: recover instead of failing the whole backend.

        Raising handed three cues to ``google`` because of a separator the
        translator had rewritten -- on a service that translates one cue at a time
        perfectly well. A batch is an optimisation; it must not be a contract.
        """
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, text=BING_PAGE)],
            post_responses=[
                # The batch: three inputs, one segment back.
                FakeResponse(200, [{"translations": [{"text": "你好世界早上好再见"}]}]),
                FakeResponse(200, [{"translations": [{"text": "你好世界"}]}]),
                FakeResponse(200, [{"translations": [{"text": "早上好"}]}]),
                FakeResponse(200, [{"translations": [{"text": "再见"}]}]),
            ],
        )
        _install(monkeypatch, bing, fake)

        outcome = bing.BingTranslateBackend().translate_texts(
            ["hello world", "good morning", "bye"], "zh-Hans", ctx
        )

        assert outcome.texts == ["你好世界", "早上好", "再见"]
        assert len(fake.post_calls) == 4, "one batch that failed to split, then one per cue"

    def test_the_fallback_preserves_length(self, ctx: RunContext, monkeypatch) -> None:
        """Length preservation is the chain's hard rule; degrading must not break it."""
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, text=BING_PAGE)],
            post_responses=[
                FakeResponse(200, [{"translations": [{"text": "合并了"}]}]),
                FakeResponse(200, [{"translations": [{"text": "一"}]}]),
                FakeResponse(200, [{"translations": [{"text": "二"}]}]),
            ],
        )
        _install(monkeypatch, bing, fake)

        outcome = bing.BingTranslateBackend().translate_texts(["one", "two"], "zh-Hans", ctx)

        assert len(outcome.texts) == 2

    def test_a_blank_cue_still_takes_the_per_cue_path(self, ctx: RunContext, monkeypatch) -> None:
        """Blanks cannot survive a delimiter join, so they never enter a batch."""
        fake = FakeHTTP(
            get_responses=[FakeResponse(200, text=BING_PAGE)],
            post_responses=[
                FakeResponse(200, [{"translations": [{"text": "你好"}]}]),
            ],
        )
        _install(monkeypatch, bing, fake)

        outcome = bing.BingTranslateBackend().translate_texts(["Hello", "   "], "zh-Hans", ctx)

        assert outcome.texts == ["你好", "   "]
