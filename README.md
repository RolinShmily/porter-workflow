# Porter Workflow

<p align="center">
  <b>English</b> | <a href="README_zh.md">简体中文</a>
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

> **Status: `v0.2.0` is under active refactoring.**
> The design and the step-by-step migration plan live in
> [`docs/REFACTOR_PLAN.md`](docs/REFACTOR_PLAN.md). The `v0.1.x` implementation
> remains available on the `main` branch.

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

See [`docs/CONFIG.md`](docs/CONFIG.md).

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
blocking call, because encoding a 1080p video can take tens of minutes. See
[`docs/MCP.md`](docs/MCP.md).

---

## Development

```bash
uv venv && uv pip install -e ".[dev,all]"

ruff check src          # includes T20: the engine must not print()
mypy src
lint-imports            # enforces the engine/frontend boundary
pytest
```

Repository layout:

```
src/porter/        engine (library) — all business logic lives here
src/porter_cli/    CLI frontend
src/porter_mcp/    MCP frontend
skills/porter-skill/   Agent Skill assets (SKILL.md, scripts, references)
tests/{unit,regression,integration}/
docs/
```

---

## License

MIT. See [LICENSE](LICENSE).

Third-party acknowledgements, including VideoCaptioner (GPL-3.0) which inspired
parts of the ASR orchestration design, are listed in the acknowledgements
section of the previous release notes and will be carried over in the final
`v0.2.0` README.
