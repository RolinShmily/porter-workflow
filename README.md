# Porter Workflow

<p align="center">
  <b>English</b> | <a href="README_zh.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/RolinShmily/porter-workflow/actions/workflows/test.yml"><img alt="CI" src="https://github.com/RolinShmily/porter-workflow/actions/workflows/test.yml/badge.svg"></a>
  <a href="https://pypi.org/project/porter-workflow/"><img alt="PyPI" src="https://img.shields.io/pypi/v/porter-workflow"></a>
  <img alt="Python 3.11 - 3.13" src="https://img.shields.io/badge/python-3.11%20%E2%80%93%203.13-blue">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

**Porter Workflow** is an automated video localization pipeline: give it a video
URL, get back a standardised asset bundle plus hard-subbed release videos with
bilingual and Chinese-only subtitles.

It ships as **one engine with three frontends**:

| Frontend | Entry point | For |
| --- | --- | --- |
| `porter` (CLI) | `porter "<URL>"` | Humans and shell scripts |
| `porter-mcp` | `uvx --from "porter-workflow[mcp]" porter-mcp` | AI agents via Model Context Protocol |
| `porter-skill` | `npx skills add RolinShmily/porter-workflow` | Agents that use the Agent Skills spec |

All three call the same `porter` library. The engine contains no argument
parsing and never writes to stdout — a requirement, because in an MCP stdio
server stdout *is* the JSON-RPC channel.

> **Status: `v0.2.x` is the current development line on `main`.**
> The v0.2 rewrite — one engine, three frontends — has landed. The v0.1
> implementation is preserved in git history at commit `b5fd577`.

---

## Installation

Requires **Python ≥ 3.11**.

```bash
# CLI, minimal install (pure-Python; translation works without a key, but
# transcription needs the [asr-local] extra or an API key -- see below)
uvx porter-workflow "<URL>"

# CLI with every optional backend
uvx --from "porter-workflow[all]" porter "<URL>"

# Persistent install
pipx install "porter-workflow[all]"
```

### System dependencies

These are **not** Python packages and must be on `PATH`:

| Dependency | Needed for | Notes |
| --- | --- | --- |
| **FFmpeg + ffprobe** | Everything | Must be built **with `libass`** for hardsubbing. Verify: `ffmpeg -filters \| grep subtitles` |
| **Deno** (recommended) | YouTube downloads | yt-dlp ≥ 2025.11.12 needs an external JS runtime to solve YouTube's JS challenges. Node ≥ 20 also works. |

Check everything at once:

```bash
porter doctor
```

### Transcription is key-free and offline, or an API key

**Local Whisper runs on your own machine: no key, no network after the first
run.** Measured 2026-09-24 on real speech -- 18 s of audio became 3 cues in
7.2 s on CPU with the default `small` model:

| ASR backend | Needs | Status |
| --- | --- | --- |
| **`whisper-local`** | the `[asr-local]` extra | **Works.** Key-free; offline once the model is cached |
| Whisper API | `OPENAI_API_KEY` (or a compatible endpoint) | Works |
| VideoCaptioner CLI | the `videocaptioner` package, installed separately | Works |
| Bcut | nothing | Host answers, but returns **zero utterances** |
| Google Web (`[stt]`) | nothing | Returns the empty result `{"result":[]}` for **every** request |

`whisper-local` is tried first, so it is what a run uses whenever it is
installed. It sits behind an extra because `faster-whisper` and `ctranslate2`
are heavy, and CTranslate2-backed rather than PyTorch:

```bash
# Key-free and offline. This is the recommended install.
uvx --from "porter-workflow[asr-local]" porter "<URL>"

# Or everything at once -- [all] includes [asr-local]
uvx --from "porter-workflow[all]" porter "<URL>"
```

The first run downloads the model from Hugging Face (464 MB for the default
`small`); after that it is fully offline. Device and precision are chosen for
you -- CUDA if it loads, CPU otherwise -- and can be pinned:

```bash
porter config set asr.whisper_local_model=medium
porter config set asr.whisper_local_device=cuda
porter config set asr.whisper_local_compute_type=float16
```

A **bare** `uvx porter-workflow "<URL>"` still cannot transcribe, because no ASR
backend is in a minimal install: it downloads and standardises the video and
then fails with `every speech-to-text backend failed`. The two key-free
*endpoints* are the ones that are dead -- both are reverse-engineered, and
Google Web's response is malformed at the HTTP level, so the read fails outright
rather than merely returning nothing. Bcut's failure is the more ambiguous of
the two -- the host answers, so it could be a quota or a changed field rather
than a dead endpoint -- which is why it keeps an "unverified" label in the
source.

So to get subtitles, do one of:

```bash
# Option A (recommended): local, key-free, offline
uvx --from "porter-workflow[asr-local]" porter "<URL>" --burn skip

# Option B: an LLM key (also improves translation quality a lot)
export OPENAI_API_KEY=sk-...
porter "<URL>" --burn skip

# Option C: install VideoCaptioner and use its engines
pip install videocaptioner
porter "<URL>" --asr-engine bijian --burn skip
```

**Translation is unaffected.** Bing, Google and MyMemory all still work without
a key; each was probed against its live endpoint and returned real Chinese. Only
transcription ever needed credentials.

`porter doctor` reports which route a job will actually take before you start it.

### Optional extras

| Extra | Adds | Enables |
| --- | --- | --- |
| `[asr-local]` | `faster-whisper` | **Local Whisper ASR — key-free and offline**, see above |
| `[llm]` | `openai`, `json-repair` | LLM translation + Whisper API ASR |
| `[stt]` | `SpeechRecognition` | Google Web STT — **measured non-functional**, see above |
| `[images]` | `pillow` | Cover image handling |
| `[mcp]` | `fastmcp` | The `porter-mcp` server |
| `[all]` | all of the above | — |

`videocaptioner` is **deliberately not a dependency.** It is GPL-3.0 (this
project is MIT) and pins `python<3.13`. Porter detects it at runtime and, if
present, uses it as an extra ASR/translation backend across a process boundary.
Install it yourself if you want those engines:

```bash
pip install videocaptioner
```

---

## Usage

```bash
# Inspect a link without downloading anything
porter inspect "<URL>"

# Full pipeline: download → transcribe → translate → burn
porter "<URL>" -o ./porter_output

# Subtitles only, no video encoding
porter "<URL>" --burn skip

# Diagnose the environment
porter doctor

# Show resolved configuration (secrets masked)
porter config list
```

### Output layout

```
<output_dir>/<video_id>_<safe_title>/
├── raw/
│   ├── video.mp4                 H.264 + AAC + faststart master
│   ├── audio.wav                 16 kHz mono reference track
│   ├── audio_enhanced.wav        denoised track used only for ASR
│   ├── transcript.json/.txt      reconstructed sentence script book
│   ├── subtitle.srt              platform-provided source subtitles, if any
│   ├── cover.jpg
│   └── metadata.json
└── cooked/
    ├── subtitle_bilingual.{srt,ass}
    ├── subtitle_zh.{srt,ass}
    ├── video_bilingual.mp4
    └── video_zh.mp4
```

### Configuration

Resolved in this order, highest priority first:

1. `--config <path>`
2. `$PORTER_CONFIG`
3. Environment variables (`OPENAI_API_KEY`, `PORTER_ASR_ENGINE`, …)
4. Project-level `./porter.json`
5. User-level config directory (`platformdirs`)
6. Built-in defaults

---

## MCP server

```json
{
  "mcpServers": {
    "porter": {
      "command": "uvx",
      "args": ["--from", "porter-workflow[mcp]", "porter-mcp"]
    }
  }
}
```

Long jobs are exposed as a start/status/result/cancel API rather than one
blocking call, because encoding a 1080p video can take tens of minutes.

---

## Development

```bash
uv sync --extra all --extra dev      # installs exactly what uv.lock pins

ruff check src          # includes T20: the engine must not print()
mypy src
lint-imports            # enforces the engine/frontend boundary
pytest
```

[`CONTRIBUTING.md`](CONTRIBUTING.md) documents the full gate, the architecture
rules those tools enforce, and the licence rules for new dependencies.

Repository layout:

```
src/porter/        engine (library) — all business logic lives here
src/porter_cli/    CLI frontend
src/porter_mcp/    MCP frontend
skills/porter-skill/   Agent Skill assets (SKILL.md, scripts, references)
tests/{unit,regression,integration}/
```

---

## License

MIT. See [LICENSE](LICENSE).

---

## Acknowledgements

Porter is built on other people's work, and the debt is worth stating plainly:

* **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — downloading from five
  platforms is a solved problem rather than five scrapers, because of this.
* **[VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner)** (GPL-3.0) —
  its sentence segmentation, alignment and subtitle-processing design shaped the
  approach in `porter.subtitles` and the ASR chain. Ideas only: no code was
  copied, and it is never imported — only run as an external process, which is
  why porter can stay MIT.
* **[FFmpeg](https://ffmpeg.org/) and
  [libass](https://github.com/libass/libass)** — the entire media layer:
  standardisation, denoising and professional ASS rendering.

[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) records every component,
its licence, and whether porter depends on it, optionally installs it, or only
spawns it as a subprocess.

---

## Contributing

Contributions are welcome. [`CONTRIBUTING.md`](CONTRIBUTING.md) covers the
development setup and the gate every change must pass; [`SECURITY.md`](SECURITY.md)
explains how to report a vulnerability privately; release history lives in
[`CHANGELOG.md`](CHANGELOG.md).

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
