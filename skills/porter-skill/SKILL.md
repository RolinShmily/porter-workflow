---
name: porter-skill
description: >
  Automated video localization SOP for YouTube, X/Twitter, Instagram, TikTok and
  Bilibili. Guides an agent through environment checks, configuration validation,
  inspect → plan → confirm → run → quality check, using the porter CLI or a
  configured MCP server, with the WebUI as an interactive human path. Downloads a
  video, produces bilingual or Chinese-only subtitles, and burns a ready-to-publish
  release video. Transcription can use key-free local Whisper with the [asr-local]
  extra, a Whisper API key, or the VideoCaptioner CLI; platform subtitle tracks
  skip recognition entirely. Use whenever the user asks to download a video,
  generate or translate subtitles, transcribe audio, make a Chinese-subtitled
  (熟肉) version, or localize streaming media.
license: MIT
compatibility: >
  Python >=3.11, uv, FFmpeg with libass, and Deno (or Node >=20) for YouTube.
  Local ASR is optional via the [asr-local] extra: key-free, offline after the
  model downloads. Otherwise transcription needs OPENAI_API_KEY or
  WHISPER_API_KEY, or the videocaptioner CLI. Translation needs no key. MCP and
  WebUI are optional frontends.
metadata:
  homepage: https://github.com/RolinShmily/porter-workflow
  version: "0.3.2"
---

# Porter Skill（流媒体搬运工 SOP）

本技能是**标准作业流程（SOP）**，不是参数大全。按顺序执行，每一步达到“完成标准”
再进入下一步。四个阶段依次是 **获取 → 转录 → 翻译 → 压制**；每个阶段把产物写到
磁盘，所以中途失败不会丢掉已完成的部分。

**先选执行通道，再做环境与配置检查，最后才开作业。** 长任务绝不用前台调用硬等。

---

## 0. 先选执行通道

| 通道 | 何时用 | 入口 | 长任务 |
|---|---|---|---|
| **MCP**（优先） | 宿主已配置 porter MCP，能看到 `porter_*` 工具 | `porter_inspect` / `porter_job_start` 等 | `porter_job_start` 立即返回，`porter_job_status` 轮询 |
| **CLI**（默认） | 全新安装、没有 MCP、或用户要求命令行 | `scripts/porter.sh` / `scripts/porter.ps1` | 后台启动 + `jobs status` 轮询 |
| **WebUI** | 用户要可视化、人工确认、浏览产物 | `porter-web` | 页面自带状态刷新 |

- **MCP 已配置时优先用 MCP**：结构化结果、没有 shell 转义/编码问题，并且
  `sampling` 能用宿主模型翻译（CLI 原理上做不到）。
- **没有 MCP 时用 CLI**。`npx skills add` 只安装技能文件，**不会**安装 MCP server，
  所以默认路径必须是 CLI；不要把 MCP 当成全新安装就能用的入口。
- **WebUI 是人工通道**：由用户点击和确认。Agent 负责启动、引导和解释，不要假装
  自己能操作页面。具体见 [`references/WEBUI.md`](references/WEBUI.md)。

三条通道共用下面的环境检查、配置检查和判断标准；只是“调用哪个入口”不同。

---

## 1. 环境与依赖：先确认，再开工

### 1.1 入口可用

```bash
# CLI
uv --version && uvx --version
<SKILL_DIR>/scripts/porter.sh --version
```

- PowerShell 用原生入口：`& '<SKILL_DIR>\scripts\porter.ps1' --version`。
- MCP 宿主直接调用 `porter_version`。
- WebUI：`uvx --from "porter-workflow[web]" porter-web --help`。

### 1.2 系统依赖

| 依赖 | 用途 | 确认方式 |
|---|---|---|
| **FFmpeg + ffprobe，带 libass** | 所有阶段；libass 用于烧硬字幕 | `ffmpeg -filters \| grep subtitles` |
| **Deno**（推荐）或 Node ≥ 20 | YouTube 的 JS 挑战 | `porter doctor` 的 JS runtime 检查 |
| **字体** | ASS 字幕渲染 | `porter doctor` 的 font 检查 |

### 1.3 Python 能力（extras）

| 能力 | Extra | 说明 |
|---|---|---|
| 预检、规划、平台字幕、翻译、压制 | 基础包 | 不需要额外安装 |
| API ASR + LLM 翻译 | `[llm]` | 需要 `OPENAI_API_KEY` / `WHISPER_API_KEY`；技能包装器默认装这个 |
| **本地 ASR（推荐）** | `[asr-local]` | `faster-whisper`，**免 Key，首次下载模型后可离线**；体积较大 |
| MCP server | `[mcp]` | `porter-mcp` |
| WebUI | `[web]` | Gradio 工作台 |

选择原则：**没有 API Key 时，转录必须装 `[asr-local]`，否则只能依赖平台字幕轨。**
安装大依赖前先征得用户同意。

```bash
# 技能包装器默认：porter-workflow[llm]
# 免 Key 本地转录：经用户同意后设置
export PORTER_SKILL_SPEC='porter-workflow[llm,asr-local]'
```

### 1.4 安装动作（需用户同意）

- 缺 `uv`：按 uv 官方安装方式安装；不要静默改系统环境。
- 缺 FFmpeg/libass 或 Deno：按 `porter doctor` 给出的平台命令安装；不要静默改系统环境。
- 受限桌面宿主：先读 [`references/HOSTS.md`](references/HOSTS.md)，设置
  `PORTER_SKILL_STATE_DIR` 并跑 `--host-check`；**不要**为了省事关闭沙盒或默认提权。

### 1.5 完成标准

```bash
<SKILL_DIR>/scripts/porter.sh doctor --json
```

- 退出码为 0，且没有 `blocker`；`degraded` 可以接受，但要向用户说明影响。
- MCP 路径调用 `porter_doctor` 得到同样的结论。
- **没有可用 ASR 且视频没有平台字幕时，作业会在 transcribe 阶段以
  `every speech-to-text backend failed` 结束。** 这时补 `[asr-local]`、配置
  `OPENAI_API_KEY`，或安装 VideoCaptioner CLI，不要直接重试。

---

## 2. 配置检查：Key、路由、输出

```bash
<SKILL_DIR>/scripts/porter.sh config path
<SKILL_DIR>/scripts/porter.sh config list    # source + 生效值，密钥已打码
```

MCP 路径调用 `porter_config`（只读，密钥打码）。

### 2.1 转录会走哪条路

| 顺序 | 路由 | 前提 | 备注 |
|---|---|---|---|
| 1 | 平台字幕轨 | 平台有原生字幕 | 免费、准确；但 **requested, not guaranteed**，抓取失败会回退 ASR |
| 2 | `whisper-local` | 安装 `[asr-local]` | 免 Key、可离线；推荐 |
| 3 | `whisper-api` | Key（`WHISPER_API_KEY` 或 `OPENAI_API_KEY`） | OpenAI 兼容端点 |
| 4 | `videocaptioner` | 用户自行安装的外部 CLI | GPL-3.0，`python<3.13` |

`--asr-engine` / `asr.engine` 只是把指定后端**提到最前**，其余仍作回退。

### 2.2 翻译与输出

- **翻译不需要 Key**：`llm` 之后还有 `bing`、`google`、`mymemory`；LLM 只是质量更高。
- 目标语言默认 `zh-Hans`；用 `--target-lang` 覆盖。
- 输出目录默认 `./porter_output`；受限宿主必须显式 `-o` 到获准路径。
- 需要登录的视频用 `--cookies-from-browser` 或 `--cookies`（CLI 专属；MCP 从配置读取）。

### 2.3 Key 的安全放置

- 优先环境变量：`OPENAI_API_KEY`、`WHISPER_API_KEY`。
- 其次 `porter config set llm.api_key=...`（默认写用户级配置）。
- **不要把 Key 写进仓库、技能目录或对话记录。** MCP 的 `porter_config` 故意只读，
  就是为了避免 Key 进入对话与遥测。

完整键表见 [`references/CONFIG.md`](references/CONFIG.md)，模板见
[`assets/config.example.json`](assets/config.example.json)。

**完成标准**：知道实际配置文件路径、ASR 路由、翻译是否会跑、输出目录是否可写。

---

## 3. 标准作业流程

### 3.1 预检 —— `inspect`

```bash
<SKILL_DIR>/scripts/inspect.sh "<URL>" --json
# 或
<SKILL_DIR>/scripts/porter.sh inspect "<URL>" --json
# MCP: porter_inspect
```

返回平台、标题、时长、分辨率、画幅、是否有字幕轨。**下载任何媒体之前完成**。

- `is_valid` 才是“链接可用”；`ok` 只是“调用成功”。
- `is_valid=false` 时**不要重试**死链；把错误原因报给用户。
- 退出码 1 表示链接不可用。

### 3.2 规划 —— `plan`

```bash
<SKILL_DIR>/scripts/porter.sh plan "<URL>" --json
# MCP: porter_plan
```

这是**最省时间的一步**，它回答“这条视频会怎么走、能不能走通”：

- `subtitles.route`：`platform` 还是 `asr`
- `translation.needed`：已有中文轨时为 `false`，可整段跳过翻译
- `feasible` / `blocking_issues`：**为 `false` 就停下**，别开作业
- `notes`：**必读**。最重要的两条：
  - 平台字幕是 **requested, not guaranteed**——抓取可能失败（平台限流，HTTP 429）
    并回退到语音识别
  - 哪些后端实际可用、是否已安装本地 ASR / 是否配置了 Key

退出码 1 表示不可行。

### 3.3 与用户确认

一条作业可能下载几百 MB、跑几分钟到几十分钟。**把 `plan` 的结论报给用户并取得
同意再开跑**：

- 走平台字幕还是 ASR；ASR 用哪个后端
- 会执行哪些阶段、是否压制（`--burn`）
- 输出目录、预计磁盘占用、是否会产生两种成片

若 `feasible=false`，报 `blocking_issues`，**不要开作业**。

### 3.4 执行

#### CLI 路径

```bash
nohup <SKILL_DIR>/scripts/porter.sh run "<URL>" -o ./porter_output > run.log 2>&1 &
# 轮询（作业记录写在共享的注册表文件里，跨进程可见）
<SKILL_DIR>/scripts/porter.sh jobs list
<SKILL_DIR>/scripts/porter.sh jobs status <job_id>
<SKILL_DIR>/scripts/porter.sh jobs cancel <job_id>
```

PowerShell 用宿主提供的**后台作业工具**异步启动
`& '<SKILL_DIR>\scripts\porter.ps1' run ...`，并用同一个
`PORTER_SKILL_STATE_DIR` 轮询 `jobs status`。不要用 `Out-String` 把耗时命令的进度
攒到结束才输出。

> **不要**把工具超时设成 1200 秒来硬扛长任务（v0.1 的做法）：视频更长时照样失败，
> 而且失败时拿不到任何进度。作业注册表就是为长任务设计的。

#### MCP 路径

| 步骤 | 工具 |
|---|---|
| 启动 | `porter_job_start`（立即返回 job id） |
| 查询 | `porter_job_status` |
| 取产物 | `porter_job_result` |
| 取消 | `porter_job_cancel` |
| 列表 | `porter_job_list` |

工具签名以 MCP 自带 schema 为准，本技能不重复抄写。完整对照见
[`references/MCP.md`](references/MCP.md)。

#### WebUI 路径

```bash
uvx --from "porter-workflow[web]" porter-web
```

默认 `127.0.0.1:7860`。让用户在工作台页填写链接、选择压制模式和 ASR 引擎，点击
开始；在“任务总览”页查看进度和物料。Agent 不能替用户点击，但应告诉用户去哪个
Tab、看哪一列。操作细节见 [`references/WEBUI.md`](references/WEBUI.md)。

### 3.5 监控与取消

- CLI：`jobs list` / `jobs status`；MCP：`porter_job_status`；WebUI：任务总览页。
- 状态 `AWAITING_USER` 表示作业在等人工输入；按 WebUI 的提示提交 action/payload。
- 取消是**协作式**的：CLI `jobs cancel`、MCP `porter_job_cancel`、WebUI 取消按钮。
  取消后仍要检查最终状态与产物。

### 3.6 质检（必做）

**不要只看退出码就说完成了。** 至少确认：

```bash
# 1. 中文字幕确实存在且含 CJK（防止“未翻译的假熟肉”）
grep -qP '[\x{4e00}-\x{9fff}]' ./porter_output/<task>/cooked/subtitle_zh.srt \
  && echo "OK: 含中文" || echo "FAIL: 无中文"

# 2. 成片可解码且 moov atom 完好（防止发布截断文件）
ffprobe -v error -show_entries format=duration -of csv=p=0 ./porter_output/<task>/cooked/video_zh.mp4
```

- 成片大小异常小通常意味着压制失败，而不是视频短。
- 检查 `cooked/` 里是否出现预期的 `video_zh.mp4` / `video_bilingual.mp4` 和对应字幕。
- 质检不通过就报告具体文件与命令输出，不要用“应该没问题”带过。

### 3.7 交付

向用户报告：

- 任务目录（`<output_dir>/<video_id>_<safe_title>/`）
- `cooked/` 下的成片与字幕路径
- 实际使用的 ASR/翻译路由，以及任何降级（如平台字幕抓取失败后回退 ASR）
- WebUI 用户可直接在“下载”区取 `cooked.zip` / `raw.zip`

---

## 4. 常用参数

```bash
run "<URL>" --burn skip          # 只要字幕，不压制
run "<URL>" --burn zh_only       # 只要纯中文成片
run "<URL>" --burn bilingual_only
run "<URL>" -o /path/to/out
run "<URL>" --target-lang zh-Hans
run "<URL>" --translator google  # 提到最前，其余仍回退
run "<URL>" --asr-engine whisper-api
run "<URL>" --llm-model deepseek-reasoner
run "<URL>" --subtitle-file ./my_subtitles.srt   # 已有源字幕，跳过识别
run "<URL>" --cookies-from-browser chrome
run "<URL>" --cookies ./cookies.txt
run "<URL>" --force              # 忽略已缓存阶段结果
run "<URL>" --json               # 供脚本消费
```

`--only-phase` 的语义是**“到此阶段为止”**，不是“只跑这个阶段”——更早的阶段仍会执行。

---

## 5. 常见失败与处理

| 现象 | 处理 |
|---|---|
| `ffmpeg` 缺失或没有 libass | 安装带 libass 的 FFmpeg（`porter doctor` 会给出平台命令）；用 `ffmpeg -filters \| grep subtitles` 确认 |
| YouTube 下载报 JS challenge / 需要 PO token | 安装 Deno（推荐）或 Node ≥ 20；重跑 `doctor` |
| transcribe 阶段 `every speech-to-text backend failed` | 装 `[asr-local]`、配置 Key，或安装 VideoCaptioner CLI；先 `plan` 确认路由 |
| 翻译返回英文 / 原样文本 | 免 Key 端点被限流或被反爬；换 `--translator` 或配置 LLM Key |
| 平台字幕抓取失败 / HTTP 429 | 设计内回退到 ASR；等待后重试或改用本地 ASR |
| 链接 404 / 私有 / 需要登录 | `is_valid=false`，不要重试死链；需要登录时用 cookies |
| 长任务超时 | 用作业注册表/`porter_job_*` 轮询，不要把超时调成 1200 秒 |
| 沙盒写目录失败 | 读 [`references/HOSTS.md`](references/HOSTS.md)，设 `PORTER_SKILL_STATE_DIR` 并 `--host-check` |

---

## 6. 参考文件

| 文件 | 何时读 |
|---|---|
| [`references/SETUP.md`](references/SETUP.md) | 安装依赖、验证环境、检查配置的完整清单 |
| [`references/WEBUI.md`](references/WEBUI.md) | WebUI 各 Tab 的可操作项与 HITL 流程 |
| [`references/CONFIG.md`](references/CONFIG.md) | 全部配置键、环境变量、优先级 |
| [`references/MCP.md`](references/MCP.md) | MCP 工具对照、配置方法、安全约束 |
| [`references/HOSTS.md`](references/HOSTS.md) | Windows 原生入口、受限宿主、工作区状态目录 |
| [`references/ARCHITECTURE.md`](references/ARCHITECTURE.md) | 四阶段管线、平台与后端回退链、设计约束 |
| [`assets/config.example.json`](assets/config.example.json) | 配置模板 |
