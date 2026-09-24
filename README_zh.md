# Porter Workflow（流媒体搬运工）

<p align="center">
  <a href="README.md">English</a> | <b>简体中文</b>
</p>

<p align="center">
  <a href="https://github.com/RolinShmily/porter-workflow/actions/workflows/test.yml"><img alt="CI" src="https://github.com/RolinShmily/porter-workflow/actions/workflows/test.yml/badge.svg"></a>
  <a href="https://pypi.org/project/porter-workflow/"><img alt="PyPI" src="https://img.shields.io/pypi/v/porter-workflow"></a>
  <img alt="Python 3.11 - 3.13" src="https://img.shields.io/badge/python-3.11%20%E2%80%93%203.13-blue">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

**Porter Workflow** 是一条全自动视频本地化流水线：给它一个视频链接，拿回一套标准化素材，以及烧好硬字幕的双语版与纯中文版成片。

它以**一个引擎 + 三个前端**的形态发布：

| 前端 | 入口 | 面向 |
| --- | --- | --- |
| `porter`（CLI） | `porter "<URL>"` | 人类与 Shell 脚本 |
| `porter-mcp` | `uvx --from "porter-workflow[mcp]" porter-mcp` | 通过 MCP 协议接入的 AI 智能体 |
| `porter-skill` | `npx skills add RolinShmily/porter-workflow` | 使用 Agent Skills 规范的智能体 |

三者调用同一个 `porter` 库。引擎内不含任何参数解析，且**绝不向 stdout 写入**——因为 MCP 的 stdio 传输里 stdout 就是 JSON-RPC 通道。

> **状态：`v0.2.x` 是 `main` 上的当前开发线。**
> v0.2 重构（一个引擎 + 三个前端）已落地。v0.1 的原实现保留在 git 历史中，
> 最后一个 v0.1 提交为 `b5fd577`。

---

## 安装

需要 **Python ≥ 3.11**。

```bash
# CLI，最小安装（纯 Python；翻译无需 Key，但转录需要 [asr-local] extra 或 API Key——见下文）
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

### 转录免 Key 且离线，或者用 API Key

**本地 Whisper 跑在你自己机器上：无需 Key，首次之后不再联网。** 2026-09-24 实测：18 秒真实语音，默认 `small` 模型，CPU 上 7.2 秒产出 3 条字幕：

| ASR 后端 | 需要 | 状态 |
| --- | --- | --- |
| **`whisper-local`** | `[asr-local]` extra | **可用。** 免 Key；模型缓存后完全离线 |
| Whisper API | `OPENAI_API_KEY`（或兼容端点） | 可用 |
| VideoCaptioner CLI | 单独安装 `videocaptioner` 包 | 可用 |
| Bcut | 无 | 主机有响应，但返回**零条 utterance** |
| Google Web（`[stt]`） | 无 | **每次请求**都返回空结果 `{"result":[]}` |

`whisper-local` 排在链首，所以只要装了它，任务就会用它。它放在 extra 里，是因为 `faster-whisper` 与 `ctranslate2` 体积不小，而且是 CTranslate2 而非 PyTorch：

```bash
# 免 Key、离线。这是推荐的安装方式。
uvx --from "porter-workflow[asr-local]" porter "<URL>"

# 或者一次装全 —— [all] 包含 [asr-local]
uvx --from "porter-workflow[all]" porter "<URL>"
```

首次运行会从 Hugging Face 下载模型（默认 `small` 为 464 MB），之后完全离线。设备与精度自动选择——能加载 CUDA 就用，否则回落 CPU——也可以固定：

```bash
porter config set asr.whisper_local_model=medium
porter config set asr.whisper_local_device=cuda
porter config set asr.whisper_local_compute_type=float16
```

裸跑 `uvx porter-workflow "<URL>"` 仍然不能转录，因为最小安装里没有任何 ASR 后端：它会完成下载与标准化，然后以 `every speech-to-text backend failed` 失败。真正失效的是两个免 Key *端点*——都是逆向来的，且 Google Web 的响应在 HTTP 层就是坏的，所以不是"返回空"而是读取直接失败。Bcut 的失败更含糊——主机有响应，可能是配额或字段变更而非端点死亡——所以源码里它保留 "unverified" 标签。

要拿到字幕，三选一：

```bash
# 方案 A（推荐）：本地、免 Key、离线
uvx --from "porter-workflow[asr-local]" porter "<URL>" --burn skip

# 方案 B：LLM Key（同时大幅改善翻译质量）
export OPENAI_API_KEY=sk-...
porter "<URL>" --burn skip

# 方案 C：装 VideoCaptioner 用它的引擎
pip install videocaptioner
porter "<URL>" --asr-engine bijian --burn skip
```

**翻译不受影响**——Bing、Google、MyMemory 都无需 Key，逐个对真实端点探测均返回真中文。从来只有转录需要凭据。

`porter doctor` 会在开工前告诉你任务实际会走哪条路。

### 可选 extras

| Extra | 引入 | 用途 |
| --- | --- | --- |
| `[asr-local]` | `faster-whisper` | **本地 Whisper 语音识别 —— 免 Key、可离线**，见上文 |
| `[llm]` | `openai`、`json-repair` | LLM 翻译 + Whisper API 语音识别 |
| `[stt]` | `SpeechRecognition` | Google Web STT —— **实测已失效**，见上文 |
| `[images]` | `pillow` | 封面图处理 |
| `[mcp]` | `fastmcp` | `porter-mcp` 服务端 |
| `[all]` | 以上全部 | —— |

`videocaptioner` **刻意不作为依赖**。它是 GPL-3.0（本项目为 MIT），且锁死 `python<3.13`。Porter 在运行时探测它，存在则跨进程边界把它当作额外的 ASR / 翻译后端。需要这些引擎请自行安装：

```bash
pip install videocaptioner
```

### 慢或被墙？国内镜像

porter 的两处下载默认都指向在国内很难受的端点：包走 PyPI，Whisper 权重走
Hugging Face。当机器看起来在中国（时区、UTC+8、或 `zh` 区域设置）时，porter 会
自动改用国内镜像：

| 内容 | 其他地区默认 | 国内默认 | 可用它改掉 |
| --- | --- | --- | --- |
| Python 包 | PyPI | [中科大](https://mirrors.ustc.edu.cn/pypi/simple) | `UV_DEFAULT_INDEX` |
| Whisper 权重 | Hugging Face | [魔搭 ModelScope](https://modelscope.cn) | `HF_ENDPOINT` |

魔搭上的 `faster-whisper-*` 是官方 CTranslate2 权重，不是重新转换的版本：它的
`config.json` 与 `Systran/faster-whisper-small` 的**逐字节相同**（SHA-256 一致）。
每个模型尺寸只需从那里下一次。

想强制指定方向，或用自己选择的源：

```bash
PORTER_MIRROR=cn     # 总是优先用镜像
PORTER_MIRROR=off    # 从不用；始终走官方源
```

**你手动设置的索引永远优先**，porter 绝不覆盖。设了 `UV_DEFAULT_INDEX`（或旧名字
`UV_INDEX_URL`）就用你的。`PIP_INDEX_URL` 同样被尊重，并且会被复制给 uv —— 因为
uv 自己**不认** `PIP_INDEX_URL`，只设它（`pip config set global.index-url <镜像>`
就是这么干的）会静默失效。

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

长任务以 start / status / result / cancel 的形式暴露，而不是一次阻塞调用——因为压制 1080p 视频要几十分钟。

---

## 开发

```bash
uv sync --extra all --extra dev      # 严格按 uv.lock 安装

ruff check src          # 含 T20：引擎禁止 print()
mypy src
lint-imports            # 守卫 引擎/前端 的依赖方向
pytest
```

[`CONTRIBUTING.md`](CONTRIBUTING.md) 记录了完整门禁、这些工具强制的架构规则，
以及新增依赖时的许可证规则。

仓库结构：

```
src/porter/             引擎（库）—— 全部业务逻辑在此
src/porter_cli/         CLI 前端
src/porter_mcp/         MCP 前端
skills/porter-skill/    Agent Skill 资产（SKILL.md、scripts、references）
tests/{unit,regression,integration}/
```

---

## 许可证

MIT，见 [LICENSE](LICENSE)。

---

## 致谢

Porter 站在他人的工作之上，这份亏欠值得写清楚：

* **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** —— 跨五个平台的下载能成为已解决
  的问题而非五个爬虫，靠的就是它。
* **[VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner)**（GPL-3.0）
  —— 其断句、对齐与字幕处理设计影响了 `porter.subtitles` 与 ASR 链的思路。
  仅借鉴思路：未复制任何代码，也从不 import——只作为外部进程调用，这正是
  porter 能保持 MIT 的原因。
* **[FFmpeg](https://ffmpeg.org/) 与
  [libass](https://github.com/libass/libass)** —— 整个媒体层：标准化、降噪与专业
  ASS 渲染。

每个组件、其许可证，以及 porter 是依赖它、可选安装它、还是仅以子进程方式调用它，
都记录在 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

---

## 参与贡献

欢迎贡献。[`CONTRIBUTING.md`](CONTRIBUTING.md) 说明开发环境与每次改动必须通过的
门禁；[`SECURITY.md`](SECURITY.md) 说明如何私下报告安全漏洞；版本历史见
[`CHANGELOG.md`](CHANGELOG.md)。

本项目遵循 [Contributor Covenant](CODE_OF_CONDUCT.md)。
