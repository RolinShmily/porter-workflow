from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from porter.config import PorterConfig
from porter.context import RunContext
from porter.errors import JobCancelled
from porter.models.request import JobOptions
from porter.models.subtitle import TranscriptSentence
from porter.refine.llm import LLMTranscriptRefiner
from porter.refine.passthrough import PassthroughRefiner


def _ctx(tmp_path: Path, **kwargs: Any) -> RunContext:
    config = PorterConfig()
    options = JobOptions(output_dir=tmp_path)
    for key, value in kwargs.items():
        if hasattr(options, key):
            setattr(options, key, value)
        elif hasattr(config.refine, key):
            setattr(config.refine, key, value)
    return RunContext(job_id="test", options=options, config=config)


class _FakeChatCompletions:
    def __init__(self, response_text: str | BaseException) -> None:
        self.response_text = response_text
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.response_text, BaseException):
            raise self.response_text
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.response_text))]
        )


class _FakeClient:
    def __init__(self, response_text: str | BaseException) -> None:
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(response_text))


class TestPassthroughRefiner:
    def test_passthrough_leaves_sentences_unchanged(self, tmp_path: Path) -> None:
        refiner = PassthroughRefiner()
        ctx = _ctx(tmp_path)
        assert refiner.available(ctx) is True

        sentences = [
            TranscriptSentence(
                sentence_id=1,
                start_ms=0,
                end_ms=1000,
                en_text="hello world",
            )
        ]
        result = refiner.refine(sentences, ctx)
        assert result == sentences
        assert result[0].refined_en_text == ""
        assert result[0].source_text == "hello world"


class TestLLMTranscriptRefiner:
    def test_refines_sentences_and_populates_refined_text(self, tmp_path: Path) -> None:
        response_json = (
            '[{"id": 0, "text": "Hello, world!"}, '
            '{"id": 1, "text": "This is Python programming."}]'
        )
        client = _FakeClient(response_json)
        refiner = LLMTranscriptRefiner(client=client)
        ctx = _ctx(tmp_path)

        sentences = [
            TranscriptSentence(sentence_id=1, start_ms=0, end_ms=1000, en_text="hello world"),
            TranscriptSentence(sentence_id=2, start_ms=1000, end_ms=2000, en_text="this is python programing"),
        ]

        result = refiner.refine(sentences, ctx)
        assert len(result) == 2
        assert result[0].refined_en_text == "Hello, world!"
        assert result[0].source_text == "Hello, world!"
        assert result[1].refined_en_text == "This is Python programming."
        assert result[1].source_text == "This is Python programming."

    def test_degrades_gracefully_on_network_or_api_error(self, tmp_path: Path) -> None:
        client = _FakeClient(RuntimeError("Rate limit / 429"))
        refiner = LLMTranscriptRefiner(client=client)
        ctx = _ctx(tmp_path)

        sentences = [
            TranscriptSentence(sentence_id=1, start_ms=0, end_ms=1000, en_text="raw transcript"),
        ]

        # Must not raise: gracefully keeps raw transcript
        result = refiner.refine(sentences, ctx)
        assert len(result) == 1
        assert result[0].refined_en_text == ""
        assert result[0].source_text == "raw transcript"

    def test_degrades_gracefully_on_malformed_json(self, tmp_path: Path) -> None:
        client = _FakeClient("not valid json at all")
        refiner = LLMTranscriptRefiner(client=client)
        ctx = _ctx(tmp_path)

        sentences = [
            TranscriptSentence(sentence_id=1, start_ms=0, end_ms=1000, en_text="raw text"),
        ]

        result = refiner.refine(sentences, ctx)
        assert result[0].source_text == "raw text"

    def test_available_respects_options_and_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient("[]")
        refiner = LLMTranscriptRefiner(client=client)

        ctx = _ctx(tmp_path)
        assert refiner.available(ctx) is True

        ctx_disabled_opt = _ctx(tmp_path, refine=False)
        assert refiner.available(ctx_disabled_opt) is False

        ctx_disabled_cfg = _ctx(tmp_path, enabled=False)
        assert refiner.available(ctx_disabled_cfg) is False

    def test_cancellation_is_respected(self, tmp_path: Path) -> None:
        client = _FakeClient("[]")
        refiner = LLMTranscriptRefiner(client=client)
        ctx = _ctx(tmp_path)
        ctx.cancel.set()

        sentences = [
            TranscriptSentence(sentence_id=1, start_ms=0, end_ms=1000, en_text="hello"),
        ]
        with pytest.raises(JobCancelled):
            refiner.refine(sentences, ctx)

    def test_cancellation_raised_by_the_client_escapes_the_graceful_catch(
        self, tmp_path: Path
    ) -> None:
        """A cancel is the user's instruction, not a refinement failure.

        Without an explicit re-raise the batch handler's graceful-degradation
        catch would swallow ``JobCancelled`` and keep spending requests on the
        remaining batches -- the same bug the ASR chain guards against.
        """
        client = _FakeClient(JobCancelled("user pressed Ctrl-C"))
        refiner = LLMTranscriptRefiner(client=client)
        ctx = _ctx(tmp_path)

        sentences = [
            TranscriptSentence(sentence_id=1, start_ms=0, end_ms=1000, en_text="hello"),
        ]
        with pytest.raises(JobCancelled):
            refiner.refine(sentences, ctx)
