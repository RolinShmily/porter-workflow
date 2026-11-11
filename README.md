<h1 align="center">Porter Workflow</h1>

<p align="center">
  <b>English</b> | <a href="README_zh.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/RolinShmily/porter-workflow/actions/workflows/test.yml"><img alt="CI" src="https://github.com/RolinShmily/porter-workflow/actions/workflows/test.yml/badge.svg"></a>
  <a href="https://pypi.org/project/porter-workflow/"><img alt="PyPI" src="https://img.shields.io/pypi/v/porter-workflow"></a>
  <img alt="Python 3.11 - 3.13" src="https://img.shields.io/badge/python-3.11%20%E2%80%93%203.13-blue">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/RolinShmily/porter-workflow/main/docs/images/preview.webp" alt="Porter Workflow: video input, media preparation, subtitle extraction, translation, and subtitled video delivery" width="1200">
</p>

**Porter Workflow** takes a video URL or a local file and produces a localized
release: a standardized asset bundle, bilingual and Chinese-only subtitles, and
hard-subbed MP4s.

## How it works

```text
URL / local file
      │
      ▼
PREPARE     download, standardize, fetch platform subtitles, extract audio
      │
      ▼
TRANSCRIBE  use the platform's subtitle track, or run speech recognition
      │
      ▼
TRANSLATE   produce target-language subtitles (default: zh-Hans)
      │
      ▼
BURN        render bilingual / Chinese-only hard-subbed MP4s
```

Completed phases are reused on a rerun. `--force` redoes them, `--only-phase`
stops after one, and `--burn skip` stops before encoding.

### Supported platforms

| Platform | URL examples | Platform subtitle track |
| --- | --- | --- |
| YouTube | `youtube.com`, `youtu.be` | Yes |
| Bilibili | `bilibili.com`, `b23.tv`, `bilibili.tv` | Yes (CC may require cookies) |
| TikTok | `tiktok.com`, `vm.tiktok.com` | Yes |
| X / Twitter | `x.com`, `twitter.com` | No — ASR runs |
| Instagram | `instagram.com` | No — ASR runs |
| Local file | any readable path | No — ASR or `--subtitle-file` |

Downloads go through yt-dlp, so other sites it supports may work, but only these
platforms have explicit specs. A native track is *requested*, not guaranteed:
rate limiting can make the fetch fail, and the ASR chain then takes over.

## Backends by phase

### PREPARE

- **URL** — yt-dlp download; **local file** — used in place.
- **FFmpeg** normalizes video to H.264/AAC and audio to 16 kHz mono.
- **Deno** (or Node ≥ 20) is recommended for YouTube's JS challenges.

### TRANSCRIBE

| Route | Requires | Notes |
| --- | --- | --- |
| Platform subtitles | — | Free and exact; preferred when the platform provides them |
| `whisper-local` | `[asr-local]` extra | Key-free and offline after the model downloads; tried first |
| `whisper-api` | `[llm]` extra + API key | Any OpenAI-compatible `/audio/transcriptions` endpoint |
| `videocaptioner` | External CLI, installed by you | GPL-3.0 and `python<3.13`, so never a declared dependency |
| `--subtitle-file FILE` | A local SRT | Explicit source track; skips platform tracks and ASR |

The first available backend wins. `--asr-engine NAME` moves a named backend to
the front but keeps the rest as fallbacks. A Chinese platform track skips the
translation phase entirely.

### TRANSLATE

| Backend | Requires | Notes |
| --- | --- | --- |
| `llm` | `[llm]` extra + API key | Best quality; can also repair transcription errors |
| `bing` | Nothing | Key-free endpoint |
| `google` | Nothing | Key-free endpoint |
| `mymemory` | Nothing | Key-free public API |
| `videocaptioner-llm` | External CLI | GPL-3.0, installed by you |
| `videocaptioner` | External CLI | GPL-3.0, installed by you |

The first backend that produces usable text wins. For a Chinese target, a
response without CJK characters is rejected and the next backend is tried.
`--translator NAME` promotes a backend to the front, like `--asr-engine`.
`--target-lang` defaults to `zh-Hans`.

### BURN

| Mode | Output |
| --- | --- |
| `dual` (default) | `video_bilingual.mp4` + `video_zh.mp4` |
| `zh_only` | `video_zh.mp4` |
| `bilingual_only` | `video_bilingual.mp4` |
| `skip` | Subtitles only, no video encoding |

Burning requires **FFmpeg with libass**. Fonts, sizes, colors, margins and fades
are configurable under the `style` section.

## Install

Requires **Python ≥ 3.11**.

System dependencies (not Python packages, must be on `PATH`):

| Dependency | Needed for | Notes |
| --- | --- | --- |
| **FFmpeg + ffprobe** | Everything | Must be built **with `libass`**. Verify: `ffmpeg -filters \| grep subtitles` |
| **Deno** (recommended) | YouTube downloads | yt-dlp ≥ 2025.11.12 needs an external JS runtime; Node ≥ 20 also works |

```bash
# Recommended: local, key-free, offline transcription
uvx --from "porter-workflow[asr-local]" porter "<URL>"

# Every optional backend, including WebUI support
uvx --from "porter-workflow[all]" porter "<URL>"

# WebUI workbench
uvx --from "porter-workflow[web]" porter-web

# Persistent install
pipx install "porter-workflow[all]"

# Check the environment
porter doctor
```

| Extra | Adds | Enables |
| --- | --- | --- |
| `[asr-local]` | `faster-whisper` | Local Whisper — key-free and offline |
| `[llm]` | `openai`, `json-repair` | LLM translation + Whisper API ASR |
| `[images]` | `pillow` | Cover image handling |
| `[mcp]` | `fastmcp` | The `porter-mcp` server |
| `[web]` | `gradio` | The `porter-web` workbench |
| `[all]` | all of the above | — |

A minimal install without `[asr-local]` or `[llm]` can download and translate,
but cannot transcribe unless a platform subtitle track exists or the external
VideoCaptioner CLI is installed. `porter doctor` reports the route a job will
actually take.

## Usage

```bash
porter inspect "<URL>"              # Inspect a link without downloading
porter plan "<URL>"                 # Show the exact route and expected failures
porter "<URL>"                      # Full pipeline
porter "<URL>" -o ./porter_output   # Choose the output directory
porter "<URL>" --burn skip          # Subtitles only
porter doctor                       # Diagnose the environment
porter config list                  # Show resolved config (secrets masked)
```

### Output layout

```text
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

### Frontends

| Frontend | Command | For |
| --- | --- | --- |
| CLI | `porter` | Humans and shell scripts |
| MCP | `uvx --from "porter-workflow[mcp]" porter-mcp` | AI agents over stdio JSON-RPC |
| WebUI | `uvx --from "porter-workflow[web]" porter-web` | Humans and agents using the HITL workbench |
| Agent Skill | `npx skills add RolinShmily/porter-workflow` | Agents using the Agent Skills spec |

All four call the same engine. MCP setup details are in
[`skills/porter-skill/references/MCP.md`](skills/porter-skill/references/MCP.md);
restricted Windows hosts are covered in
[`skills/porter-skill/references/HOSTS.md`](skills/porter-skill/references/HOSTS.md).

## Configuration

Resolved in this order, highest priority first:

1. `--config <path>`
2. `$PORTER_CONFIG`
3. Environment variables (they override the selected file)
4. Nearest project config: `porter.json` → `porter.toml` → `config.json`
5. User config under `platformdirs` (only if there is no project config)
6. Built-in defaults

| Section | Keys | Purpose |
| --- | --- | --- |
| top level | `output_dir`, `cookies_file`, `cookies_browser` | Output location and gated-media access |
| `llm` | `api_key`, `api_base`, `model` | LLM translation, refinement and Whisper API fallback |
| `asr` | `engine`, `language`, `audio_denoise`, `whisper_local_model`, `whisper_local_device`, `whisper_local_compute_type`, `whisper_api_key`, `whisper_api_base`, `whisper_model` | Speech recognition |
| `refine` | `enabled`, `model` | LLM transcript cleanup before translation |
| `ffmpeg` | `ffmpeg_path`, `ffprobe_path`, `video_codec`, `preset`, `crf`, `pixel_format`, `audio_codec`, `audio_bitrate`, `audio_sample_rate`, `wav_sample_rate`, `auto_tune` | Encoding and hardware tuning |
| `style` | `zh_font`, `en_font`, sizes, colors, `outline_*`, `shadow`, `margin_*`, `fade_*` | ASS subtitle appearance |

```bash
porter config list                     # Effective values + source, secrets masked
porter config get llm                  # Show one section
porter config set llm.model=deepseek-chat
porter config set llm.api_key=sk-...   # Writes user-level config by default
```

The complete key table, defaults and examples are in
[`skills/porter-skill/references/CONFIG.md`](skills/porter-skill/references/CONFIG.md).

## Build and develop

Development needs **Python 3.11–3.13**, **uv**, **FFmpeg + libass**, and
**Deno** or **Node ≥ 20**. The runtime dependencies are `yt-dlp[default]`,
`requests`, `pydantic` and `platformdirs`; everything else is an optional extra.

```bash
uv sync --extra all --extra dev      # Install exactly what uv.lock pins

ruff check src tests scripts        # Lint; T20 enforces no print() in the engine
mypy src scripts                    # Strict type checking
lint-imports                        # Engine/frontend dependency contracts
pytest -m "not network"             # Offline suite
```

Repository layout:

```text
src/porter/
├── __init__.py       stable public API (lazy exports), __version__
├── core/             engine — all business logic
├── cli/              CLI frontend
├── mcp/              MCP frontend
└── webui/            Gradio WebUI frontend (HITL workbench)
skills/porter-skill/  Agent Skill assets (SKILL.md, scripts, references)
scripts/              developer / release helper scripts (launcher.py)
docs/                 development, architecture, reference, design, images
tests/{unit,regression,integration}/
```

- [Documentation index](docs/README.md)
- [Architecture overview](docs/architecture/overview.md)
- [Development gate](docs/development/gate.md)
- [CLI reference](docs/reference/cli.md)
- [Contributing](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE). Third-party components and their licences are
listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
