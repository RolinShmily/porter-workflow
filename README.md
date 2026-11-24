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

**Porter Workflow** turns a video URL, a local video, an audio file or a subtitle
file into a localized release: a standardized asset bundle, bilingual and
Chinese-only subtitles, and hard-subbed MP4s. It is built agent-first: every
run is preceded by an executable plan that says exactly what will happen.

## How it works

```text
URL / local video / audio / subtitle
      │
      ▼
PROBE       identify the source, platform and subtitle tracks
ACQUIRE     download, copy the local file, or load the supplied subtitles
NORMALIZE   H.264/AAC master + 16 kHz mono audio (skipped for subtitles)
TRANSCRIPT  platform track, supplied subtitle, local Whisper, or Whisper API
TRANSLATE   one translation capability (free endpoints first)
WRITE       SRT + ASS for bilingual and target-only tracks
RENDER      bilingual / Chinese-only hard-subbed MP4s
```

`--task` stops after a meaningful result: `probe`, `audio`, `transcribe`,
`subtitle`, `release` (default), or `convert`. `--burn skip` plans the
subtitles without encoding video.

### Workflows

The resolver picks one workflow from the source, not a fallback chain:

| Workflow | When |
| --- | --- |
| `url.subtitled` | A URL whose platform lists a subtitle track |
| `url.asr` | A URL with no usable track — recognition runs |
| `local.subtitled` | A local video with a supplied subtitle file |
| `local.asr` | A local video or audio file — recognition runs |
| `subtitle.only` | A `.srt` / `.vtt` input; translate and rewrite, no video |
| `audio.asr` | An audio input; transcribe, translate, write |

### One capability per stage

Each stage has peer capabilities, and the plan names exactly one of them:

| Stage | Capabilities (free unless marked) |
| --- | --- |
| probe | `probe.ytdlp`, `probe.ffprobe`, `probe.subtitle` |
| acquire | `acquire.ytdlp-video`, `acquire.ytdlp-audio`, `acquire.ytdlp-subs`, `acquire.local` |
| normalize | `normalize.ffmpeg` |
| transcript | `transcript.platform-track`, `transcript.supplied`, `transcript.whisper-local`, `transcript.whisper-api` (paid, audio egress) |
| translate | `translate.bing`, `translate.google`, `translate.mymemory`, `translate.llm` (paid, text egress) |
| write | `write.subtitles` |
| render | `render.ffmpeg-libass` |

There is **no runtime fallback between capabilities**. If the chosen capability
fails, the run reports `alternatives` and `suggested_workflow`; replan with a
different choice. A capability named explicitly on the command line is the only
one planned, so an unavailable choice blocks the plan instead of being silently
replaced.

Paid capabilities are only selected when named explicitly or when `--allow-paid`
is passed. A key in the environment is not consent. `porter capabilities` marks
each entry `available` / `MISSING`, its cost, and whether audio or text leaves
the machine.

### Supported platforms

| Platform | URL examples | Platform subtitle track |
| --- | --- | --- |
| YouTube | `youtube.com`, `youtu.be` | Yes |
| Bilibili | `bilibili.com`, `b23.tv`, `bilibili.tv` | Yes (CC may require cookies) |
| TikTok | `tiktok.com`, `vm.tiktok.com` | Yes |
| X / Twitter | `x.com`, `twitter.com` | No — the ASR workflow is planned |
| Instagram | `instagram.com` | No — the ASR workflow is planned |
| Local file | any readable path | Only via `--subtitle-file` |

Downloads go through yt-dlp, so other sites it supports may work, but only these
platforms have explicit specs. A platform track is *requested, not guaranteed*:
rate limiting can make the fetch fail, and the job then reports the failure and
its alternatives instead of quietly switching engines.

## Install

Requires **Python 3.11–3.13**.

System dependencies (not Python packages, must be on `PATH`):

| Dependency | Needed for | Notes |
| --- | --- | --- |
| **FFmpeg + ffprobe** | Everything | Must be built **with `libass`**. Verify: `ffmpeg -filters \| grep subtitles` |
| **Deno** (recommended) | YouTube downloads | yt-dlp ≥ 2025.11.12 needs an external JS runtime; Node ≥ 20 also works |

```bash
# Core: download, platform subtitles, free translation, hardsub
uvx --from porter-workflow porter "<URL>"

# Add local, key-free, offline speech recognition
uvx --from "porter-workflow[asr-local]" porter "<URL>"

# Everything, including the MCP server and the WebUI
uvx --from "porter-workflow[all]" porter "<URL>"

# Check the environment before the first job
uvx --from porter-workflow porter doctor
```

| Extra | Adds | Enables |
| --- | --- | --- |
| `[asr-local]` | `faster-whisper` | `transcript.whisper-local` — key-free and offline |
| `[llm]` | `openai`, `json-repair` | `translate.llm` + `transcript.whisper-api` |
| `[images]` | `pillow` | Cover image handling |
| `[mcp]` | `fastmcp` | The `porter-mcp` server |
| `[web]` | `gradio` | The `porter-web` workbench |
| `[all]` | all of the above | — |

The core install can localize a URL with a platform subtitle track, translate it
with a key-free endpoint, and burn it. Extras add capabilities; they do not
change the workflow model. `porter doctor` and `porter capabilities` report what
the current environment can actually run.

## Usage

```bash
porter capabilities                 # what can this installation execute?
porter probe "<URL>"                # source facts, no plan, no download
porter plan "<URL>"                 # resolved workflow, capabilities, blockers
porter plan "<URL>" --json > plan.json
porter run --plan plan.json         # execute exactly that plan
porter run "<URL>"                  # resolve and execute in one step
porter run "<URL>" -o ./output      # choose the output directory
porter run "<URL>" --task subtitle  # subtitles only, no video encode
porter doctor                       # diagnose the environment
porter config list                  # show resolved config (secrets masked)
```

`porter plan` is authoritative: it probes the source, probes the machine, and
reports which single capability would run each stage, its cost and egress, and
any blocker with a `fix`. Plans are reproducible, so `plan --json` followed by
`run --plan` is the recommended contract for agents and scripts.

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
├── cooked/
│   ├── subtitle_bilingual.{srt,ass}
│   ├── subtitle_zh.{srt,ass}
│   ├── video_bilingual.mp4
│   └── video_zh.mp4
└── .tmp/                         scratch, safe to delete between runs
```

### Frontends

| Frontend | Command | For |
| --- | --- | --- |
| CLI | `porter` | Humans, scripts, and the canonical plan contract |
| MCP | `uvx --from "porter-workflow[mcp]" porter-mcp` | Agents over stdio JSON-RPC |
| WebUI | `uvx --from "porter-workflow[web]" porter-web` | Humans using the plan workbench and HITL |
| Agent Skill | `npx skills add RolinShmily/porter-workflow` | Agents following the SOP |

All four use the same `WorkflowPlan` / `WorkflowResult` model. MCP resolves a
plan with `porter_plan` and starts it with `porter_job_start(plan=...)`; the
WebUI displays the plan JSON and executes the same plan. None of them has a
second pipeline or a runtime fallback chain. Setup details:

- [`skills/porter-skill/SKILL.md`](skills/porter-skill/SKILL.md) — the SOP
- [`skills/porter-skill/references/PLAN.md`](skills/porter-skill/references/PLAN.md) — plan/result fields and issue codes
- [`skills/porter-skill/references/MCP.md`](skills/porter-skill/references/MCP.md) — MCP tools and call order
- [`skills/porter-skill/references/WEBUI.md`](skills/porter-skill/references/WEBUI.md) — WebUI workbench

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
| `llm` | `api_key`, `api_base`, `model` | Explicit `translate.llm` and `transcript.whisper-api` |
| `asr` | `engine`, `language`, `audio_denoise`, `whisper_local_model`, `whisper_local_device`, `whisper_local_compute_type`, `whisper_api_key`, `whisper_api_base`, `whisper_model` | Recognition defaults and preferences |
| `refine` | `enabled`, `model` | Optional LLM transcript cleanup |
| `ffmpeg` | `ffmpeg_path`, `ffprobe_path`, `video_codec`, `preset`, `crf`, `pixel_format`, `audio_codec`, `audio_bitrate`, `audio_sample_rate`, `wav_sample_rate`, `auto_tune` | Encoding and hardware tuning |
| `style` | `zh_font`, `en_font`, sizes, colors, `outline_*`, `shadow`, `margin_*`, `fade_*` | ASS subtitle appearance |

`asr.engine` is a *preference* for automatic selection, not a paid consent and
not a requirement to run ASR. A workflow only runs recognition when the plan
says so.

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
`requests`, `pydantic` and `platformdirs`; every other engine capability is an
optional extra.

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
│   └── workflow/     plan model, resolver, probe, executor
├── cli/              CLI frontend
├── mcp/              MCP frontend
└── webui/            Gradio WebUI frontend
skills/porter-skill/  Agent Skill assets (SKILL.md, Python scripts, references)
scripts/              developer / release helper scripts
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
