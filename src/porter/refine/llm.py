"""LLM-powered transcript proofreader and ASR corrector.

Receives whole sentences reconstructed from speech-to-text cues, repairs ASR
homophones, restores punctuation, capitalises proper nouns and acronyms, and
populates ``sentence.refined_en_text``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from porter.context import RunContext
from porter.logging import get_logger
from porter.models.subtitle import TranscriptSentence

__all__ = ["LLMTranscriptRefiner"]

_logger = get_logger(__name__)

_BATCH_SIZE = 20
_CLIENT_TIMEOUT_SECONDS = 30.0
_DEFAULT_BASE_URL = "https://api.openai.com/v1"

_SYSTEM_PROMPT = (
    "You are an expert audio transcript proofreader and video localization editor. "
    "Your task is to fix speech recognition (ASR) mistakes, missing punctuation, "
    "and proper noun capitalization in video transcripts. "
    "Respond ONLY in a valid JSON array."
)


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


class LLMTranscriptRefiner:
    """Proofreads sentences via an OpenAI-compatible chat completions endpoint."""

    name = "llm-refiner"

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def available(self, ctx: RunContext) -> bool:
        """Whether refinement is enabled and LLM credentials exist."""
        if not ctx.options.refine or not ctx.config.refine.enabled:
            return False
        if self._client is not None:
            return True
        api_key = ctx.config.llm.api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return False
        return _load_openai() is not None

    def refine(
        self,
        sentences: list[TranscriptSentence],
        ctx: RunContext,
    ) -> list[TranscriptSentence]:
        """Proofread and correct ``sentences``, filling ``refined_en_text``."""
        if not sentences:
            return sentences

        if not self.available(ctx):
            return sentences

        client = self._client_for(ctx)
        if client is None:
            return sentences

        model = (
            ctx.config.refine.model
            or ctx.options.llm_model
            or ctx.config.llm.model
        )

        ctx.logger.info("refining %d transcript sentences via LLM (%s)", len(sentences), model)

        for start in range(0, len(sentences), _BATCH_SIZE):
            ctx.check_cancelled()
            batch = sentences[start : start + _BATCH_SIZE]
            self._refine_batch(client, model, batch, start, ctx)

        return sentences

    # -- internals ----------------------------------------------------------

    def _client_for(self, ctx: RunContext) -> Any:
        if self._client is not None:
            return self._client

        openai_cls = _load_openai()
        if openai_cls is None:
            return None

        api_key = ctx.config.llm.api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None

        base_url = (
            ctx.config.llm.api_base
            or os.environ.get("OPENAI_BASE_URL")
            or _DEFAULT_BASE_URL
        )
        return openai_cls(api_key=api_key, base_url=base_url, timeout=_CLIENT_TIMEOUT_SECONDS)

    def _refine_batch(
        self,
        client: Any,
        model: str,
        batch: list[TranscriptSentence],
        base_index: int,
        ctx: RunContext,
    ) -> None:
        """Call the LLM to proofread one batch. Degrades gracefully on failure."""
        json_repair = _load_json_repair()
        if json_repair is None:
            _logger.warning("json_repair not available; skipping LLM refinement batch")
            return

        batch_payload = [
            {"id": base_index + offset, "text": sentence.en_text}
            for offset, sentence in enumerate(batch)
        ]

        prompt = (
            "You are reviewing an automatic speech recognition (ASR) transcript of a video.\n"
            "Instructions:\n"
            "1. Fix speech recognition mis-hearings, typos, and homophones (e.g. 'their' vs 'there', domain jargon).\n"
            "2. Correct capitalization of proper nouns, names, brands, tech stacks, and acronyms.\n"
            "3. Restore appropriate punctuation (. , ? !) so sentences are grammatically sound and readable.\n"
            "4. DO NOT translate into other languages. Keep the language exactly as original (English).\n"
            "5. Preserve conversational dialogue flow and oral tone.\n"
            "6. Output MUST be a strict JSON array of objects with keys: 'id' (number), 'text' (proofread text).\n\n"
            f"Input sentences:\n{json.dumps(batch_payload, ensure_ascii=False)}"
        )

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            )
            content = response.choices[0].message.content or ""
            parsed = json_repair.loads(content)
            if not isinstance(parsed, list):
                _logger.warning("refinement LLM returned non-array JSON; skipping batch")
                return

            by_id: dict[int, str] = {}
            for item in parsed:
                if isinstance(item, dict):
                    item_id = item.get("id")
                    item_text = item.get("text")
                    if isinstance(item_id, int) and isinstance(item_text, str) and item_text.strip():
                        by_id[item_id] = item_text.strip()

            for offset, sentence in enumerate(batch):
                cid = base_index + offset
                if cid in by_id:
                    sentence.refined_en_text = by_id[cid]

        except Exception as exc:
            _logger.warning("LLM transcript refinement batch failed (%s); keeping raw ASR text", exc)
