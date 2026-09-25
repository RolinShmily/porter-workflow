"""The translation chain: walk engines until one produces real Chinese.

Implements :class:`~porter.ports.Translator`. Owns three things v0.1 spread across
a 579-line translator module and a 723-line controller:

1. **The fallback order** (LLM -> Bing -> Google -> MyMemory -> CLI).
2. **The CJK self-check** — the guard against a "translation" that is not one.
3. **Writing the four output files** (bilingual/Chinese SRT and ASS).

## The CJK self-check is the reason this is a chain and not a for-loop

The failure mode it catches is specific and common: a key-free backend answers
HTTP 200 with the *input* echoed back — because it detected a bot, because the
language pair is unsupported, or because its quota ran out and it degraded to
pass-through. Every cue then has non-empty ``target_text`` that is still English.

Nothing downstream notices. Every cue carries non-empty target text, so the
subtitle looks translated, the BURN phase succeeds, and the operator gets a
bilingual video with two identical English tracks. The job reports DONE.

So each backend's output is checked for actual CJK before it is accepted, and a
backend that returns English is treated exactly like one that returned an error:
log it, try the next. :func:`porter.subtitles.phrasing.has_chinese_translation`
is the check, and it tests for CJK characters rather than for non-empty text.

## Skipping the work entirely

If the cues already carry Chinese — which happens when the ASR chain aligned the
platform's own Chinese track — no backend is called at all. The files are still
written, because TRANSLATE's contract is to produce them.

## Translation happens on sentences, not on cues

This is the difference between idiomatic Chinese and word-for-word Chinese, and
it is the one place where the *shape* of the data changes rather than just its
contents:

1. Fragmented ASR cues are merged and rebuilt into whole sentences
   (:func:`~porter.subtitles.transcript.reconstruct_sentences_from_fragments`).
2. Those sentences -- not the cues -- are what a backend is asked to translate.
   A translator that sees "the output of" cannot know it continues into "the
   encoder is wrong"; a sentence-level translator can.
3. Each translated sentence is cut back into timed cues
   (:func:`~porter.subtitles.transcript.split_chinese_sentence_into_cues`),
   splitting the Chinese first and then cutting the English to match.

So the cue list is *replaced*, not annotated, and the number of output cues can
differ from the number of input cues. That is intended.

An LLM backend may also return corrected English alongside the translation; when
it does, that corrected text replaces the source in the bilingual track, because
translating corrected English and then printing the uncorrected English next to it
would be visibly inconsistent.
"""

from __future__ import annotations

import time

from porter.context import RunContext
from porter.errors import JobCancelled, PorterError
from porter.events import ArtifactKind, ArtifactReady, Phase
from porter.logging import get_logger
from porter.models.subtitle import SubtitleItem, SubtitleSet, TranscriptSentence
from porter.subtitles.ass import (
    compute_adaptive_subtitle_style,
    generate_bilingual_ass,
    generate_zh_ass,
)
from porter.subtitles.phrasing import (
    has_chinese_translation,
    merge_short_fragments,
    normalize_subtitle_items,
)
from porter.subtitles.srt import generate_bilingual_srt, generate_zh_srt
from porter.subtitles.transcript import (
    reconstruct_sentences_from_fragments,
    save_transcript_json,
    save_transcript_txt,
    split_chinese_sentence_into_cues,
)
from porter.translate import reuse
from porter.translate.base import (
    TranslationBackend,
    TranslationBackendError,
    TranslationOutcome,
)

__all__ = ["TranslationChain"]

_logger = get_logger(__name__)

#: Fallback resolution when the media probe could not measure the video. 16:9 is
#: the safer guess for ASS styling: the style only controls text placement, and a
#: horizontal layout on a vertical video still renders readably, whereas the
#: reverse puts text off-screen. (This is why the P2 fix to ``probe.dimensions``
#: matters — the wrong answer here used to be *fabricated* as 1920x1080.)
_DEFAULT_WIDTH = 1920
_DEFAULT_HEIGHT = 1080


class TranslationChain:
    """Walk translation backends in order until one produces Chinese."""

    name = "chain"

    def __init__(self, backends: list[TranslationBackend] | None = None) -> None:
        self.backends: list[TranslationBackend] = list(backends or [])

    def add(self, backend: TranslationBackend) -> TranslationChain:
        """Append a backend. Returns self, so assembly is one expression."""
        self.backends.append(backend)
        return self

    # -- Translator ---------------------------------------------------------

    def available(self, ctx: RunContext) -> bool:
        """Whether any translation engine is usable.

        A ``False`` here does not make the job impossible: the platform may supply
        a Chinese track, and that is only known after PREPARE.
        """
        return any(_probe(backend, ctx) for backend in self.backends)

    def availability(self, ctx: RunContext) -> list[tuple[str, bool]]:
        """Each backend's name and whether its probe succeeds, in chain order.

        The mirror of :meth:`AsrChain.availability`, and for the same reason: a
        caller that reports the planned execution path must not re-derive the
        ordering, or the report and the pipeline drift apart.
        """
        return [(backend.name, _probe(backend, ctx)) for backend in self.backends]

    def translate(
        self,
        subtitles: SubtitleSet,
        target_lang: str,
        ctx: RunContext,
    ) -> SubtitleSet:
        """Populate target text and write the bilingual and Chinese outputs."""
        ctx.check_cancelled()
        ctx.progress(Phase.TRANSLATE, 0.0, "preparing translation")

        # Merge rolling shards first: YouTube's automatic captions arrive as
        # two- and three-word pieces, and neither sentence reconstruction nor a
        # translator can do anything useful with those.
        subtitles.items = merge_short_fragments(subtitles.items)
        sentences = reconstruct_sentences_from_fragments(subtitles.items)

        if has_chinese_translation(subtitles.items):
            ctx.logger.info("cues already carry Chinese; skipping every backend")
            ctx.progress(Phase.TRANSLATE, 0.5, "using the platform Chinese track")
            self._carry_platform_chinese(subtitles, sentences, ctx)
        else:
            outcome, reused = self._translate_or_reuse(sentences, target_lang, subtitles, ctx)
            subtitles.items = _cues_from_sentences(
                sentences, outcome.texts, outcome.sources
            )
            ctx.logger.info(
                "%s %d sentences into %d cues via %s",
                "reused the cached translation of" if reused else "translated",
                len(sentences),
                len(subtitles.items),
                outcome.origin,
            )

        self._write(sentences, subtitles, ctx)
        ctx.progress(Phase.TRANSLATE, 1.0, "subtitles written")
        return subtitles

    # -- internals ----------------------------------------------------------

    def _translate_or_reuse(
        self,
        sentences: list[TranscriptSentence],
        target_lang: str,
        subtitles: SubtitleSet,
        ctx: RunContext,
    ) -> tuple[TranslationOutcome, bool]:
        """The cached translation if it still applies, otherwise a fresh one.

        Returns ``(outcome, reused)`` so the caller can report which happened
        instead of claiming a translation that did not take place.

        The cache holds text, never the rendered files, so a hit still re-renders
        below -- see :mod:`porter.translate.reuse` for why that split is the whole
        point.
        """
        cooked = subtitles.transcript_json_path.parent
        expected = reuse.fingerprint(sentences, target_lang, ctx)

        if not ctx.options.force:
            cached = reuse.load(cooked, expected, len(sentences))
            if cached is not None:
                ctx.progress(Phase.TRANSLATE, 0.5, "reusing cached translation")
                return cached, True

        outcome = self._run(sentences, target_lang, ctx)
        reuse.save(cooked, expected, outcome)
        return outcome, False

    def _run(
        self,
        sentences: list[TranscriptSentence],
        target_lang: str,
        ctx: RunContext,
    ) -> TranslationOutcome:
        """Try each backend until one returns text that is actually Chinese."""
        inputs = [sentence.en_text for sentence in sentences]
        if not inputs:
            raise PorterError(
                "there are no sentences to translate",
                phase=Phase.TRANSLATE.value,
            )

        failures: list[str] = []
        attempted = 0

        for backend in self.backends:
            ctx.check_cancelled()

            if not _probe(backend, ctx):
                _logger.debug("skipping unavailable translation backend %s", backend.name)
                continue

            attempted += 1
            started = time.monotonic()

            try:
                outcome = backend.translate_texts(inputs, target_lang, ctx)
            except JobCancelled:
                # Not a backend failure. Trying the remaining engines would
                # ignore the user's Ctrl-C for another four network round-trips.
                raise
            except (TranslationBackendError, PorterError) as exc:
                failures.append(f"{backend.name}: {exc}")
                ctx.logger.warning(
                    "translation backend %s failed: %s", backend.name, exc
                )
                continue

            elapsed = time.monotonic() - started
            problem = _reject(outcome, inputs, backend.name)
            if problem is not None:
                failures.append(f"{backend.name}: {problem}")
                ctx.logger.warning(
                    "translation backend %s produced unusable output after %.1fs: %s",
                    backend.name,
                    elapsed,
                    problem,
                )
                continue

            ctx.logger.info(
                "translation backend %s translated %d sentences in %.1fs",
                backend.name,
                len(inputs),
                elapsed,
            )
            sources = outcome.sources if _sources_usable(outcome.sources, inputs) else None
            return TranslationOutcome(
                texts=outcome.texts,
                origin=outcome.origin or backend.name,
                sources=sources,
            )

        raise PorterError(
            "every translation backend failed",
            phase=Phase.TRANSLATE.value,
            attempted=attempted,
            failures=failures,
        )

    def _carry_platform_chinese(
        self,
        subtitles: SubtitleSet,
        sentences: list[TranscriptSentence],
        ctx: RunContext,
    ) -> None:
        """Copy the platform Chinese track onto the sentences and the transcript.

        When PREPARE fetched the platform's own Chinese subtitles, TRANSCRIBE
        aligned them onto the cues. The sentences rebuilt from those cues must
        carry that text too, or the transcript written below would show Chinese in
        the cues and ``(Pending Translation)`` in the script book.
        """
        by_fragment = {
            item.index: item.target_text for item in subtitles.items if item.target_text
        }
        for sentence in sentences:
            if sentence.zh_text:
                continue
            pieces = [
                by_fragment[index]
                for index in (sentence.fragment_indices or [])
                if by_fragment.get(index)
            ]
            sentence.zh_text = "".join(pieces)

    def _write(
        self,
        sentences: list[TranscriptSentence],
        subtitles: SubtitleSet,
        ctx: RunContext,
    ) -> None:
        """Write the transcript and all four subtitle files, and announce them.

        Both SRTs and both ASS files are always written, even in ``--burn skip``
        mode: they are the artifacts a person actually wants if they are going to
        re-encode or re-edit, and v0.1 wrote them unconditionally too.
        """
        width, height = _play_resolution(subtitles)

        # The transcript is written before the subtitles because it is the input
        # to translation, and it is the first artifact to read when a translation
        # looks wrong: it shows exactly what the translator was given.
        save_transcript_json(sentences, subtitles.transcript_json_path)
        save_transcript_txt(sentences, subtitles.transcript_txt_path)

        bilingual_style, zh_style, res_x, res_y = compute_adaptive_subtitle_style(
            width, height, ctx.config.style
        )
        subtitles.subtitle_bilingual_srt.write_text(
            generate_bilingual_srt(subtitles.items), encoding="utf-8"
        )
        subtitles.subtitle_zh_srt.write_text(
            generate_zh_srt(subtitles.items), encoding="utf-8"
        )
        subtitles.subtitle_bilingual_ass.write_text(
            generate_bilingual_ass(
                items=subtitles.items,
                style=bilingual_style,
                play_res_x=res_x,
                play_res_y=res_y,
            ),
            encoding="utf-8",
        )
        subtitles.subtitle_zh_ass.write_text(
            generate_zh_ass(
                items=subtitles.items,
                style=zh_style,
                play_res_x=res_x,
                play_res_y=res_y,
            ),
            encoding="utf-8",
        )

        for kind, path in (
            (ArtifactKind.TRANSCRIPT_JSON, subtitles.transcript_json_path),
            (ArtifactKind.TRANSCRIPT_TXT, subtitles.transcript_txt_path),
            (ArtifactKind.SUBTITLE_BILINGUAL_SRT, subtitles.subtitle_bilingual_srt),
            (ArtifactKind.SUBTITLE_ZH_SRT, subtitles.subtitle_zh_srt),
            (ArtifactKind.SUBTITLE_BILINGUAL_ASS, subtitles.subtitle_bilingual_ass),
            (ArtifactKind.SUBTITLE_ZH_ASS, subtitles.subtitle_zh_ass),
        ):
            ctx.emit(ArtifactReady(kind=kind, path=path, phase=Phase.TRANSLATE))


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _probe(backend: TranslationBackend, ctx: RunContext) -> bool:
    """``available()`` that cannot abort the chain.

    Logged at error level *with the traceback*, so a broken probe is visible
    rather than swallowed, while one bad backend does not take out the others.
    """
    try:
        return bool(backend.available(ctx))
    except Exception:
        ctx.logger.error(
            "translation backend %s probe raised", backend.name, exc_info=True
        )
        return False


def _reject(
    outcome: TranslationOutcome, inputs: list[str], name: str
) -> str | None:
    """Why this outcome is unusable, or ``None`` when it is fine.

    Two checks, in the order that produces the more useful message:

    1. **Alignment.** ``outcome.texts[i]`` must be the translation of ``inputs[i]``.
       A backend that drops an untranslatable element shifts every later cue, and
       the result is a file where every line is plausibly translated and attached
       to the wrong moment of the video. Checking length is cheap and catches it;
       checking alignment properly is not possible from here, so the contract is
       enforced by length plus the backend's own tests.
    2. **Real Chinese.** Without this the pass-through failure described in the
       module docstring reaches the render.
    """
    if len(outcome.texts) != len(inputs):
        return (
            f"returned {len(outcome.texts)} strings for {len(inputs)} inputs, "
            "which would misalign every subsequent cue"
        )

    joined = " ".join(outcome.texts)
    if not _has_cjk(joined):
        return "returned no Chinese characters (a pass-through or an unsupported pair)"

    return None


def _cues_from_sentences(
    sentences: list[TranscriptSentence],
    texts: list[str],
    sources: list[str] | None,
) -> list[SubtitleItem]:
    """Cut translated sentences back into timed cues.

    Cue indices are assigned sequentially across all sentences rather than per
    sentence, so ``split_chinese_sentence_into_cues``'s ``start_index`` argument
    is driven by the running total -- otherwise every sentence would restart at 1
    and the SRT would have duplicate indices.

    ``sources``, when the backend supplied corrected English, replaces the source
    text so the bilingual track shows the same English the translation was made
    from.
    """
    cues: list[SubtitleItem] = []
    for offset, sentence in enumerate(sentences):
        chinese = texts[offset] if offset < len(texts) else ""
        english = sentence.en_text
        if sources is not None and offset < len(sources) and sources[offset].strip():
            english = sources[offset]
        cues.extend(
            split_chinese_sentence_into_cues(
                en_text=english,
                zh_text=chinese,
                start_ms=sentence.start_ms,
                end_ms=sentence.end_ms,
                start_index=len(cues) + 1,
            )
        )

    # Reuse the shared overlap repair: proportional splitting can produce two
    # cues that meet exactly, and rounding can push one past the next start.
    return normalize_subtitle_items(cues)


def _sources_usable(sources: list[str] | None, inputs: list[str]) -> bool:
    """Whether a backend's corrected-source list can be trusted.

    A wrong-length list is worse than none: it would pair corrected English from
    one sentence with Chinese from another. Rejecting it silently is correct here
    because ``sources`` is optional -- falling back to the original English
    degrades the display, while a mispaired list produces wrong subtitles.
    """
    if sources is None:
        return False
    if len(sources) != len(inputs):
        _logger.warning(
            "translation backend returned %d corrected sources for %d sentences; "
            "ignoring them rather than risking a mispairing",
            len(sources),
            len(inputs),
        )
        return False
    return True


def _has_cjk(text: str) -> bool:
    """CJK test over a joined string, without allocating a cue list."""
    return any("\u4e00" <= character <= "\u9fff" for character in text)


def _play_resolution(subtitles: SubtitleSet) -> tuple[int, int]:
    """Resolution for the ASS header, from the measurement TRANSCRIBE carried.

    ``ASS`` coordinates are absolute pixels scaled to ``PlayResX/PlayResY``, so a
    header that disagrees with the video scales every subtitle wrong. v0.1 passed
    1920x1080 unconditionally, which is why a vertical video got horizontal
    margins.

    Falls back to 16:9 only when the geometry was genuinely not measured, and 16:9
    is the safer guess: a horizontal layout on a vertical video still renders
    readably, while the reverse pushes text off-screen. The style scaling in
    :func:`porter.subtitles.ass.compute_adaptive_subtitle_style` then works from
    ``play_res_y``, so the aspect ratio is what actually drives the layout.
    """
    width = subtitles.video_width or _DEFAULT_WIDTH
    height = subtitles.video_height or _DEFAULT_HEIGHT
    if width <= 0 or height <= 0:
        return _DEFAULT_WIDTH, _DEFAULT_HEIGHT
    return width, height
