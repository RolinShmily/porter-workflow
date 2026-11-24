# 环境与依赖（SOP 展开）

本页展开 [`SKILL.md`](../SKILL.md) §0 的三道门。目标是**在下载任何媒体之前**确认：

1. 这台机器能跑 porter（`doctor`）
2. 每个阶段有哪些对等能力（`capabilities`）
3. 这次输入的源是什么（`probe`）

环境检查每个会话做一次即可；安装依赖、切换 `PORTER_SKILL_SPEC` 或改配置后重跑。

**porter 没有回退链。** 这里的每一道门都是为了在 plan 之前把“能选什么”确定下来；
计划一旦生成，运行期不会换能力。

---

## 0. 入口：Python 与 uvx

```bash
python3 --version      # Windows: py -3 --version
uvx --version          # 没有时先安装 uv
```

- porter 要求 Python ≥ 3.11；包装器 `scripts/porter.py` 只要求 ≥ 3.8，`uvx` 会解析
  或下载 porter 需要的解释器。
- 本文命令写作 `python3 <SKILL_DIR>/scripts/porter.py`；Windows 用
  `py -3 <SKILL_DIR>\scripts\porter.py`。
- 没有 Python：由 Agent 指导安装，不要静默改系统环境。没有 uv：按 uv 官方方式安装。
- 技能入口只有一个 Python 文件（`scripts/porter.py`），没有 Bash/PowerShell 两套
  实现；`scripts/bootstrap.py` 只用于离线或源码 checkout 的 venv 安装。

---

## 1. 完整依赖清单

| 依赖 | 用途 | 缺失时 |
|---|---|---|
| Python ≥ 3.11 | 运行 porter | blocker |
| `uv` / `uvx` | 技能包装器解析并运行发行包 | CLI 入口不可用 |
| FFmpeg + ffprobe | 探测、标准化、提取音轨、压制 | blocker（除 `subtitle.only`） |
| FFmpeg 的 libass（`ass` 滤镜） | 烧硬字幕 | 只能出字幕，不能压制 |
| Deno 或 Node ≥ 20 | yt-dlp 处理 YouTube JS 挑战 | YouTube 抽取格式变少，可能下载失败 |
| 中文字体 | ASS 渲染 | 中文显示为方块 |
| `porter-workflow` 基础包 | yt-dlp、requests、pydantic、平台字幕、翻译、压制 | blocker |
| `[asr-local]` | 本地 faster-whisper，免费离线 | 无字幕轨时必须付费或提供字幕 |
| `[llm]` | Whisper API、LLM 翻译 | 只是付费能力不可用 |
| `[mcp]` / `[web]` | 旧版前端 | 与本文 SOP 无关 |

翻译的免 Key 端点只需要基础包（`requests`）。`[asr-local]` 首次运行会下载模型
（默认 `small` 约 484 MB）；安装前取得用户同意。

---

## 2. 门一：`doctor`

```bash
python3 <SKILL_DIR>/scripts/porter.py doctor --json
```

### 输出结构

```jsonc
{
  "platform": "Linux ...",
  "ok": false,
  "blockers": ["ffmpeg"],
  "degradations": ["js_runtime"],
  "findings": [
    {
      "key": "ffmpeg",
      "label": "ffmpeg & ffprobe",
      "ok": false,
      "severity": "blocker",
      "detail": "not found on PATH",
      "remediation_key": "ffmpeg"
    }
  ],
  "guidance": {
    "ffmpeg": {
      "summary": "ffmpeg and ffprobe must both be installed and on PATH.",
      "steps": ["Debian/Ubuntu:  sudo apt install ffmpeg", "..."],
      "note": "...",
      "url": "https://ffmpeg.org/download.html"
    }
  }
}
```

### 判定规则

- **退出码 0 且 `blockers` 为空**才通过。退出码 1 表示存在 blocker。
- `degradations` 不阻塞，但必须向用户说明影响。
- `guidance` 只包含失败项的修复步骤；`steps` 可直接执行。
- `doctor` 检查：Python、ffmpeg/ffprobe、版本、libass、编码器、yt-dlp、JS runtime、
  中文字体、输出目录写权限、ASR 能力、翻译能力。
- `doctor` **不验证**远端 Key、额度、网络；这些要等真实调用。
- `doctor` 不会创建输出目录：它探测最近的已存在父目录，所以通过不代表目录已建好，
  只代表可以创建。

### 常见 blocker 的处理方向

| finding | 处理 |
|---|---|
| `ffmpeg` / `ffmpeg_version` | 按 `guidance.ffmpeg.steps` 安装；确认 `ffprobe` 也在 PATH |
| `ffmpeg_libass` | 换带 libass 的构建；只出字幕可用 `--task subtitle` 绕过 |
| `js_runtime` | 装 Deno（推荐）或 Node ≥ 20；任何受支持的运行时都够 |
| `yt_dlp` | `uv pip install yt-dlp`；它是基础包依赖，通常不会缺 |
| `font` | 装 Noto Sans CJK，或在 `porter.json` 改 `style.zh_font` |
| `output_dir` | 换可写路径（`-o` / `PORTER_OUTPUT_DIR`） |
| `asr_route`（degraded） | 有字幕轨可继续；否则装 `[asr-local]` 或配置付费 Key |
| `translation_route` | 不会 blocker；免 Key 端点默认可用 |

修完重跑 `doctor`，直到退出码 0。

---

## 3. 门二：`capabilities`

```bash
python3 <SKILL_DIR>/scripts/porter.py capabilities --json
python3 <SKILL_DIR>/scripts/porter.py capabilities --stage transcript --available --json
```

每项字段：

```jsonc
{
  "id": "transcript.whisper-local",
  "stage": "transcript",
  "available": true,
  "verified": true,
  "requires": ["faster-whisper"],
  "cost": "free",
  "egress": "none",
  "download_mb": 0,
  "params_schema": {},
  "description": "Local faster-whisper recognition. ..."
}
```

- `available=false` 的能力仍会列出：区分“未安装”与“不支持”。
- `cost=paid` 默认不会被 planner 选中。
- `download_mb>0` 表示首次运行会下载模型，必须先告知用户。
- `egress=audio` 表示音频会上传到第三方（Whisper API）；`text` 表示字幕文本。

常用过滤：

```bash
# 转录阶段有哪些可用能力
porter capabilities --stage transcript --available

# 翻译阶段全部能力（含不可用）
porter capabilities --stage translate
```

---

## 4. 门三：`probe`

```bash
python3 <SKILL_DIR>/scripts/porter.py probe "<SOURCE>" --json
```

- `kind` 决定工作流：`url` / `local_video` / `subtitle` / `audio`。
- `tracks[].verified=false` 只是“平台列出”，不是“已抓取”。
- `warnings` 里的 blocker 表示源不可用；退出码 1。
- 本地文件用 ffprobe 实测 `has_video` / `has_audio`；URL 的 `has_audio` 在探测阶段
  固定为 `false`（元数据不能证明音频流存在）。
- 需要登录：`--cookies FILE` 或 `--cookies-from-browser BROWSER`。

`probe` 不下载媒体；它是安全的预检。

---

## 5. Python 包与 extras

| 安装 | 带来什么 | 何时需要 |
|---|---|---|
| `porter-workflow` | yt-dlp、requests、pydantic、platformdirs；平台字幕、翻译、压制 | 默认 |
| `porter-workflow[asr-local]` | `faster-whisper`、`ctranslate2` | **免费离线 ASR（推荐）** |
| `porter-workflow[llm]` | `openai`、`json-repair` | Whisper API、LLM 翻译（付费） |
| `porter-workflow[mcp]` | `fastmcp` | 旧版 MCP server |
| `porter-workflow[web]` | `gradio` | 旧版 WebUI |
| `porter-workflow[all]` | 以上全部 | 一次装全；体积最大 |

**决策规则：**

1. 源有平台字幕轨 → 基础包即可转录（但抓取可能失败，见 [`PLAN.md`](PLAN.md)）。
2. 源无字幕轨且不付费 → 必须装 `[asr-local]`。
3. 愿意付费 → 装 `[llm]` 并配置 `WHISPER_API_KEY` / `OPENAI_API_KEY`；计划仍需显式
   命名付费能力或 `--allow-paid`。
4. 只处理字幕文件 → 基础包即可，不需要 ffmpeg 或 ASR。

### 技能包装器

```bash
# 默认基础包
python3 <SKILL_DIR>/scripts/porter.py --version

# 免费本地 ASR
export PORTER_SKILL_SPEC='porter-workflow[asr-local]'

# 再加付费能力
export PORTER_SKILL_SPEC='porter-workflow[asr-local,llm]'
```

设置后，同一会话里的 `plan` / `run` / `jobs status` 都要沿用同一个 spec，否则可能
是两个不同的临时环境。

---

## 6. 配置来源（只读检查）

```bash
porter config path
porter config list      # Source + 生效值，密钥打码
porter config get asr
porter config get llm
```

优先级从高到低：`--config` → `$PORTER_CONFIG` → 环境变量 → 最近的项目配置
（`porter.json` → `porter.toml` → `config.json`）→ 用户配置 → 内置默认。

项目配置与用户配置**二选一**，不是深合并。完整键表见 [`CONFIG.md`](CONFIG.md)。

**Key 的安全放置：**

- 首选环境变量：`WHISPER_API_KEY`、`OPENAI_API_KEY`。
- 其次 `porter config set llm.api_key=...`（用户级配置）。
- 不要写进仓库、技能目录或对话记录。

---

## 7. 开作业前的检查清单

- [ ] `doctor --json` 退出码 0，`blockers` 为空；`degradations` 已向用户说明
- [ ] 需要的能力在 `capabilities` 中 `available=true`
- [ ] `probe` 的 `kind` 与 `warnings` 已确认
- [ ] `plan --json` 的 `feasible=true`
- [ ] 付费能力已显式命名或 `--allow-paid`，且用户已同意费用
- [ ] 模型下载体积（`download_mb`）与数据出口（`egress`）已告知用户
- [ ] 输出目录可写，且位于宿主允许的路径内
- [ ] 计划已保存为 `plan.json`，`plan_id` 已记录
