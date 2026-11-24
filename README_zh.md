<h1 align="center">Porter Workflow（流媒体搬运工）</h1>

<p align="center">
  <a href="README.md">English</a> | <b>简体中文</b>
</p>

<p align="center">
  <a href="https://github.com/RolinShmily/porter-workflow/actions/workflows/test.yml"><img alt="CI" src="https://github.com/RolinShmily/porter-workflow/actions/workflows/test.yml/badge.svg"></a>
  <a href="https://pypi.org/project/porter-workflow/"><img alt="PyPI" src="https://img.shields.io/pypi/v/porter-workflow"></a>
  <img alt="Python 3.11 - 3.13" src="https://img.shields.io/badge/python-3.11%20%E2%80%93%203.13-blue">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/RolinShmily/porter-workflow/main/docs/images/preview.webp" alt="Porter Workflow 视频本地化流程：输入视频、准备素材、获取字幕、翻译排版、交付成片" width="1200">
</p>

**Porter Workflow** 接收视频链接、本地视频、音频文件或字幕文件，产出一套本地化
成品：标准化素材、双语与纯中文字幕，以及烧好硬字幕的 MP4。它面向智能体优先设计：
每次运行前都有一份可执行计划，明确说明将要发生什么。

## 整体流程

```text
链接 / 本地视频 / 音频 / 字幕
      │
      ▼
PROBE       识别源类型、平台与字幕轨
ACQUIRE     下载、复制本地文件，或载入提供的字幕
NORMALIZE   生成 H.264/AAC 母版与 16 kHz 单声道音轨（纯字幕跳过）
TRANSCRIPT  平台字幕轨、提供字幕、本地 Whisper 或 Whisper API
TRANSLATE   一个翻译能力（免费端点优先）
WRITE       生成双语与目标语言单语的 SRT + ASS
RENDER      输出双语 / 纯中文硬字幕 MP4
```

`--task` 在某个有意义的结果处停止：`probe`、`audio`、`transcribe`、`subtitle`、
`release`（默认）或 `convert`。`--burn skip` 只规划字幕、不编码视频。

### 工作流

解析器根据源选择一个工作流，而不是一条回退链：

| 工作流 | 适用场景 |
| --- | --- |
| `url.subtitled` | 平台列出了字幕轨的链接 |
| `url.asr` | 没有可用字幕轨的链接——规划语音识别 |
| `local.subtitled` | 本地视频 + 提供的字幕文件 |
| `local.asr` | 本地视频或音频——规划语音识别 |
| `subtitle.only` | `.srt` / `.vtt` 输入；只翻译与重写，不处理视频 |
| `audio.asr` | 音频输入；转录、翻译、写字幕 |

### 每个阶段只有一个能力

各阶段的能力是**平级**的，计划只命名其中一个：

| 阶段 | 能力（未标注即为免费） |
| --- | --- |
| probe | `probe.ytdlp`、`probe.ffprobe`、`probe.subtitle` |
| acquire | `acquire.ytdlp-video`、`acquire.ytdlp-audio`、`acquire.ytdlp-subs`、`acquire.local` |
| normalize | `normalize.ffmpeg` |
| transcript | `transcript.platform-track`、`transcript.supplied`、`transcript.whisper-local`、`transcript.whisper-api`（付费，音频出口） |
| translate | `translate.bing`、`translate.google`、`translate.mymemory`、`translate.llm`（付费，文本出口） |
| write | `write.subtitles` |
| render | `render.ffmpeg-libass` |

能力之间**没有运行期回退**。选定的能力失败时，结果会给出 `alternatives` 与
`suggested_workflow`；换一个选择重新 `plan`。命令行显式命名的能力是计划里唯一
的能力，不可用就阻塞计划，而不是被悄悄替换。

付费能力只有在显式命名或传入 `--allow-paid` 时才会被选中；环境里有 Key 不等于
同意付费。`porter capabilities` 会标记每个能力是 `available` 还是 `MISSING`、
成本，以及音频或文本是否会离开本机。

### 支持的平台

| 平台 | 链接示例 | 平台字幕轨 |
| --- | --- | --- |
| YouTube | `youtube.com`、`youtu.be` | 有 |
| Bilibili | `bilibili.com`、`b23.tv`、`bilibili.tv` | 有（CC 可能需要 cookies） |
| TikTok | `tiktok.com`、`vm.tiktok.com` | 有 |
| X / Twitter | `x.com`、`twitter.com` | 无——规划 ASR 工作流 |
| Instagram | `instagram.com` | 无——规划 ASR 工作流 |
| 本地文件 | 任意可读路径 | 仅通过 `--subtitle-file` |

下载统一走 yt-dlp，因此它支持的其他站点也可能可用，但只有上述平台有显式规格。
平台字幕轨是**请求而非保证**：限流会让抓取失败，此时作业报告失败与可选替代，
而不是在后台偷偷切换引擎。

## 安装

需要 **Python 3.11–3.13**。

以下**不是** Python 包，必须在 `PATH` 上：

| 依赖 | 用途 | 说明 |
| --- | --- | --- |
| **FFmpeg + ffprobe** | 全部功能 | 必须**编译了 libass**。验证：`ffmpeg -filters \| grep subtitles` |
| **Deno**（推荐） | YouTube 下载 | yt-dlp ≥ 2025.11.12 需要外部 JS 运行时；Node ≥ 20 亦可 |

```bash
# 核心工作流引擎（含下载、本地离线 Whisper、免 Key / LLM 翻译、FFmpeg 压制）
pip install porter-workflow

# 安装并启动交互式 WebUI 工作台
uv tool install "porter-workflow[web]"
porter                  # 启动 http://127.0.0.1:7860

# 或无需永久安装，直接拉起 WebUI
uvx --from "porter-workflow[web]" porter
```

所有视频本地化能力（含免 Key 离线 `faster-whisper` 以及可选付费的 LLM / Whisper
API 适配器）均内置于基础包 `porter-workflow` 中。仅 `gradio` 保留在可选扩展
`[web]` 里，以保证 `import porter` 在脚本、服务和智能体技能中被导入时足够纯净。

## 用法

### WebUI 工作台（`porter`）

```bash
porter                  # 启动 http://127.0.0.1:7860
porter --port 8080      # 指定端口
porter --version        # 查看版本
```

在工作台中输入视频链接或本地路径，可选择任务范围、目标画质（默认 `1080p`，源片
达不到时平滑降级）、压制模式与各阶段能力；预检后可查看完整的 `WorkflowPlan` JSON，
执行时实时追踪进度、预览硬字幕成片并打包下载 `raw.zip` / `cooked.zip`。

### Python 核心 API（`import porter`）

```python
from porter import plan_source, run_doctor, run_plan

report = run_doctor()
plan = plan_source("https://www.youtube.com/watch?v=...", quality="1080p", burn="zh_only")
if plan.feasible:
    outcome = run_plan(plan)
    print(outcome.workflow.artifacts)
```

`plan_source()` 负责探测源、自动合并生效配置（cookies、输出目录、目标画质、音频
降噪）并生成不可变、可序列化的 `WorkflowPlan`；`run_plan()` 严格按该计划执行，并
统一接入跨进程 `JobRegistry` 与协作式取消。

### 输出目录

```text
<output_dir>/<video_id>_<safe_title>/
├── raw/
│   ├── video.mp4                 H.264 + AAC + faststart 母版
│   ├── audio.wav                 16 kHz 单声道基准音轨
│   ├── audio_enhanced.wav        仅用于 ASR 的降噪音轨
│   ├── transcript.json/.txt      重建后的整句台词本
│   ├── subtitle.srt              平台原生字幕（若有）
│   ├── cover.jpg
│   └── metadata.json
├── cooked/
│   ├── subtitle_bilingual.{srt,ass}
│   ├── subtitle_zh.{srt,ass}
│   ├── video_bilingual.mp4
│   └── video_zh.mp4
└── .tmp/                         临时目录，可在两次运行之间删除
```

## 配置

按以下顺序解析，优先级由高到低：

1. 显式传入 `resolve(path)` 的路径
2. `$PORTER_CONFIG`
3. 环境变量（覆盖所选文件中的对应键）
4. 最近的项目配置：`porter.json` → `porter.toml` → `config.json`
5. 用户级配置目录（`porter/config.json`，仅当没有项目配置）
6. 内置默认值

| 配置段 | 键 | 作用 |
| --- | --- | --- |
| 顶层 | `output_dir`、`video_quality`、`cookies_file`、`cookies_browser` | 输出位置、目标画质（默认 `1080p`）与受限媒体访问 |
| `llm` | `api_key`、`api_base`、`model` | 显式选择 `translate.llm` 与 `transcript.whisper-api` 时使用 |
| `asr` | `engine`、`language`、`audio_denoise`、`whisper_local_model`、`whisper_local_device`、`whisper_local_compute_type`、`whisper_api_key`、`whisper_api_base`、`whisper_model` | 识别默认值与偏好 |
| `refine` | `enabled`、`model` | 可选的 LLM 转录校对 |
| `ffmpeg` | `ffmpeg_path`、`ffprobe_path`、`video_codec`、`preset`、`crf`、`pixel_format`、`audio_codec`、`audio_bitrate`、`audio_sample_rate`、`wav_sample_rate`、`auto_tune` | 编码与硬件调优 |
| `style` | `zh_font`、`en_font`、字号、颜色、`outline_*`、`shadow`、`margin_*`、`fade_*` | ASS 字幕外观 |

`asr.engine` 只是自动选择时的**偏好**，不是付费授权，也不强制运行 ASR。配置可直接在
WebUI「配置文件与首选项」页修改，或通过 `porter.save_key(...)` /
`porter.writable_config_path()` 读写。

## 构建与开发

开发需要 **Python 3.11–3.13**、**uv**、**FFmpeg + libass**，以及 **Deno** 或
**Node ≥ 20**。

```bash
uv sync --extra web --extra dev     # 严格按 uv.lock 安装

ruff check src tests scripts        # 含 T20：引擎禁止 print()
mypy src scripts                    # strict 类型检查
lint-imports                        # 引擎/前端依赖契约
pytest -m "not network"             # 离线测试
```

仓库结构：

```text
src/porter/
├── __init__.py       稳定公开 API（懒加载）与 __version__
├── core/             引擎——全部业务逻辑
│   └── workflow/     计划模型、解析器、探测、执行器与统一门面
└── webui/            可选 Gradio WebUI 工作台（[web]）
scripts/              开发/发布辅助脚本
docs/                 开发、架构、参考、设计、图片
tests/{unit,regression,integration}/
```

- [文档中心](docs/README.md)
- [架构总览](docs/architecture/overview.md)
- [开发门禁](docs/development/gate.md)
- [参与贡献](CONTRIBUTING.md)

## 许可证

MIT，见 [LICENSE](LICENSE)。第三方组件及其许可证列在
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
