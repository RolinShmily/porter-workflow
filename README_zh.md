# Porter Workflow（流媒体搬运工）

<p align="center">
  <a href="README.md">English</a> | <b>简体中文</b>
</p>

**Porter Workflow** 是一条全自动视频本地化流水线：给它一个视频链接，拿回一套标准化素材，以及烧好硬字幕的双语版与纯中文版成片。

它以**一个引擎 + 三个前端**的形态发布：

| 前端 | 入口 | 面向 |
| --- | --- | --- |
| `porter`（CLI） | `porter "<URL>"` | 人类与 Shell 脚本 |
| `porter-mcp` | `uvx --from "porter-workflow[mcp]" porter-mcp` | 通过 MCP 协议接入的 AI 智能体 |
| `porter-skill` | `npx skills add RolinShmily/porter-workflow` | 使用 Agent Skills 规范的智能体 |

三者调用同一个 `porter` 库。引擎内不含任何参数解析，且**绝不向 stdout 写入**——因为 MCP 的 stdio 传输里 stdout 就是 JSON-RPC 通道。

> **状态：`v0.2.0` 正在重构中。**
> 完整设计与分阶段迁移手册见 [`docs/REFACTOR_PLAN.md`](docs/REFACTOR_PLAN.md)。
> `v0.1.x` 的原实现保留在 `main` 分支。

---

## 安装

需要 **Python ≥ 3.10**。

```bash
# CLI，最小安装（纯 Python；翻译无需 Key，但转录需要——见下文）
uvx porter-workflow "<URL>"

# 带全部可选后端
uvx --from "porter-workflow[all]" porter "<URL>"

# 持久安装
pipx install "porter-workflow[all]"
```

### 系统依赖

以下**不是** Python 包，必须在 `PATH` 上：

| 依赖 | 用途 | 说明 |
| --- | --- | --- |
| **FFmpeg + ffprobe** | 全部功能 | 必须**编译了 `libass`** 才能烧硬字幕。验证：`ffmpeg -filters \| grep subtitles` |
| **Deno**（推荐） | YouTube 下载 | yt-dlp ≥ 2025.11.12 需要外部 JS 运行时来解 YouTube 的 JS 挑战。Node ≥ 20 亦可。 |

一次性检查：

```bash
porter doctor
```

### 转录必须有 Key（或 VideoCaptioner CLI）

**不存在可用的免 Key 语音识别路径。** 这是实测结论，不是推测——截至 2026-09-22，在真实 10 分钟视频上：

| ASR 后端 | 状态 |
| --- | --- |
| Whisper API | 需要 `OPENAI_API_KEY`（或兼容端点） |
| VideoCaptioner CLI | 需要单独安装 `videocaptioner` 包 |
| Bcut | 主机有响应，但返回**零条 utterance** |
| Google Web（`[stt]`） | **每次请求**都返回空结果 `{"result":[]}` |

两个免 Key 端点都是逆向来的，现在都不能转录。Google Web 的响应在 HTTP 层也是坏的，所以不是"返回空"而是读取直接失败。Bcut 的失败更含糊——主机有响应，可能是配额或字段变更而非端点死亡——所以源码里它保留"unverified"标签。

所以裸跑 `uvx porter-workflow "<URL>"` 会完成下载与标准化，然后在转录阶段以
`every speech-to-text backend failed` 失败。要拿到字幕，二选一：

```bash
# 方案 A：LLM Key（同时大幅改善翻译质量）
export OPENAI_API_KEY=sk-...
porter "<URL>" --burn skip

# 方案 B：装 VideoCaptioner 用它的引擎
pip install videocaptioner
porter "<URL>" --asr-engine bijian --burn skip
```

**翻译不受影响**——Bing、Google、MyMemory 都无需 Key，逐个对真实端点探测均返回真中文。只有转录需要凭据。

`porter doctor` 会在开工前告诉你任务实际会走哪条路。

### 可选 extras

| Extra | 引入 | 用途 |
| --- | --- | --- |
| `[llm]` | `openai`、`json-repair` | LLM 翻译 + Whisper API 语音识别 |
| `[stt]` | `SpeechRecognition` | Google Web STT —— **实测已失效**，见下文 |
| `[images]` | `pillow` | 封面图处理 |
| `[mcp]` | `fastmcp` | `porter-mcp` 服务端 |
| `[all]` | 以上全部 | —— |

`videocaptioner` **刻意不作为依赖**。它是 GPL-3.0（本项目为 MIT），且锁死 `python<3.13`。Porter 在运行时探测它，存在则跨进程边界把它当作额外的 ASR / 翻译后端。需要这些引擎请自行安装：

```bash
pip install videocaptioner
```

---

## 用法

```bash
# 预检链接，不产生任何下载
porter inspect "<URL>"

# 完整流水线：下载 → 转录 → 翻译 → 压制
porter "<URL>" -o ./porter_output

# 只出字幕，不压制
porter "<URL>" --burn skip

# 环境诊断
porter doctor

# 查看已生效配置（密钥脱敏）
porter config list
```

### 输出目录结构

```
<output_dir>/<video_id>_<safe_title>/
├── raw/
│   ├── video.mp4                 H.264 + AAC + faststart 母版
│   ├── audio.wav                 16 kHz 单声道基准音轨
│   ├── audio_enhanced.wav        仅用于 ASR 的降噪音轨
│   ├── transcript.json/.txt      重建后的整句台词本
│   ├── subtitle.srt              平台原生字幕（若有）
│   ├── cover.jpg
│   └── metadata.json
└── cooked/
    ├── subtitle_bilingual.{srt,ass}
    ├── subtitle_zh.{srt,ass}
    ├── video_bilingual.mp4
    └── video_zh.mp4
```

### 配置

按以下顺序解析，优先级由高到低：

1. `--config <path>`
2. `$PORTER_CONFIG`
3. 环境变量（`OPENAI_API_KEY`、`PORTER_ASR_ENGINE` …）
4. 项目级 `./porter.json`
5. 用户级配置目录（`platformdirs`）
6. 内置默认值

详见 [`docs/CONFIG.md`](docs/CONFIG.md)。

---

## MCP 服务端

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

长任务以 start / status / result / cancel 的形式暴露，而不是一次阻塞调用——因为压制 1080p 视频要几十分钟。详见 [`docs/MCP.md`](docs/MCP.md)。

---

## 开发

```bash
uv venv && uv pip install -e ".[dev,all]"

ruff check src          # 含 T20：引擎禁止 print()
mypy src
lint-imports            # 守卫 引擎/前端 的依赖方向
pytest
```

仓库结构：

```
src/porter/             引擎（库）—— 全部业务逻辑在此
src/porter_cli/         CLI 前端
src/porter_mcp/         MCP 前端
skills/porter-skill/    Agent Skill 资产（SKILL.md、scripts、references）
tests/{unit,regression,integration}/
docs/
```

---

## 许可证

MIT，见 [LICENSE](LICENSE)。

第三方致谢（含 VideoCaptioner，GPL-3.0，启发了 ASR 编排的部分设计）将在 `v0.2.0` 的最终 README 中完整迁移。
