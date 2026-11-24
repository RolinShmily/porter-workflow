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
# Core workflow engine (download, local offline Whisper, LLM/free translation, hardsub)
pip install porter-workflow

# Interactive Gradio WebUI workbench
uv tool install "porter-workflow[web]"
porter                  # opens http://127.0.0.1:7860

# Or run the WebUI directly without permanent install
uvx --from "porter-workflow[web]" porter
```

All localization capabilities — including local offline Whisper (`faster-whisper`)
and optional paid LLM/Whisper API adapters — ship in the base `porter-workflow`
package. Only `gradio` is isolated in the `[web]` extra so `import porter` stays
pure for scripts, services and agent skills.

## Usage

### WebUI Workbench (`porter`)

```bash
porter                  # http://127.0.0.1:7860
porter --port 8080      # custom port
porter --version        # print installed version
```

The workbench lets you paste a URL or local path, choose task scope, target
video quality (`1080p` default, degrading gracefully when unavailable), burn
mode and capabilities, inspect the resolved `WorkflowPlan` JSON, monitor live
progress, preview hard-subbed MP4s and download `raw.zip` / `cooked.zip`.

### Python Core API (`import porter`)

```python
from porter import plan_source, run_doctor, run_plan

report = run_doctor()
plan = plan_source("https://www.youtube.com/watch?v=...", quality="1080p", burn="zh_only")
if plan.feasible:
    outcome = run_plan(plan)
    print(outcome.workflow.artifacts)
```

`plan_source()` probes the source, merges your effective `PorterConfig` defaults
(cookies, output directory, target video quality, audio denoise), and returns an
immutable, JSON-serialisable `WorkflowPlan`. `run_plan()` executes that exact
plan with shared `JobRegistry` tracking and cooperative cancellation.

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

## Configuration

Resolved in this order, highest priority first:

1. Explicit config path passed to `resolve(path)`
2. `$PORTER_CONFIG`
3. Environment variables (they override the selected file)
4. Nearest project config: `porter.json` → `porter.toml` → `config.json`
5. User config under `platformdirs` (`porter/config.json`)
6. Built-in defaults

| Section | Keys | Purpose |
| --- | --- | --- |
| top level | `output_dir`, `video_quality`, `cookies_file`, `cookies_browser` | Output location, target resolution (`1080p` default) and gated-media access |
| `llm` | `api_key`, `api_base`, `model` | Explicit `translate.llm` and `transcript.whisper-api` |
| `asr` | `engine`, `language`, `audio_denoise`, `whisper_local_model`, `whisper_local_device`, `whisper_local_compute_type`, `whisper_api_key`, `whisper_api_base`, `whisper_model` | Recognition defaults and preferences |
| `refine` | `enabled`, `model` | Optional LLM transcript cleanup |
| `ffmpeg` | `ffmpeg_path`, `ffprobe_path`, `video_codec`, `preset`, `crf`, `pixel_format`, `audio_codec`, `audio_bitrate`, `audio_sample_rate`, `wav_sample_rate`, `auto_tune` | Encoding and hardware tuning |
| `style` | `zh_font`, `en_font`, sizes, colors, `outline_*`, `shadow`, `margin_*`, `fade_*` | ASS subtitle appearance |

`asr.engine` is a *preference* for automatic selection, not a paid consent and
not a requirement to run ASR. You can edit configuration in the WebUI Settings
tab or via `porter.save_key(...)` / `porter.writable_config_path()`.

## Build and develop

Development needs **Python 3.11–3.13**, **uv**, **FFmpeg + libass**, and
**Deno** or **Node ≥ 20**.

```bash
uv sync --extra web --extra dev     # Install exactly what uv.lock pins

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
│   └── workflow/     plan model, resolver, probe, executor, facade
└── webui/            optional Gradio WebUI workbench ([web])
scripts/              developer / release helper scripts
docs/                 development, architecture, reference, design, images
tests/{unit,regression,integration}/
```

- [Documentation index](docs/README.md)
- [Architecture overview](docs/architecture/overview.md)
- [Development gate](docs/development/gate.md)
- [Contributing](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE). Third-party components and their licences are
listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
