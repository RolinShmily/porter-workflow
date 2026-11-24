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
# 核心安装：下载、平台字幕、免费翻译、压制
uvx --from porter-workflow porter "<URL>"

# 增加本地、免 Key、可离线的语音识别
uvx --from "porter-workflow[asr-local]" porter "<URL>"

# 全部能力，含 MCP 服务端与 WebUI
uvx --from "porter-workflow[all]" porter "<URL>"

# 首次运行前检查环境
uvx --from porter-workflow porter doctor
```

| Extra | 引入 | 用途 |
| --- | --- | --- |
| `[asr-local]` | `faster-whisper` | `transcript.whisper-local`——免 Key、可离线 |
| `[llm]` | `openai`、`json-repair` | `translate.llm` + `transcript.whisper-api` |
| `[images]` | `pillow` | 封面图处理 |
| `[mcp]` | `fastmcp` | `porter-mcp` 服务端 |
| `[web]` | `gradio` | `porter-web` 工作台 |
| `[all]` | 以上全部 | —— |

核心安装已经可以处理带平台字幕轨的链接：免费翻译并压制。Extra 只是增加能力，
不改变工作流模型。当前环境实际能跑什么，以 `porter doctor` 与
`porter capabilities` 为准。

## 用法

```bash
porter capabilities                 # 当前安装能执行哪些能力？
porter probe "<URL>"                # 只看源事实，不规划、不下载
porter plan "<URL>"                 # 解析工作流、能力与阻塞项
porter plan "<URL>" --json > plan.json
porter run --plan plan.json         # 严格执行这份计划
porter run "<URL>"                  # 一步完成解析与执行
porter run "<URL>" -o ./output      # 指定输出目录
porter run "<URL>" --task subtitle  # 只出字幕，不编码视频
porter doctor                       # 环境诊断
porter config list                  # 查看生效配置（密钥脱敏）
```

`porter plan` 是权威入口：它探测源、探测本机能力，报告每个阶段将要执行的唯一
能力、成本与数据出口，以及带 `fix` 的阻塞项。计划可复现，所以
`plan --json` → `run --plan` 是推荐给智能体与脚本的契约。

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

### 前端入口

| 前端 | 命令 | 面向 |
| --- | --- | --- |
| CLI | `porter` | 人类、脚本，以及权威的计划契约 |
| MCP | `uvx --from "porter-workflow[mcp]" porter-mcp` | 通过 stdio JSON-RPC 接入的智能体 |
| WebUI | `uvx --from "porter-workflow[web]" porter-web` | 使用计划工作台与人机协同的人类 |
| Agent Skill | `npx skills add RolinShmily/porter-workflow` | 按 SOP 执行的智能体 |

四者使用同一份 `WorkflowPlan` / `WorkflowResult` 模型。MCP 用 `porter_plan`
生成计划、用 `porter_job_start(plan=...)` 启动；WebUI 展示计划 JSON 并执行同一份
计划。没有任何入口保留第二条流水线或运行期回退链。配置细节见：

- [`skills/porter-skill/SKILL.md`](skills/porter-skill/SKILL.md)——SOP
- [`skills/porter-skill/references/PLAN.md`](skills/porter-skill/references/PLAN.md)——计划/结果字段与问题码
- [`skills/porter-skill/references/MCP.md`](skills/porter-skill/references/MCP.md)——MCP 工具与调用顺序
- [`skills/porter-skill/references/WEBUI.md`](skills/porter-skill/references/WEBUI.md)——WebUI 工作台

## 配置

按以下顺序解析，优先级由高到低：

1. `--config <path>`
2. `$PORTER_CONFIG`
3. 环境变量（覆盖所选文件中的对应键）
4. 最近的项目配置：`porter.json` → `porter.toml` → `config.json`
5. 用户级配置目录（仅当没有项目配置）
6. 内置默认值

| 配置段 | 键 | 作用 |
| --- | --- | --- |
| 顶层 | `output_dir`、`cookies_file`、`cookies_browser` | 输出位置与受限媒体访问 |
| `llm` | `api_key`、`api_base`、`model` | 显式选择 `translate.llm` 与 `transcript.whisper-api` 时使用 |
| `asr` | `engine`、`language`、`audio_denoise`、`whisper_local_model`、`whisper_local_device`、`whisper_local_compute_type`、`whisper_api_key`、`whisper_api_base`、`whisper_model` | 识别默认值与偏好 |
| `refine` | `enabled`、`model` | 可选的 LLM 转录校对 |
| `ffmpeg` | `ffmpeg_path`、`ffprobe_path`、`video_codec`、`preset`、`crf`、`pixel_format`、`audio_codec`、`audio_bitrate`、`audio_sample_rate`、`wav_sample_rate`、`auto_tune` | 编码与硬件调优 |
| `style` | `zh_font`、`en_font`、字号、颜色、`outline_*`、`shadow`、`margin_*`、`fade_*` | ASS 字幕外观 |

`asr.engine` 只是自动选择时的**偏好**，不是付费授权，也不强制运行 ASR。是否
运行语音识别由计划决定。

```bash
porter config list                     # 生效值 + 来源，密钥脱敏
porter config get llm                  # 查看某一段
porter config set llm.model=deepseek-chat
porter config set llm.api_key=sk-...   # 默认写入用户级配置
```

完整键表、默认值与示例见
[`skills/porter-skill/references/CONFIG.md`](skills/porter-skill/references/CONFIG.md)。

## 构建与开发

开发需要 **Python 3.11–3.13**、**uv**、**FFmpeg + libass**，以及 **Deno** 或
**Node ≥ 20**。运行时依赖只有 `yt-dlp[default]`、`requests`、`pydantic` 和
`platformdirs`，其余引擎能力都是可选 extra。

```bash
uv sync --extra all --extra dev      # 严格按 uv.lock 安装

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
│   └── workflow/     计划模型、解析器、探测与执行器
├── cli/              CLI 前端
├── mcp/              MCP 前端
└── webui/            Gradio WebUI 前端
skills/porter-skill/  Agent Skill 资产（SKILL.md、Python 脚本、references）
scripts/              开发/发布辅助脚本
docs/                 开发、架构、参考、设计、图片
tests/{unit,regression,integration}/
```

- [文档中心](docs/README.md)
- [架构总览](docs/architecture/overview.md)
- [开发门禁](docs/development/gate.md)
- [CLI 参考](docs/reference/cli.md)
- [参与贡献](CONTRIBUTING.md)

## 许可证

MIT，见 [LICENSE](LICENSE)。第三方组件及其许可证列在
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
