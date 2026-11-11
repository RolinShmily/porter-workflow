# 环境与配置检查（SOP）

本页是 [`SKILL.md`](../SKILL.md) 第 1–2 步的展开清单。目标是**在下载任何媒体之前**
确认：入口能运行、系统依赖齐全、ASR 有可用路由、配置来源明确、输出目录可写。

环境检查每个会话做一次即可；安装依赖、切换 `PORTER_SKILL_SPEC` 或改配置后重跑。

---

## 1. 入口检查

### CLI

```bash
uv --version
uvx --version
<SKILL_DIR>/scripts/porter.sh --version
```

PowerShell：

```powershell
& '<SKILL_DIR>\scripts\porter.ps1' --version
```

`porter.sh` / `porter.ps1` 把参数原样转发给 `uvx --from "$PORTER_SKILL_SPEC" porter`。
默认 spec 是 `porter-workflow[llm]`；需要本地 ASR 时设置：

```bash
export PORTER_SKILL_SPEC='porter-workflow[llm,asr-local]'
```

设置后，同一会话里的 `run`、`jobs status` 等都要沿用同一个 spec，否则可能是两个
不同的临时环境。

### MCP

如果宿主能看到以下工具，说明 MCP 已配置：

```text
porter_version, porter_doctor, porter_inspect, porter_plan,
porter_job_start, porter_job_status, porter_job_result, porter_job_cancel,
porter_job_list, porter_config
```

MCP 的配置方法见 [`MCP.md`](MCP.md)。注意：`npx skills add` **不会**安装 MCP server。

### WebUI

```bash
uvx --from "porter-workflow[web]" porter-web --help
```

---

## 2. 系统依赖

| 依赖 | 是否阻塞 | 确认 |
|---|---|---|
| Python ≥ 3.11 | 是 | `python3 --version` |
| uv / uvx | 是（CLI 路径） | `uv --version` |
| FFmpeg + ffprobe | 是 | `ffmpeg -version`、`ffprobe -version` |
| FFmpeg 的 libass 支持 | 是（压制） | `ffmpeg -filters \| grep subtitles` |
| Deno 或 Node ≥ 20 | YouTube 是，其他平台否 | `porter doctor` 的 JS runtime 项 |
| 字体 | 压制需要 | `porter doctor` 的 font 项 |

安装方向：

- **uv**：按 uv 官方文档安装；不要静默修改系统级环境。
- **FFmpeg + libass**：优先系统包管理器（Linux 发行版、Homebrew、winget 等）。
  Windows 上注意 `ffmpeg.exe` 与 `ffprobe.exe` 都要在 `PATH`。
- **Deno / Node**：YouTube 的 yt-dlp JS 挑战需要外部运行时；Deno 优先。
- 安装后确认 `ffmpeg`、`ffprobe` 与 JS runtime 都在当前 shell 的 `PATH` 上。

验证：

```bash
porter doctor --json
```

- 退出码 0 = 没有 blocker；退出码 1 = 有阻塞项。
- `blockers` 必须清零才能开作业。
- `degradations` 不阻塞，但要向用户说明（例如没有硬件编码器、只能用软件编码）。
- `doctor` 只验证**能力存在性**（命令、滤镜、Key 是否设置），不验证远端 Key 是否
  有效、额度是否充足；真实端点要等第一次调用才会暴露问题。

---

## 3. Python extras 怎么选

| Extra | 带来什么 | 什么时候需要 |
|---|---|---|
| 基础包 | yt-dlp、pydantic、platformdirs、requests | 预检、规划、平台字幕、翻译、压制 |
| `[llm]` | `openai`、`json-repair` | Whisper API ASR、LLM 翻译/精修 |
| `[asr-local]` | `faster-whisper`、`ctranslate2` | **免 Key 本地 ASR（推荐）**；首次下载模型 |
| `[mcp]` | `fastmcp` | `porter-mcp` |
| `[web]` | `gradio` | `porter-web` |
| `[all]` | 以上全部 | 想一次装全；体积最大 |

**决策规则：**

1. 平台有原生字幕轨 → 不装 ASR 也能转录（但字幕抓取可能失败并回退 ASR）。
2. 没有平台字幕且没有 API Key → 必须装 `[asr-local]`，否则无法转录。
3. 有 OpenAI 兼容 Key → `[llm]` 即可；本地 ASR 仍可作为离线回退。
4. 装了 VideoCaptioner CLI → 可作为外部 ASR/翻译后端，但它是 GPL-3.0 且
   `python<3.13`，不是 porter 的依赖。

安装大依赖（尤其 `[asr-local]`）前先征得用户同意，并说明首次模型下载体积
（默认 `small` 约 464 MB）。

---

## 4. 配置检查

### 4.1 来源与优先级

从高到低：

1. `--config <path>`
2. `$PORTER_CONFIG`
3. 环境变量（覆盖所选文件中的对应键）
4. 最近的项目配置：`porter.json` → `porter.toml` → `config.json`
5. 用户级配置目录（仅当没有项目配置时）
6. 内置默认值

项目配置与用户配置是**二选一**：找到项目配置后，用户配置不再参与。`porter config
list` 的 `Source:` 行就是实际使用的文件。

```bash
porter config path
porter config list
porter config get llm
porter config get asr
```

密钥永远以 `sk-...abcd` 形式打码；把输出贴给用户或写进报告是安全的。

### 4.2 ASR 路由

| 顺序 | 后端 | 前提 | 离线 |
|---|---|---|---|
| 1 | `whisper-local` | `[asr-local]` 已安装 | 模型缓存后可以 |
| 2 | `whisper-api` | `WHISPER_API_KEY` 或 `OPENAI_API_KEY` | 否 |
| 3 | `videocaptioner` | 外部 CLI 在 `PATH` 或 `~/.local/bin` | 取决于它的引擎 |

- `asr.engine` / `PORTER_ASR_ENGINE` / `--asr-engine` 只做**提升**，不做过滤。
- 本地模型/设备/精度：`asr.whisper_local_model`、`whisper_local_device`、
  `whisper_local_compute_type`。
- 没有可用后端且没有平台字幕 → 作业会在 transcribe 阶段失败；不要硬跑。

### 4.3 翻译

- 不配 LLM Key 也能翻译：`llm` 不可用时会走 `bing` → `google` → `mymemory`。
- 中文目标会做 CJK 自检；返回原文/英文会被判失败并换下一个后端。
- 想稳定质量就配置 `llm.api_key` / `llm.api_base` / `llm.model`。

### 4.4 输出与受限宿主

- 默认 `output_dir=./porter_output`。
- 受限宿主：读 [`HOSTS.md`](HOSTS.md)，把 `PORTER_SKILL_STATE_DIR` 指到获准的
  **绝对路径**，并确保 `-o` 也在获准目录内。
- `doctor` 通过不代表当前目录可写；必要时显式 `-o`。

---

## 5. 开作业前的检查清单

- [ ] `porter --version` 或 `porter_version` 有输出
- [ ] `porter doctor` 退出码 0，无 blocker；degradation 已向用户说明
- [ ] `ffmpeg -filters | grep subtitles` 有输出
- [ ] YouTube 链接已确认 JS runtime
- [ ] 已明确 ASR 路由：平台字幕 / 本地 / API / VideoCaptioner
- [ ] 已明确翻译路由与是否需要 LLM Key
- [ ] 输出目录可写，且位于宿主允许的路径内
- [ ] 已把 `plan` 的结论和成本报给用户并取得确认
