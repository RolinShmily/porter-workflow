# Porter Workflow

<p align="center">
  <b>English</b> | <a href="README_zh.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/RolinShmily/porter-workflow/actions/workflows/test.yml"><img alt="CI" src="https://github.com/RolinShmily/porter-workflow/actions/workflows/test.yml/badge.svg"></a>
  <a href="https://pypi.org/project/porter-workflow/"><img alt="PyPI" src="https://img.shields.io/pypi/v/porter-workflow"></a>
  <img alt="Python 3.10 - 3.13" src="https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue">
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

> **Status: `v0.2.0` is the current development line on `main`.**
> The v0.2 rewrite — one engine, three frontends — has landed. The v0.1
> implementation is preserved in git history at commit `b5fd577`.

---

## Installation

Requires **Python ≥ 3.10**.

```bash
# CLI, minimal install (pure-Python; translation works without a key, but
# transcription does NOT -- see "Transcription needs a key" below)
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

### Transcription needs a key (or the VideoCaptioner CLI)

**There is no working key-free speech-to-text path.** This is measured, not
theoretical — as of 2026-09-22, on a real 10-minute video:

| ASR backend | Status |
| --- | --- |
| Whisper API | Needs `OPENAI_API_KEY` (or a compatible endpoint) |
| VideoCaptioner CLI | Needs the `videocaptioner` package installed separately |
| Bcut | Host answers, but returns **zero utterances** |
| Google Web (`[stt]`) | Returns the empty result `{"result":[]}` for **every** request |

Both key-free endpoints are reverse-engineered and neither transcribes any
more. Google Web's response is also malformed at the HTTP level, so the read
fails outright rather than merely returning nothing. Bcut's failure is the more
ambiguous of the two -- the host answers, so it could be a quota or a changed
field rather than a dead endpoint -- which is why it keeps an "unverified" label
in the source.

So a bare `uvx porter-workflow "<URL>"` will download and standardise the video
and then fail at the transcription phase with
`every speech-to-text backend failed`. To get subtitles, do one of:

```bash
# Option A: an LLM key (also improves translation quality a lot)
export OPENAI_API_KEY=sk-...
porter "<URL>" --burn skip

# Option B: install VideoCaptioner and use its engines
pip install videocaptioner
porter "<URL>" --asr-engine bijian --burn skip
```

**Translation is unaffected.** Bing, Google and MyMemory all still work without
a key; each was probed against its live endpoint and returned real Chinese. Only
transcription needs credentials.

`porter doctor` reports which route a job will actually take before you start it.

### Optional extras

| Extra | Adds | Enables |
| --- | --- | --- |
| `[llm]` | `openai`, `json-repair` | LLM translation + Whisper API ASR |
| `[stt]` | `SpeechRecognition` | Google Web STT — **measured non-functional**, see below |
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
