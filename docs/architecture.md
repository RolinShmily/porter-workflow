# 架构总览

Porter 采用**单核引擎 + 薄入口层**架构：全部业务逻辑位于 `porter.core`，WebUI（`porter.webui`，可选 `[web]`）与 Agent Skill（[`skills/porter-workflow/`](../skills/porter-workflow/SKILL.md)）均调用 `porter` 根包公开门面，共享同一套 `WorkflowPlan` 模型。

## 1. 目录与分层契约

```text
src/porter/
├── __init__.py        稳定公开 API（PEP 562 懒加载），__version__
├── core/
│   ├── workflow/      7 阶段计划模型、能力目录、源探测、解析、单能力执行与门面
│   ├── pipeline.py    底层 4 阶段媒体执行编排
│   ├── ports.py       管线与后端间的 Protocol 接缝
│   ├── doctor/        机器健康探测与修复指引
│   ├── asr/           语音识别对等后端
│   ├── translate/     翻译对等后端
│   ├── platforms/     平台识别、yt-dlp 提取、目标画质策略与原生字幕
│   ├── refine/        转录文本精炼
│   ├── media/         FFmpegRunner、标准化、降噪、试编码、libass 压制
│   ├── subtitles/     SRT/ASS、短碎片合并、句子重构、意群断句
│   ├── jobs/          作业存储与跨进程注册表
│   ├── models/        领域模型
│   └── config.py / context.py / events.py / logging.py / errors.py / mirrors.py / utils/
└── webui/             可选 Gradio 工作台（仅做展示与参数适配）
```

`pyproject.toml` 中的 `import-linter` 与 `tests/unit/test_architecture.py` 双重强制：
1. **单向依赖**：`porter.core` 与 `porter/__init__.py` 严禁 import `porter.webui`。
2. **引擎 12 层自顶向下**（高层可引低层，禁反向与同层互引）：
   `workflow → pipeline → doctor → (asr | translate | platforms | refine) → (media | subtitles) → ports → jobs → context → models → config → events → (logging | errors | utils | mirrors)`。

## 2. 工作流、端口与管线

- **上层工作流（`core.workflow`）**：根据 `probe_source()` 从 6 条工作流中择一（`url.subtitled` / `url.asr` / `local.subtitled` / `local.asr` / `subtitle.only` / `audio.asr`），为 7 个逻辑阶段选定**恰好一个对等能力**，封存为带 `plan_id` 与 `config_fingerprint` 的 `WorkflowPlan`。
- **底层管线（`core.pipeline.Pipeline`）**：通过 `core.ports` 六个窄协议（`Downloader`、`LocalPreparer`、`Transcriber`、`TranscriptRefiner`、`Translator`、`Renderer`）顺序执行媒体阶段（`subtitle.only` 跳过 `Pipeline`）。

```text
工作流阶段：probe → acquire → normalize → transcript → translate → write → render
执行事件期：          └─── prepare ───┘   transcribe    └── translate ──┘   burn
```

- **无运行期跨能力回退**：`executor.py` 只构造计划指定的单一能力；失败返回结构化 `StageError`（含 `alternatives` / `suggested_workflow`）供显式重规划。
- **平台字幕与目标画质**：默认按 `1080p` 短边排序（不足降级并发 `QUALITY_DEGRADED`）；仅平台存在**人工**目标语轨时跳过翻译（`TRANSLATION_SKIPPED`），运行期拉取失败返回 `TARGET_TRACK_MISSING`。

## 3. 媒体层与硬件加速

`core/media/ffmpeg.py`（`FFmpegRunner`）是唯一启动 FFmpeg/ffprobe 子进程的位置：统一 `-nostdin`、保留 stderr 尾部错误、显式 UTF-8 解码。
- **标准化**：`raw/video.mp4`（优先 `-c:v copy`，AAC 44.1 kHz + `faststart`）；`raw/audio.wav`（16 kHz 单声道）；`raw/audio_enhanced.wav` 降噪音轨**仅用于 ASR**，成片直通原音轨（`-c:a copy`）。
- **试编码探测**：`core/media/encode.py` 通过真实编码 256×144 黑帧按序验证 `h264_nvenc → h264_qsv → h264_amf → h264_videotoolbox → libx264`。压制先写临时文件校验后再原子重命名。

## 4. 字幕重构与 CJK 自检

1. 合并滚动短碎片 → 按语法/静音重构 `TranscriptSentence` → 整句翻译 → 按中文意群重新断句（≤ 20 字/行，按字数比例分配时间戳），并报告 `input_cue_count` / `cue_count` / `cues_merged`。
2. `has_chinese_translation()` 强制校验中文译文含 CJK 字符，拒绝免 Key 端点 HTTP 200 原文回显假翻译；单能力内可降级逐句重试。

## 5. 跨进程作业与协作取消

`JobStore` + 共享 `JobRegistry`（位于 `PORTER_CACHE_DIR`）跨进程同步状态（`PENDING → RUNNING → DONE / FAILED / CANCELLED / AWAITING_USER`），绑定 `owner_pid` 与启动时间并在读取时回收死进程记录。取消写入 `cancel_requested`，看门狗轮询置位 `RunContext` 取消标志并终止已注册子进程。
