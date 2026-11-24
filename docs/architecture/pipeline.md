# 工作流与管线

Porter 的编排分为两层：

- **上层工作流（`porter.core.workflow`）**：所有前端（CLI、MCP、WebUI）的统一入口。
  按源事实从 6 条工作流中选定一条（`url.subtitled`、`url.asr`、`local.subtitled`、
  `local.asr`、`subtitle.only`、`audio.asr`），把作业拆成 7 个逻辑阶段，并为每个阶段
  选定**恰好一个对等能力**（无运行期跨能力回退）。
- **底层媒体管线（`porter.core.pipeline.Pipeline`）**：负责媒体类作业的四阶段顺序执行。
  每个阶段委托给 `porter.core.ports` 里的一个协议，因此同一套编排可以用真实后端跑生产，
  也可以用假后端跑测试。`subtitle.only` 工作流直接读字幕、翻译、写产物，不经过 `Pipeline`。

## 七个逻辑阶段与四个执行阶段

```
工作流阶段：probe → acquire → normalize → transcript → translate → write → render
执行事件期：          └─── prepare ───┘   transcribe    └── translate ──┘   burn
```

| 执行阶段 | 对应工作流阶段 | 做什么 | 主要产物 |
| --- | --- | --- | --- |
| `prepare` | `acquire` + `normalize` | 获取媒体、标准化母版、提取 16 kHz 单声道音轨、增强、探测画幅、取封面与平台字幕 | `raw/video.mp4`、`raw/audio.wav`、`raw/audio_enhanced.wav`、`raw/metadata.json` |
| `transcribe` | `transcript` | 按计划选定的单一转录能力执行（平台字幕轨、供字幕或指定 ASR），不做运行期跨能力回退 | `raw/subtitle.srt`、`raw/transcript.json`、`raw/transcript.txt` |
| `translate` | `translate` + `write` | 用计划选定的单一翻译能力做句子级翻译、碎片重构，写出双语/中文 SRT 与 ASS | `cooked/subtitle_bilingual.{srt,ass}`、`cooked/subtitle_zh.{srt,ass}` |
| `burn` | `render` | 用 libass 烧录硬字幕 | `cooked/video_bilingual.mp4`、`cooked/video_zh.mp4` |

每个阶段消费上一阶段的内存结果，同时把自己的产物写到磁盘。失败时已完成的阶段
不会白跑，这是阶段复用与 `--task` 截断的基础。

## 阶段复用与 `--force`

每个阶段在开始前检查自己的产物是否已经存在且可用；`transcript` 复用时还会校验
产物来源前缀（`origin_prefix`），避免上一次不同能力产生的字幕被错误复用。
`--force` 是唯一关闭复用的开关：它让所有阶段重新执行，而不是“从断点继续”。

注意：`--force` **不启用**“只用磁盘产物跳到某个阶段”的功能。后者尚未实现。

## 任务截断（`--task`）与底层 `--only-phase`

面向工作流的截断参数是 `--task`（`probe` / `audio` / `transcribe` / `subtitle` / `release`），
它按阶段截断计划，不负责切换后端。底层 `Pipeline` 仍保留 `--only-phase` 语义：

因为每个阶段需要上一阶段的输出，`--only-phase translate` 仍会执行
`prepare` 和 `transcribe`。它表达的是“做完这个阶段就停”。

`burn` 选项先于 `only-phase` 应用：

- `--only-phase translate`：跑 prepare、transcribe、translate，不跑 burn。
- `--only-phase burn --burn skip`：准备阶段列表时 BURN 已被 `skip` 移除，
  因此只跑前三个阶段然后停止——这正是“只要字幕、不要成片”的意图。
- `--only-phase burn` 且 `--burn skip`：没有任何阶段可跑，代码返回空列表，
  而不是静默跑完其他三个阶段。

## 压制模式

| `--burn` | 产物 |
| --- | --- |
| `dual`（默认） | `video_bilingual.mp4` + `video_zh.mp4` |
| `zh_only` | 仅 `video_zh.mp4` |
| `bilingual_only` | 仅 `video_bilingual.mp4` |
| `skip` | 只产出字幕，不压制 |

`BurnMode` 取代了 v0.1 的 `skip_burn` / `only_bilingual` / `only_zh` 三个布尔
开关，后者的组合里包含两个不可能状态。

## 事件与进度

引擎不打印进度，而是发结构化事件：

| 事件 | 用途 |
| --- | --- |
| `PhaseStarted` / `PhaseCompleted` / `PhaseFailed` | 阶段边界与耗时 |
| `ProgressUpdated` | 百分比与消息，前端自行渲染 |
| `ArtifactReady` | 产物路径与种类，供前端/Agent 立即取用 |
| `AwaitingUserAction` | HITL：需要人工确认才能继续 |
| `LogRecord` | 转发日志记录，不直接写终端 |

事件都是 JSON 可序列化的，MCP 可以原样转发。事件 sink 抛异常不会让作业失败：
`RunContext.emit()` 吞掉 sink 错误并记 debug 日志；取消是单独的标志。

## 取消

`RunContext.cancel` 是 `threading.Event`，任意线程可以置位。阶段在单位工作之间
调用 `ctx.check_cancelled()`，抛出 `JobCancelled`：

- CLI 映射为退出码 130；
- MCP 映射为 `JobState.CANCELLED`；
- 长跑子进程会注册句柄，被尽快终止。

取消不是后端失败：遇到 `JobCancelled` 会立即向上传播并停止，不做重试。

## 失败如何返回

`execute_plan()` 与 `Pipeline.run()` 对**预期失败**不抛异常：错误被捕获成
`WorkflowResult.error`（`StageError`，附带 `stage`、`capability`、`alternatives`、
`suggested_workflow`）与 `JobResult.error`（`ErrorInfo`），状态为 `FAILED`，这样 CLI、
MCP 与 WebUI 都能把失败当结构化数据返回，供调用方或 Agent 重新 `plan`。真正的编程错误
（例如端口返回了不可能的状态）仍会向上抛。
