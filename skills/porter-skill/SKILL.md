---
name: porter-skill
description: >
  Agent-driven SOP for automated video localization on YouTube, X/Twitter,
  Instagram, TikTok and Bilibili, plus local video, audio and subtitle files.
  Starts by establishing the complete runtime environment with `doctor`, then
  teaches the seven-stage workflow model and drives it through
  probe → plan → confirm → run --plan → poll → quality check. A plan selects
  exactly one peer capability per stage; there is no runtime fallback chain.
  Free by default: a platform subtitle track or local Whisper via the
  [asr-local] extra, and key-free translation. The Whisper API needs a
  WHISPER_API_KEY (or OPENAI_API_KEY) and is opt-in. Use whenever the user asks
  to download a video, generate or translate subtitles, transcribe audio, make
  a Chinese-subtitled (熟肉) version, or batch-process local media.
license: MIT
compatibility: >
  Python >=3.11, uv, and FFmpeg with libass. YouTube also needs a JavaScript
  runtime (Deno or Node >=20). Free transcription requires the optional
  [asr-local] extra; without it, a source must already carry a subtitle track.
  Paid transcription requires an OpenAI-compatible key. Translation works with
  no key. CLI is the canonical interface; the MCP server and WebUI resolve and
  execute the same WorkflowPlan model.
metadata:
  homepage: https://github.com/RolinShmily/porter-workflow
  version: "0.3.2"
---

# Porter Skill（流媒体搬运工 SOP）

本技能是 **Agent 驱动的标准作业流程**，不是参数大全。按顺序执行，每一步达到
“完成标准”再进入下一步：

```
环境三道门 → 理解工作流模型 → probe → plan → 用户确认 → run --plan → 轮询 → 质检
```

**核心规则：没有运行期回退链。** 一个计划为每个阶段选定**恰好一个**能力；能力执行
失败时，计划报告候选能力与建议工作流，由 Agent 重新规划，而不是在运行中偷偷换引擎。

**CLI 是权威入口。** MCP 与 WebUI 现在解析并执行同一份 `WorkflowPlan`，但 CLI 的
`plan --json` → `run --plan` 仍是可复制、可审阅的标准契约，见 §8。

下文命令写作 `python3 <SKILL_DIR>/scripts/porter.py`。Windows PowerShell 把
`python3` 换成 `py -3`、把路径分隔符换成 `\`；入口是同一个 Python 文件。

---

## 0. 完整运行环境（开工前必须通过）

### 0.0 入口：先确认 Python

```bash
python3 --version      # Windows: py -3 --version
```

- porter 本体要求 **Python ≥ 3.11**；包装器 `scripts/porter.py` 只要求 ≥ 3.8，
  由 `uvx` 负责解析 porter 需要的 3.11+（必要时下载 uv 托管的解释器）。
- `python3` 不存在时先试 `python --version`、`py -3 --version`；都不可用才需要安装。
- **没有 Python 或版本过低时**：由 Agent 按宿主平台指导安装（python.org 安装包、
  系统包管理器，或安装 uv 后 `uv python install 3.12`）。技能不附带安装器，也不
  应该用静默脚本改系统环境。
- `uvx` 缺失时，包装器会以退出码 127 给出明确提示；按 uv 官方方式安装后再试。

### 0.1 这台机器需要什么

| 依赖 | 用途 | 是否必需 |
|---|---|---|
| Python ≥ 3.11 | 运行 porter | 是 |
| `uv` / `uvx` | 技能包装器的安装与运行 | 是（CLI 路径） |
| FFmpeg + ffprobe | 探测、标准化、提取音轨、压制 | 是 |
| FFmpeg 的 **libass**（`ass` 滤镜） | 烧硬字幕 | 压制必需；只出字幕则不需要 |
| JavaScript runtime（Deno 或 Node ≥ 20） | yt-dlp 处理 YouTube 挑战 | YouTube 必需 |
| 中文字体（如 Microsoft YaHei / Noto Sans CJK） | ASS 字幕渲染 | 压制需要 |
| 基础包 `porter-workflow` | 平台字幕、翻译、压制、yt-dlp | 是 |
| `[asr-local]` extra | 免费、离线的本地 Whisper | 无字幕轨且不付费时必需 |
| `[llm]` extra | Whisper API、LLM 翻译 | 仅付费能力需要 |
| `WHISPER_API_KEY` / `OPENAI_API_KEY` | 付费 Whisper API | 可选 |

翻译**不需要 Key**：Bing / Google / MyMemory 都是免 Key 对等能力。付费能力默认
不会被选中。

### 0.2 门一：机器健康 —— `doctor`

```bash
python3 <SKILL_DIR>/scripts/porter.py doctor --json
```

判定规则：

- **退出码 0 且 `blockers` 为空**才算通过。`degradations` 不阻塞，但必须向用户说明
  影响（例如没有硬件编码器、只能用软件编码）。
- `guidance` 是失败项的修复说明：`guidance.<remediation_key>.steps` 是可直接执行的
  安装步骤。**照它补依赖，不要猜包名。**
- `doctor` 检查 Python、ffmpeg/ffprobe、libass、视频编码器、yt-dlp、JS runtime、
  中文字体、输出目录，以及 ASR / 翻译能力。退出码 1 表示存在 blocker。
- `doctor` 只验证**本机能力**，不验证远端 Key 是否有效、额度是否充足或网络是否可达；
  这些问题会在第一次真实调用时暴露。
- 典型 blocker：没有 ffmpeg/libass、没有 yt-dlp、YouTube 缺 JS runtime、输出目录
  不可写。按 `guidance` 修完再重跑 `doctor`。
- `asr_route` 为 `degraded`（既无本地 Whisper 也无 Key）时：**有字幕轨的作业仍可
  做**；无字幕轨的作业需要装 `[asr-local]` 或配置付费 Key。

安装系统依赖或大模型（`[asr-local]` 首次会下载模型）前，**先征得用户同意**；不要
静默修改系统环境。

### 0.3 门二：阶段能力 —— `capabilities`

```bash
python3 <SKILL_DIR>/scripts/porter.py capabilities --json
python3 <SKILL_DIR>/scripts/porter.py capabilities --stage transcript --available --json
```

它回答“这台机器在每个阶段能选哪些**对等**能力”。每项包含：

- `id`：如 `transcript.whisper-local`、`translate.google`
- `available`：本机探测结果（不可用的能力仍会列出，便于区分“未安装”与“不支持”）
- `cost`：`free` / `network` / `paid`
- `egress`：`none` / `text` / `audio`（有多少数据离开本机）
- `download_mb`：首次运行可能要下载的体积
- `requires`：依赖

免费默认：平台字幕轨、本地 Whisper（若安装）、Bing/Google/MyMemory 翻译。
`paid` 能力（`transcript.whisper-api`、`translate.llm`）默认不会被选择。

### 0.4 门三：这次作业的源 —— `probe`

```bash
python3 <SKILL_DIR>/scripts/porter.py probe "<SOURCE>" --json
```

- `kind`：`url` / `local_video` / `subtitle` / `audio`，决定可用的工作流。
- `tracks[].verified=false` 表示平台**列出**了字幕轨，但尚未抓取；计划只能把它当作
  “请求”，不能当作“保证”。
- `warnings` 里的 blocker（如 `SOURCE_UNUSABLE`、`SOURCE_NOT_FOUND`）表示输入不可
  用，**不要继续规划**。
- 需要登录的链接：`--cookies cookies.txt` 或 `--cookies-from-browser chrome`。
- `probe` 只取元数据，不下载媒体；退出码 1 表示源不可用。

### 0.5 完成标准

三道门都通过，并且你能向用户说清：机器缺什么（或没有缺）、每个阶段有哪些可用能力、
这个源是什么类型、有没有已列出的字幕轨。**不要跳过门三直接 `plan`**；`plan` 会重新
探测，但 Agent 应先看到事实并向用户解释。

---

## 1. 工作流模型（Agent 必须理解）

### 1.1 七个阶段

| 阶段 | 做什么 | 典型产物 |
|---|---|---|
| `probe` | 分类源并读取事实 | `source_facts` |
| `acquire` | 下载 URL 媒体，或接入本地文件 | `source_media`、`platform_subtitles` |
| `normalize` | 标准化母版、提取 16 kHz 单声道音轨 | `master_video`、`master_audio` |
| `transcript` | 取平台字幕轨、读入供字幕，或跑 ASR | `source_transcript` |
| `translate` | 句子级翻译为双语/目标语字幕 | `target_transcript` |
| `write` | 写出 SRT/ASS 文件 | `bilingual_srt`、`zh_srt`、`bilingual_ass`、`zh_ass` |
| `render` | 用 libass 烧录硬字幕 | `video_bilingual`、`video_zh` |

阶段产物写到磁盘，失败时已完成的阶段可以复用；`--force` 会忽略缓存。

### 1.2 六种工作流由源事实决定

| 工作流 | 何时选择 | 阶段（默认能力） |
|---|---|---|
| `url.subtitled` | URL 列出源语言字幕轨，或给了 `--subtitle-file` | probe → acquire → normalize → transcript.platform-track → translate → write → render |
| `url.asr` | URL 没有字幕轨，或显式要求 `whisper-local` / `whisper-api` | probe → acquire → normalize → transcript.whisper-local → translate → write → render |
| `local.subtitled` | 本地视频 + `--subtitle-file` | probe → acquire.local → normalize → transcript.supplied → translate → write → render |
| `local.asr` | 本地视频，没有字幕轨 | probe → acquire.local → normalize → transcript.whisper-local → translate → write → render |
| `subtitle.only` | 输入本身是 `.srt` / `.vtt` | probe → transcript.supplied → translate → write（无媒体、无 ffmpeg） |
| `audio.asr` | 输入是音频文件 | probe → acquire.local → normalize → transcript → translate → write（无 render） |

`--task` **截断**工作流，不选择后端：

| `--task` | 停在哪 |
|---|---|
| `probe` | 只探测 |
| `audio` | normalize 之后 |
| `transcribe` | transcript 之后 |
| `subtitle` | write 之后（默认要求字幕，不压制） |
| `release` | render 之后（默认） |
| `convert` | 尚未接入；计划会报 `TASK_NOT_SUPPORTED` |

音频输入请求 `release` 会报 `NO_VIDEO_FOR_RELEASE`；改用 `--task subtitle` 或
`--task transcribe`。

### 1.3 对等能力与“没有回退”

- 计划里的 `stages[]` 每项只有一个 `capability`，不是一个候选列表。
- 运行期只允许**同一能力内部**的有限重试（例如单次网络抖动）；**跨能力替换必须
  重新规划**。
- 失败时 `WorkflowResult.error` 携带 `code`、`message`、`alternatives`、
  `suggested_workflow`。读它，然后用不同参数重新 `plan`。
- 显式 `--transcript whisper-api` / `--translate llm` 是**指名请求**：该能力不可用
  就是 blocker，计划不会静默换成别的。
- 付费策略：`paid` 能力默认排除。显式命名即视为同意；`--allow-paid` 用于允许
  planner 在免费能力不可用时选择付费能力。**先向用户说明费用并取得同意。**

---

## 2. 标准作业循环

### 2.1 规划并保存计划

```bash
mkdir -p .porter-run
python3 <SKILL_DIR>/scripts/porter.py plan "<SOURCE>" --json > .porter-run/plan.json
echo "exit=$?"
```

`plan --json` 输出 `WorkflowPlan`。关键字段：

- `feasible`：**为 `false` 就停下**，不要 run。
- `workflow`、`task`、`source_probe`：源事实与选定的工作流。
- `stages[]`：每阶段的 `id` / `stage` / `capability` / `params` / `needs` / `produces`。
- `issues[]`：`code` / `severity` / `message` / `fix` / `alternatives`。
- `plan_id`：内容哈希；同一计划在不同时间生成应得到相同 id。
- `runtime`：`output_dir`、`target_lang`、`force`、`audio_denoise`。

`feasible=false` 时，按 `issues[].fix` 修环境或换输入，然后重新 plan。

### 2.2 与用户确认

一条作业可能下载几百 MB、跑几分钟到几十分钟。把以下内容报给用户并取得同意：

- 工作流与每阶段选中的能力（尤其是 ASR 与翻译）
- 是否付费（`cost=paid`）、是否会下载模型（`download_mb`）
- 哪些数据会离开本机（`egress=text/audio`）
- 输出目录、压制模式（`--burn`）、预计产物

### 2.3 执行计划

```bash
nohup python3 <SKILL_DIR>/scripts/porter.py run --plan .porter-run/plan.json --json \
  > .porter-run/result.json 2> .porter-run/run.log &
```

- `run --plan` **逐字执行**计划：source、输出目录、目标语言、压制模式、供字幕、
  cookies 都来自 JSON，所以计划本身可复现。
- 运行期只允许覆盖 `-o` 与 `--force`；其他规划参数会被忽略并打印警告。要改能力
  选择，重新 `plan`，不要改运行参数。
- 计划保存了非密钥配置指纹。plan 之后改了 ffmpeg 路径、ASR 模型等配置，执行会被
  `PLAN_CONFIG_CHANGED` 拒绝；重新 plan。
- 简单场景可以直接 `run "<SOURCE>"`，一步完成“解析 + 执行”；但 Agent 应优先
  `plan` → `run --plan`，保留可审计、可复现的计划。

### 2.4 轮询

```bash
python3 <SKILL_DIR>/scripts/porter.py jobs list
python3 <SKILL_DIR>/scripts/porter.py jobs status <job_id>
python3 <SKILL_DIR>/scripts/porter.py jobs cancel <job_id>
```

- 作业注册表跨进程可见；**不要把工具超时调成 1200 秒硬等**，长视频照样会超时且
  拿不到进度。
- 取消是协作式的；取消后仍需检查最终状态与已产出的 artifacts。

### 2.5 读取结果与失败处理

`run --json` 输出 `WorkflowResult`：

- `state`：`done` / `failed` / `cancelled`
- `plan_id`、`workflow`、`task`、`task_dir`
- `stages[]`：`status`（`done` / `reused` / `skipped` / `failed`）、`capability`、
  `outputs`、`error`
- `artifacts`：关键产物路径（`video`、`audio`、字幕、成片）
- `error`：`code` / `message` / `retryable` / `alternatives` / `suggested_workflow`

失败处理顺序：

1. **不要原样重跑同一个 plan** 期待不同引擎。
2. 读 `error.alternatives`，选一个可用能力，显式重新 `plan`。
3. 若 `error.suggested_workflow` 与当前工作流不同，按建议改走另一条工作流。
4. 环境问题回到 `doctor` / `capabilities`；输入问题回到 `probe`。

---

## 3. 场景样例（完整可抄）

### 3.1 URL + 平台字幕轨（最省资源）

```bash
python3 <SKILL_DIR>/scripts/porter.py doctor --json
python3 <SKILL_DIR>/scripts/porter.py probe "https://www.youtube.com/watch?v=..." --json
python3 <SKILL_DIR>/scripts/porter.py plan "https://www.youtube.com/watch?v=..." --burn zh_only --json > plan.json
python3 <SKILL_DIR>/scripts/porter.py run --plan plan.json --json
```

预期：`workflow=url.subtitled`，`transcript.platform-track`，翻译为某个免 Key 端点。
注意：平台字幕是 **requested, not guaranteed**；运行期抓取失败会作为该能力失败报告，
不会自动回退 ASR。此时按 `alternatives` 重新 plan 为 `whisper-local`。

### 3.2 URL 无字幕轨 → 本地 Whisper

```bash
# 环境（需用户同意；首次下载模型约 484 MB）
uv pip install 'porter-workflow[asr-local]'
python3 <SKILL_DIR>/scripts/porter.py doctor --json
python3 <SKILL_DIR>/scripts/porter.py plan "<URL>" --transcript whisper-local --json > plan.json
python3 <SKILL_DIR>/scripts/porter.py run --plan plan.json --json
```

预期：`workflow=url.asr`，`transcript.whisper-local`，`egress=none`。

### 3.3 已有字幕文件 → 只翻译

```bash
python3 <SKILL_DIR>/scripts/porter.py probe ./episode.srt --json
python3 <SKILL_DIR>/scripts/porter.py plan ./episode.srt --task subtitle --translate google --json > plan.json
python3 <SKILL_DIR>/scripts/porter.py run --plan plan.json --json
```

预期：`workflow=subtitle.only`，不下载、不跑 ffmpeg、不跑 ASR；只写 `cooked/` 下的
四个字幕文件。`--target-lang` 可改目标语言。

### 3.4 本地视频 / 音频

```bash
# 本地视频，跑 ASR 后出字幕
python3 <SKILL_DIR>/scripts/porter.py plan ./talk.mp4 --transcript whisper-local --json > plan.json

# 音频输入：没有视频可压制
python3 <SKILL_DIR>/scripts/porter.py plan ./talk.mp3 --task subtitle --transcript whisper-local --json > plan.json
```

音频 + `--task release` 会被拒绝（`NO_VIDEO_FOR_RELEASE`）；`audio.asr` 工作流会在
`write` 后结束。

### 3.5 付费能力（必须显式、必须先征得同意）

```bash
# 显式命名付费 ASR：命名即同意
python3 <SKILL_DIR>/scripts/porter.py plan "<URL>" --transcript whisper-api --json > plan.json

# 允许 planner 在免费能力不可用时选择付费能力
python3 <SKILL_DIR>/scripts/porter.py plan "<URL>" --allow-paid --json > plan.json

# 付费 LLM 翻译
python3 <SKILL_DIR>/scripts/porter.py plan "<URL>" --translate llm --llm-model deepseek-chat --json > plan.json
```

付费前确认 Key 已通过环境变量提供（`WHISPER_API_KEY` / `OPENAI_API_KEY`），并在
计划中核对 `cost=paid` 与 `egress`。

---

## 4. 失败与重新规划矩阵

| 现象（计划或结果） | 含义 | 动作 |
|---|---|---|
| `CAPABILITY_UNAVAILABLE` | 显式请求的能力不在这台机器上 | `capabilities --stage <stage> --available` 看同类；修依赖或重新 plan |
| `PAID_CAPABILITY_NOT_ALLOWED` | 默认策略拒绝付费 | 显式命名该能力或加 `--allow-paid`；先取得用户同意 |
| `NO_CAPABILITY` | 该阶段没有任何可用能力 | 转录：装 `[asr-local]`、配置 Key，或 `--subtitle-file`；翻译：装 `requests` |
| `PLATFORM_TRACK_MISSING` | 源没有列出源语言字幕轨 | `--subtitle-file`，或按 ASR 重新 plan |
| `SUPPLIED_SUBTITLE_MISSING` / `SOURCE_NOT_FOUND` | 指定字幕文件不存在 | 检查路径 |
| `CAPABILITY_WRONG_WORKFLOW` | 在无字幕轨的源上请求平台/供字幕 | 用 ASR，或提供 `--subtitle-file` |
| `CAPABILITY_WRONG_SOURCE` | 在纯字幕输入上请求需要音频的能力 | 去掉该请求，或改传媒体文件 |
| `TASK_NOT_SUPPORTED`（convert） | 字幕格式转换尚未接入 | 暂用 `--task subtitle` |
| `PLAN_CONFIG_CHANGED` | 计划生成后配置变了 | 重新 `plan` |
| 平台字幕抓取失败 / HTTP 429 | 计划内能力失败 | 读 `alternatives`，重新 plan 为 ASR |
| 翻译端点限流 / 返回原文 | 计划内能力失败 | 重新 plan 换另一个免 Key 翻译，或（经同意）用 LLM |
| `ffmpeg` 缺失或无 libass | 环境 blocker | 按 `doctor` 的 `guidance` 安装；或 `--task subtitle` 跳过压制 |
| YouTube 抽取格式异常 | 缺 JS runtime | 按 `doctor` 的 `guidance` 安装 Deno 或 Node ≥ 20 |
| 链接 404 / 私有 / 需要登录 | 源不可用 | 不要重试死链；需要登录时用 cookies |

---

## 5. 产物、缓存与质检

### 5.1 任务目录

```
<output_dir>/<video_id>_<safe_title>/
    raw/      标准化母版、音轨、元数据
    cooked/   字幕与成片
    .tmp/     临时文件，可删除
```

`run --json` 的 `task_dir` 指向该目录；`artifacts` 给出关键文件的绝对路径。常用键：

- `video` / `audio` / `audio_enhanced` / `cover` / `metadata`
- `source_transcript`（`cooked/subtitle.srt`）
- `subtitle_bilingual_srt` / `subtitle_zh_srt` / `subtitle_bilingual_ass` / `subtitle_zh_ass`
- `video_bilingual` / `video_zh`

### 5.2 缓存与来源

ASR 与翻译缓存带有能力来源标记；换能力后不会复用上一能力的文本（`--force` 则完全
忽略缓存）。`stages[].status=reused` 是正常现象，不是错误。

### 5.3 质检（必做）

**不要只看退出码就说完成了。** 至少确认：

```bash
# 1. 中文字幕确实含 CJK（防止“未翻译的假熟肉”）
python3 -c "import pathlib,sys; t=pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'); sys.exit(0 if any('一' <= c <= '鿿' for c in t) else 1)" \
  "<task_dir>/cooked/subtitle_zh.srt" && echo "OK: 含中文" || echo "FAIL: 无中文"

# 2. 成片可解码、时长合理（防止发布截断文件）
ffprobe -v error -show_entries format=duration -of csv=p=0 "<task_dir>/cooked/video_zh.mp4"

# 3. 计划与结果一致
#    result.json 的 plan_id 应等于 plan.json 的 plan_id
```

- 成片大小异常小通常意味着压制失败，而不是视频短。
- 检查 `cooked/` 里是否出现计划声明的产物（`produces`）。
- 质检不通过就报告具体文件与命令输出，不要用“应该没问题”带过。

### 5.4 交付

向用户报告：任务目录、`cooked/` 下的成片与字幕、实际使用的工作流与每阶段能力、
任何 `reused` 阶段或降级（如软件编码），以及付费能力是否被使用。

---

## 6. 安全约束

- **API Key 只放环境变量**：`WHISPER_API_KEY` / `OPENAI_API_KEY`。不要写进仓库、
  技能目录、命令行参数或对话记录。`porter config list` 会打码。
- **cookies 只传路径**，不要打印文件内容；不要把 cookie 值贴进对话。
- **付费前取得同意**；`--allow-paid` 不是免费通行证。
- **不要静默修改系统环境**：安装 FFmpeg、JS runtime、`[asr-local]` 或下载模型前
  先说明并征得同意。
- 受限桌面宿主：先读 [`references/HOSTS.md`](references/HOSTS.md)，设置
  `PORTER_SKILL_STATE_DIR` 并运行 `--host-check`；不要为了省事关掉沙盒或默认提权。
- 包装器默认安装基础包；需要免费 ASR 时设置
  `PORTER_SKILL_SPEC='porter-workflow[asr-local]'`，需要付费能力时再加 `[llm]`。
  同一会话里的 `plan` / `run` / `jobs status` 必须使用同一个 spec。

---

## 7. 参考文件

| 文件 | 何时读 |
|---|---|
| [`references/SETUP.md`](references/SETUP.md) | 三道门的完整清单、安装决策、受限宿主 |
| [`references/PLAN.md`](references/PLAN.md) | `WorkflowPlan` / `WorkflowResult` 字段、问题码、能力表 |
| [`references/CONFIG.md`](references/CONFIG.md) | 全部配置键、环境变量、优先级 |
| [`references/ARCHITECTURE.md`](references/ARCHITECTURE.md) | 七阶段、六工作流、解析器/执行器、分层 |
| [`references/MCP.md`](references/MCP.md) | MCP 工具、`plan`/`job_start(plan=...)` 契约与安全约束 |
| [`references/WEBUI.md`](references/WEBUI.md) | WebUI 的计划解析、执行与人工协作 |
| [`references/HOSTS.md`](references/HOSTS.md) | Windows 原生入口、受限宿主、工作区状态目录 |
| [`assets/config.example.json`](assets/config.example.json) | 配置模板 |

---

## 8. 前端状态：三个入口，一个计划模型

- **CLI 是权威入口**，`plan --json` → `run --plan` 是可复制、可审阅的标准契约。
- **MCP**：`porter_capabilities` / `porter_probe` / `porter_plan` /
  `porter_job_start(plan=...)` / `porter_job_result` 使用同一份
  `WorkflowPlan` / `WorkflowResult`。`porter_job_start` 拒绝同时传入 `plan` 与
  规划参数，也拒绝不可行计划。详见 [`references/MCP.md`](references/MCP.md)。
- **WebUI**：工作台先“预检并生成计划”，展示计划 JSON，再执行同一份计划；规划选项
  变化会使旧计划失效。详见 [`references/WEBUI.md`](references/WEBUI.md)。
- 三个入口都不做运行期跨能力回退；失败时用 `alternatives` /
  `suggested_workflow` 重新 `plan`。
- `npx skills add` 只安装技能文件，**不会**安装 MCP server；全新安装的默认路径
  必须是 CLI。
