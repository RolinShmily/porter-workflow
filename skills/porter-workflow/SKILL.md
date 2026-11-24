---
name: porter-workflow
description: >
  Automated video localization SOP for YouTube, Bilibili, TikTok, X/Twitter,
  Instagram, and local video/audio/subtitle files. Drives the published
  `porter-workflow` Python engine directly through a self-contained JSON script
  (`scripts/porter_cli.py`): doctor → capabilities → probe → plan → confirm →
  run --plan → poll → quality check. Single capability per stage with no runtime
  fallback; includes free offline local Whisper, target video quality with
  graceful degradation, key-free translation, and FFmpeg/libass hardsubbing.
  Use whenever the user asks to download a video, transcribe audio, generate or
  translate subtitles, produce a Chinese-subtitled (熟肉) release video, or
  localize local media.
license: MIT
compatibility: >
  Python >=3.8 for the skill wrapper; uv provisions Python 3.11–3.13 and
  porter-workflow>=0.4.0 automatically. Requires FFmpeg + ffprobe with libass,
  and Deno (or Node >=20) for YouTube. Local Whisper (faster-whisper), LLM
  translation adapters, and key-free translators are all bundled in
  porter-workflow; only the optional Gradio WebUI uses porter-workflow[web].
metadata:
  homepage: https://github.com/RolinShmily/porter-workflow
  version: "0.4.0"
---

# Porter Workflow（视频本地化 SOP）

本技能直接调用 PyPI 上发布的 `porter-workflow` 核心引擎（`import porter`），通过
单一内置脚本 `scripts/porter_cli.py` 提供纯 JSON 接口。按顺序推进，每一步达到完成
标准后再进入下一步：

```text
环境三道门 (doctor → capabilities → probe) → plan → 用户确认 → run --plan → 轮询 → 质检
```

**核心不变量：**
- **无运行期跨能力回退**：每个阶段只选定恰好一个能力；失败时读取 `error.alternatives`
  与 `error.suggested_workflow`，显式重新 `plan`，不要原样重跑。
- **开箱即用**：`porter-workflow>=0.4.0` 已内置 `yt-dlp`、免费离线本地 Whisper
  （`faster-whisper`）、免 Key 翻译（Bing / Google / MyMemory）和付费 LLM/Whisper API
  适配器；无需配置各种 extras。
- **目标画质基准**：默认 `1080p`（按短边分辨率排序，横竖屏统一），达不到时平滑降级
  并发出 `QUALITY_DEGRADED` warning。

下文命令写作 `python3 <SKILL_DIR>/scripts/porter_cli.py`；Windows PowerShell 将
`python3` 替换为 `py -3`、斜杠换为 `\`。所有命令 stdout 均为纯 JSON，日志在 stderr。

---

## 1. 环境三道门（开工前必做）

### 1.0 前置检查

```bash
python3 --version   # Windows: py -3 --version
uv --version
```

- 缺少 `uv` 时按官方方式引导用户安装（不要静默改系统环境）。
- 受限桌面宿主先读 [`references/HOSTS.md`](references/HOSTS.md)，设好
  `PORTER_SKILL_STATE_DIR` 后执行 `--host-check`。

### 1.1 门一：机器健康 —— `doctor`

```bash
python3 <SKILL_DIR>/scripts/porter_cli.py doctor
```

- 退出码 `0` 且 `blockers` 为空即通过；`degradations` 不阻塞但需告知用户（如无硬件
  编码器将走软件编码）。
- 缺少系统依赖（FFmpeg/libass、Deno/Node、中文字体）时，严格按输出里的 `guidance`
  步骤征得用户同意后安装，再重跑 `doctor`。

### 1.2 门二：阶段能力 —— `capabilities`

```bash
python3 <SKILL_DIR>/scripts/porter_cli.py capabilities
python3 <SKILL_DIR>/scripts/porter_cli.py capabilities --stage transcript --available
```

- 查看各阶段对等能力的 `available`、`cost`（`free`/`network`/`paid`）与 `egress`
  （`none`/`text`/`audio`）。
- 付费能力（`transcript.whisper-api`、`translate.llm`）默认排除，仅显式命名或传
  `--allow-paid` 且有 Key 时才会被选中。

### 1.3 门三：源探测 —— `probe`

```bash
python3 <SKILL_DIR>/scripts/porter_cli.py probe "<SOURCE>"
```

- 返回 `kind`（`url`/`local_video`/`subtitle`/`audio`）、平台、分辨率、时长、列出
  的字幕轨（`tracks[].verified=false` 表示平台列出但未抓取，**requested, not guaranteed**）。
- 退出码 `1` 或 `warnings` 含 `blocker`（如 `SOURCE_UNUSABLE`/`SOURCE_NOT_FOUND`）
  时直接报告错误，不要继续 `plan`。需要登录态的链接可传 `--cookies` 或
  `--cookies-from-browser`。

---

## 2. 工作流模型速览

七阶段：`probe` → `acquire` → `normalize` → `transcript` → `translate` → `write` → `render`。

| 工作流 | 触发条件 | 默认阶段路径 |
|---|---|---|
| `url.subtitled` | URL 列出源语言字幕轨或提供 `--subtitle-file` | probe → acquire → normalize → transcript.platform-track → translate → write → render |
| `url.asr` | URL 无字幕轨或显式指定 Whisper | probe → acquire → normalize → transcript.whisper-local → translate → write → render |
| `local.subtitled` | 本地视频 + `--subtitle-file` | probe → acquire.local → normalize → transcript.supplied → translate → write → render |
| `local.asr` | 本地视频无字幕轨 | probe → acquire.local → normalize → transcript.whisper-local → translate → write → render |
| `subtitle.only` | `.srt` / `.vtt` 输入 | probe → transcript.supplied → translate → write（无 ffmpeg） |
| `audio.asr` | 音频输入 | probe → acquire.local → normalize → transcript → translate → write（无 render） |

`--task` 截断终点：`probe` / `audio` / `transcribe` / `subtitle` / `release`（默认）。
音频输入请求 `release` 会报 `NO_VIDEO_FOR_RELEASE`，改用 `--task subtitle` 或 `transcribe`。
完整字段与问题码见 [`references/PLAN.md`](references/PLAN.md)。

---

## 3. 标准作业循环

### 3.1 规划并保存计划

```bash
python3 <SKILL_DIR>/scripts/porter_cli.py plan "<SOURCE>" \
  --quality 1080p --burn dual -o ./porter_output --out .porter-run/plan.json
```

- 使用 `--out` 直接写 UTF-8 JSON（避免 Windows PowerShell `>` 重定向产生 UTF-16 BOM）。
- 核对输出中的：
  - `feasible`：为 `false` 立即停下，按 `issues[].fix` 解决后再重新 `plan`。
  - `stages[]`：每阶段唯一能力、是否付费（`cost=paid`）、数据出口（`egress`）。
  - `issues[]` warning：如 `QUALITY_DEGRADED`（源片或未登录清晰度低于目标画质）、
    `TRACKS_UNVERIFIED`、`TRANSLATION_SKIPPED`（仅人工目标语轨会跳过翻译）。

### 3.2 与用户确认

下载与压制可能耗时数分钟并占用磁盘空间，开跑前向用户简明确认：
- 工作流类型、目标画质与实测分辨率（若降级说明原因）
- 转录与翻译能力（是否使用付费能力、是否首次下载 ~484 MB Whisper 模型）
- 压制模式（`dual` / `zh_only` / `bilingual_only` / `skip`）与输出目录

### 3.3 后台执行与轮询

长任务必须**异步后台启动**并轮询作业注册表，禁止把工具超时硬拉长等待：

```bash
nohup python3 <SKILL_DIR>/scripts/porter_cli.py run \
  --plan .porter-run/plan.json --out .porter-run/result.json \
  > /dev/null 2> .porter-run/run.log &

# 轮询与取消（共享 JobRegistry，跨进程可见）
python3 <SKILL_DIR>/scripts/porter_cli.py jobs list --all
python3 <SKILL_DIR>/scripts/porter_cli.py jobs status <job_id>
python3 <SKILL_DIR>/scripts/porter_cli.py jobs cancel <job_id>
```

PowerShell 请使用宿主后台任务工具异步运行，并保持相同的 `PORTER_SKILL_STATE_DIR`。

### 3.4 失败重规划

读取 `result.json` 的 `error`：
1. `TARGET_TRACK_MISSING`：平台人工目标语轨未拉到 → 用 `--force-translate` 或指定
   `--translate google` 重新 `plan`。
2. 翻译能力限流/网络失败（如 `translate.bing` 失败）→ 读取 `error.alternatives`
   （如 `translate.google` / `translate.mymemory`），显式重新 `plan`。
3. 平台源字幕轨抓取失败 → 按 `error.suggested_workflow`（`url.asr`）加
   `--transcript whisper-local` 重新 `plan`。
4. `PLAN_CONFIG_CHANGED` → 计划生成后配置变更，重新 `plan`。

---

## 4. 质检与交付（必做）

不要仅凭退出码 `0` 宣布完成，至少验证三项：

```bash
# 1. 中文字幕确实含中文字符（防止未翻译假熟肉）
python3 -c "import pathlib,sys; t=pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'); sys.exit(0 if any('\u4e00'<=c<='\u9fff' for c in t) else 1)" \
  "<task_dir>/cooked/subtitle_zh.srt"

# 2. 成片可解码且时长合理（存在压制阶段时）
ffprobe -v error -show_entries format=duration -of csv=p=0 "<task_dir>/cooked/video_zh.mp4"

# 3. result.json 的 plan_id 与 plan.json 一致
```

交付时向用户汇报：任务目录（`task_dir`）、`cooked/` 成片与字幕路径、实际使用的工作流
与各阶段能力、实际画质以及是否使用了付费能力。

---

## 5. 可选：启动人类 WebUI 工作台

如果用户希望可视化操作、在线预览成片或调整配置表单，可引导启动 WebUI（会按需拉取
`porter-workflow[web]`）：

```bash
python3 <SKILL_DIR>/scripts/porter_cli.py webui --port 7860
```

---

## 6. 安全约束与配置

- **密钥保护**：API Key 优先走环境变量（`OPENAI_API_KEY` / `WHISPER_API_KEY`），
  或用 `config set` 写入用户配置；`config show` 已脱敏，严禁读取或打印明文密钥与
  cookies 文件内容。
- **付费授权**：使用 `whisper-api` / `translate.llm` 或 `--allow-paid` 前必须先说明
  费用并获得用户明确同意。
- **本地开发覆盖**：调试仓库源码时可设 `PORTER_SKILL_SPEC=.` 让脚本使用当前目录代码。

参考文档：
- [`references/PLAN.md`](references/PLAN.md) — `WorkflowPlan` / `WorkflowResult` 字段、能力表与问题码
- [`references/CONFIG.md`](references/CONFIG.md) — 配置键、优先级与环境变量
- [`references/HOSTS.md`](references/HOSTS.md) — 受限宿主、Windows 与 `PORTER_SKILL_STATE_DIR`
