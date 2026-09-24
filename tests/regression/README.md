# Regression tests

Behaviour pins: the assertions the engine must not lose. They are the v0.1 test
suite carried across the v0.2 restructure, so that the rewrite could change
structure without changing results.

## The rule

**Only import paths and patch targets may change. Assertions may not.**

If an assertion has to change, the behaviour changed. That is a legitimate
outcome, but it must be called out explicitly **in the file that changed it**,
with a reason. Silent assertion edits are how a refactor loses its safety net.

## Getting the v0.1 originals

`b5fd577` is the last commit of the v0.1 line (`porter-skill`). Its tests live at
the repository root under `tests/`:

```bash
git show b5fd577:tests/test_subtitle.py
git show b5fd577:tests/test_bilibili_extractor.py
```

## Where the v0.1 tests went

| v0.1 path | v0.2 path | notes |
| --- | --- | --- |
| `tests/test_subtitle.py` | `tests/regression/test_subtitle_conversion.py` | ported |
| `tests/test_x_extractor.py` | `tests/regression/test_titles_port.py`, `test_inspector_port.py` | ported |
| `tests/test_tiktok_extractor.py` | `tests/regression/test_titles_port.py`, `test_url_cleaning_port.py` | ported |
| `tests/test_instagram_extractor.py` | `tests/regression/test_titles_port.py`, `test_url_cleaning_port.py` | ported |
| `tests/test_bilibili_extractor.py` | `tests/regression/test_subtitle_conversion.py`, `test_titles_port.py`, `test_url_cleaning_port.py` | ported |
| `tests/test_extractors.py` | split — see the table below | ported |
| `tests/test_inspector.py` | `tests/regression/test_inspector_port.py` | ported |
| `tests/test_synthesizer.py` | `tests/regression/test_synthesizer_port.py` | ported |
| `tests/test_config.py` | `tests/unit/test_config.py` | rewritten: new search order |
| `tests/test_env_check.py` | `tests/unit/test_doctor_probes.py` | rewritten: structured report |
| `tests/test_pipeline.py` | superseded — see below | not a translation |

`test_extractors.py` held eight tests that scattered across four destinations,
because v0.1 had one extractor-shaped module per platform and v0.2 has one shared
pipeline plus a spec:

| v0.1 test | where it went |
| --- | --- |
| `test_sanitize_filename` | `tests/unit/test_utils_text.py` |
| `test_youtube_extractor_can_handle` | `tests/unit/test_platforms.py` |
| `test_get_extractor_factory` | `tests/regression/test_extractor_contracts_port.py` |
| `test_youtube_extractor_subtitles_selection` | `test_subtitle_conversion.py` |
| `test_youtube_extractor_chinese_subtitles_selection` | `test_subtitle_conversion.py` |
| `test_convert_vtt_to_srt` | `test_subtitle_conversion.py` |
| `test_youtube_extractor_mock_run` | superseded by `tests/unit/test_fetch_pipeline.py` |
| `test_enhance_audio_for_asr` | superseded by `tests/unit/test_fetch_pipeline.py` |

## Declared divergences

Every one of these is a place where an assertion genuinely changed. Each is also
recorded in the test file that implements it, with the reasoning.

| # | v0.1 assertion | v0.2 | why |
| --- | --- | --- | --- |
| 1 | `identify_platform(...) == "generic"` | `is None` | There is no generic extractor; `get_extractor` raised immediately after. `"generic"` implied a fallback that did not exist. |
| 2 | `pytest.raises(ValueError, match="Unsupported URL platform")` | `UnsupportedPlatformError` | A well-formed URL with no handler is not a malformed argument. The new type carries `code`/`exit_code` and lists the supported platforms. |
| 3 | `get_video_dimensions(missing) == (1920, 1080)` | `dimensions(...) is None` | The v0.1 assertion **asserts a bug**: a fabricated resolution made the pipeline style vertical videos with horizontal margins. |
| 4 | `enhance_audio_for_asr(...) is True` | `enhance_for_asr(...) is not None` | Returns the output path instead of a boolean, so the caller does not have to reconstruct it. |
| 5 | `require_subtitles` shape for `burn_hardsub` | the burn options model | The burn call now takes a typed options object rather than a loose mapping. |
| 6 | CLI printed `PORTER-SKILL DOCTOR` to stdout; no-args exit code `1` | structured report on stderr; exit `2` | stdout is reserved for results; `2` is the conventional "misuse" code and is what the `run`/`inspect`/`config` commands already use. |
| 7 | `reconstruct_sentences_from_fragments` merged two unpunctuated short sentences | splits them (sixth condition) | **Output change, not a signature change.** See below. |

Divergence 3 is the important one among 1-6: it is the case where the rule
("assertions may not change") had to yield, because obeying it would have
preserved a defect.

### Divergence 7: a deliberate output change in sentence splitting

This one is different in kind from the others -- it is not a changed assertion
about a signature or an error type, it is a changed **result**. It is recorded
here because the porting rule was "assertions may not change", and this breaks
that rule on purpose.

v0.1 split a sentence on five conditions, and all five key off punctuation, a
pause or length. **ASR output has no punctuation at all**, so in practice they
rarely fire and two short sentences merged:

```
(0-700)    "what is going on here"
(800-1500) "I think that's right"
-> "What is going on here I think that's right?"
```

Measured on a real run, the merged text was mistranslated (the Chinese came back
with scrambled word order) *and* mis-punctuated: the punctuation heuristic sees a
leading "What" and marks the statement as a question.

v0.2 adds a condition that inspects the words -- the next fragment opens with a
subject pronoun or question word, and the text so far does not end on something
that leaves the clause open. Four guards reject specific real failures
(determiners/prepositions/auxiliaries, dangling interrogatives, reported speech,
and fragments too short to be a sentence).

#### The condition is enforced in two places, and that is the whole story

`starts_new_sentence` lives in `phrasing.py` and is called by **both**
`merge_short_fragments` and `reconstruct_sentences_from_fragments`.

The first version of this fix guarded only the reconstruction stage. Its unit
tests passed, its differential harness passed, and the live pipeline **still
produced the merged sentence** -- because `translate/chain.py` runs
`merge_short_fragments` first, with an 800ms gap window, which had already joined
the two fragments before reconstruction could see a boundary. The 800ms window is
wider than the 600ms one in reconstruction, so the boundary never survived.

Two lessons are recorded because both cost real time:

1. **A differential harness must model the real call order.** The first harness
   compared the two stages in isolation and reported success while the product
   stayed broken.
2. **End-to-end execution is the only proof.** The fix was declared done once
   already and was not; re-running the real pipeline is what showed it.

Final verification, real Google backend, 6 fragmented cues in and 4 subtitle
lines out:

```
So the output of the encoder is wrong,  -> 所以编码器的输出是错误的，
and we need to fix it before we ship.   -> 我们需要在发货前修复它
What is going on here?                  -> 这是怎么回事？
I think that's right.                   -> 我认为这是对的。
```

Before the fix the last two lines were one merged cue, translated as
`我认为这是正确的，这是怎么回事？` -- clauses reversed.

Verified by a differential harness over a corpus of realistic ASR sequences,
modelling **both** stages in order and printing every case whose grouping
changed, so each change could be judged individually rather than counted. All
target cases split; all "must not split" cases are protected; every remaining
change was judged correct.

Tests: `tests/unit/test_sentence_reconstruction.py` (48 tests), including a
`TestTheTwoStagePipeline` class that exercises the real order -- the class that
would have caught the incomplete first fix. The v0.1 ported tests in
`test_subtitle_conversion.py` are unaffected, because the new condition only
fires where v0.1 merged.

## Superseded rather than ported

**`tests/test_pipeline.py`** (4 tests). Its coverage now exists, but not as a
translation of the file. Each test needs a different answer:

**`test_pipeline_orchestration`** — mocks the extractor and the subtitle
generator, runs the real burn, and asserts the two release videos exist. This is
exactly what `tests/integration/test_burn_pipeline.py` and
`tests/unit/test_local_video.py` do, and they do it more strictly: they drive the
whole `Pipeline.run`, use a real local file as well as a synthetic one, and verify
the subtitles are **visible in the pixels** instead of only that a file exists. A
burn that silently did nothing still produces a valid, playable video of the right
length, so the v0.1 assertion passes on that failure.

**`test_cli_doctor`**, **`test_cli_no_args`**, **`test_cli_inspect`** — these
assert v0.1's CLI *text and exit codes*, which v0.2 changed deliberately:

| v0.1 asserted | v0.2 does | Why |
| --- | --- | --- |
| `"PORTER-SKILL DOCTOR"` on stdout | a different banner, on **stderr** | stdout is reserved for `--json`, so progress cannot corrupt the document |
| no args -> `"Error: Please provide a video URL or run with --doctor"`, exit 1 | usage on **stdout**, exit **2** | it is argparse misuse, and 2 is the misuse code everywhere else in the CLI |
| `porter <url> --inspect` | `porter inspect <url>` | `inspect` is a subcommand, like `run` / `doctor` / `config` |

Porting those three would mean asserting behaviour v0.2 removed. Their
replacements are in `tests/unit/test_cli.py`, which tests the *new* contract.

## One v0.1 defect fixed while porting

`normalize_subtitle_items` repaired overlaps with `for i in range(len - 1)`,
which **never examines the final cue**. A degenerate zero-length last cue
survived and rendered a single frame of unreadable text. v0.2 adds the missing
final check, and `TestOverlapRepair::test_a_zero_length_last_cue_is_widened`
pins it.
