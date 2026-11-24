# porter 架构

本文说明 porter 的内部结构，供需要判断“为什么这么设计”或“哪里可能出问题”时阅读。
日常使用只需 [`SKILL.md`](../SKILL.md)。

---

## 1. 七阶段与六工作流

```
probe → acquire → normalize → transcript → translate → write → render
```

| 阶段 | 做什么 | 产物 |
|---|---|---|
| `probe` | 分类源并读取事实 | `source_facts` |
| `acquire` | 下载 URL 媒体，或接入本地文件 | `source_media`、`platform_subtitles` |
| `normalize` | 标准化母版、提取 16 kHz 单声道音轨 | `master_video`、`master_audio` |
| `transcript` | 平台字幕轨、供字幕，或语音识别 | `source_transcript` |
| `translate` | 句子级翻译、碎片重构 | `target_transcript` |
| `write` | 写双语/目标语 SRT 与 ASS | `bilingual_srt` 等四个文件 |
| `render` | 用 libass 烧录硬字幕 | `video_bilingual`、`video_zh` |

工作流由**源事实**决定，不是由平台名字硬编码：

| 工作流 | 条件 |
|---|---|
| `url.subtitled` | URL 列出源语言字幕轨，或给了 `--subtitle-file` |
| `url.asr` | URL 无字幕轨，或显式要求 ASR |
| `local.subtitled` | 本地视频 + `--subtitle-file` |
| `local.asr` | 本地视频无字幕轨 |
| `subtitle.only` | 输入是字幕文件 |
| `audio.asr` | 输入是音频 |

`--task` 截断工作流（probe / audio / transcribe / subtitle / release），不选择后端。
`convert` 尚未接入执行器，计划会明确报 `TASK_NOT_SUPPORTED`。

---

## 2. 能力是对等的，不是回退链

`capabilities.py` 定义 18 个能力，每个能力是**一个阶段的一种实现**：

- probe：`probe.ytdlp` / `probe.ffprobe` / `probe.subtitle`
- acquire：`acquire.ytdlp-video` / `acquire.ytdlp-audio` / `acquire.ytdlp-subs` / `acquire.local`
- normalize：`normalize.ffmpeg`
- transcript：`transcript.platform-track` / `transcript.supplied` / `transcript.whisper-local` / `transcript.whisper-api`
- translate：`translate.bing` / `translate.google` / `translate.mymemory` / `translate.llm`
- write：`write.subtitles`
- render：`render.ffmpeg-libass`

能力记录 `available`、`cost`（free/network/paid）、`egress`（none/text/audio）、
`download_mb`、`requires`。**没有顺序含义**；`_DEFAULT_CANDIDATES` 只是免费优先的
偏好顺序，解析器从中选一个。

### 付费策略

- `paid` 能力默认被排除：环境里有 Key 是配置，不是同意付费。
- 显式命名（`--transcript whisper-api`、`--translate llm`）即视为同意。
- `--allow-paid` 允许解析器在免费能力不可用时选择付费能力。

### 没有运行期回退

`executor.py` 用计划选中的**单一能力**构造 `AsrChain` / `TranslationChain`：

- 平台字幕：`allow_platform=True`，其余 `False`
- 供字幕：`allow_supplied=True`，其余 `False`
- 本地 Whisper：只有 `[WhisperLocalBackend()]`，`expected_origin_prefix="faster-whisper:"`
- Whisper API：只有 `[WhisperApiBackend()]`，`expected_origin_prefix="whisper:"`

缓存恢复时校验来源：上一能力产生的字幕不会被新能力复用。若能力失败，结果里返回
`alternatives` 与 `suggested_workflow`，由 Agent 重新 plan。

---

## 3. 解析器与执行器

### 解析器（`resolver.py`）

1. `probe_source()` 读取源事实。
2. `_choose_workflow()` 按源类型与显式转录请求选工作流。
3. 为每个阶段选**恰好一个**能力，并检查可用性/付费策略。
4. 生成 `WorkflowPlan`，内容哈希封存 `plan_id`；有 blocker 时 `feasible=false`。

同一实现会生成 `PlanIssue`，包含 `code`、`message`、`fix`、`alternatives`、
`suggested_workflow`。计划即使不可行也保留“如果修好会怎么跑”的阶段列表，便于向
用户展示具体修复。

### 执行器（`executor.py`）

1. 预检：`feasible` 与配置指纹。指纹不一致直接 `PLAN_CONFIG_CHANGED`。
2. 把计划映射为 `JobOptions`；`--plan` 的 source/输出/语言/压制/字幕/cookies 都来自
   计划。
3. 按阶段分组为事件 Phase：PREPARE（acquire+normalize）、TRANSCRIBE、TRANSLATE
   （translate+write）、BURN（render）。
4. 字幕-only 工作流不构造媒体管线：读文件 → 翻译 → 写四个字幕产物。
5. 失败返回 `WorkflowResult.error`，不抛出跨能力重试。

`plan` 是契约，`run --plan` 逐字执行；运行期只允许覆盖 `-o` 与 `--force`。

---

## 4. 句子级翻译与碎片重构

ASR 和平台字幕常常给出**滚动碎片**（YouTube 自动字幕每 2–3 个词一条）。直接逐条
翻译会得到语序颠倒的碎词。翻译阶段做三件事：

1. **合并短碎片**——把过短的相邻 cue 合成可翻译的完整句
2. **重构句子**——按语法、标点与静音间隙切分
3. **按中文意群重新断句**——单行 ≤20 字，时间戳按字数比例平滑分配

**副作用（重要）**：对**已经是句级切分**的字幕文件，合并仍会发生——实测 3 条输入
可能变成 2 条输出。合并后的 cue 时间跨度正确覆盖原 cue，所以行为正确但边界变了。
结果会如实报告 `input_cue_count` / `cue_count` / `cues_merged`。

新执行器固定使用 `PassthroughRefiner`：计划没有选择 LLM 精修能力，就不会发起隐藏的
付费调用。

---

## 5. 媒体层

所有 ffmpeg/ffprobe 调用收在一个入口（`media/ffmpeg.py`），统一应用：

- `-nostdin`（否则 ffmpeg 会偷走 stdin）
- stderr 只保留尾部若干行（否则错误信息被淹没）
- 显式 utf-8 + `errors="replace"`（避免非 ASCII 路径在 ASCII locale 下炸掉）

**成片音频直通复制**（`-c:a copy`）：`raw/audio_enhanced.wav` 只供 ASR 使用，从不
进入成片。这样识别准确率与成片音质兼得。

### 硬件编码分级

启动时用**试编码一帧**的方式判断硬件编码器是否真的可用（而不是只看设备节点存在）：

```
h264_nvenc / hevc_nvenc / h264_qsv / h264_amf → libx264
```

判定结果缓存在进程内的 `EncoderSelector`，所以一次进程内多次压制只探测一次。
软件编码是 `INFO`，不是 degradation：它能工作。

### 压制输出是原子的

先写到目标旁的临时文件，**探测确认可读后**才改名到位。v0.1 是无条件改名，导致截断
的编码被当成成品发布——这正是质检步骤要跑 `ffprobe` 的原因。

---

## 6. 作业注册表

作业记录写在一个共享的 JSON 注册表文件里（原子写 + 文件锁 + 用 `/proc` 的进程启动
时间判断 PID 是否陈旧）。

**这解决了“长任务无法轮询”的问题**：`porter run` 在终端 A 启动的作业，终端 B 的
`porter jobs status` 看得到。取消同理——由一个每作业的看门狗线程轮询注册表实现，
因为静默的 yt-dlp 下载期间事件通道根本不产生事件。

---

## 7. 配置与分层

引擎（`src/porter/core/`）不知道部署形态；根门面 `src/porter/__init__.py` 提供稳定
公开 API。CLI（`src/porter/cli/`）、MCP（`src/porter/mcp/`）与 WebUI
（`src/porter/webui/`）是三个前端，都只依赖引擎的公开接口，互相之间不 import。

新的工作流层位于引擎最上层：

```
porter.cli / porter.mcp / porter.webui
        ↓
porter.core.workflow   (model → capabilities → probe → resolver → executor)
        ↓
porter.core.pipeline
        ↓
core.asr / translate / platforms / refine / media / doctor
        ↓
core.models / errors / events / config
```

分层由 `import-linter` 强制（`engine must not import frontends`、`frontends are
independent` 与 `engine layers` 三条契约）。引擎（含根门面）内**零 `print()`**
（ruff `T20`）：MCP 的 stdout 是 JSON-RPC 通道，任何一行杂输出都会破坏协议。所有
日志走 stderr。

**前端一致性**：CLI、MCP、WebUI 都通过 `porter.core.workflow` 解析
`WorkflowPlan` 并调用同一个 `execute_plan`。没有任何前端保留第二套计划模型或
运行期回退链。
