# 管线

`porter.core.pipeline.Pipeline` 只负责**顺序**。每个阶段委托给
`porter.core.ports` 里的一个协议，因此同一套编排可以用真实后端跑生产，
也可以用假后端跑测试。

## 四个阶段

```
prepare → transcribe → translate → burn
```

| 阶段 | 做什么 | 主要产物 |
| --- | --- | --- |
| `prepare` | 获取媒体、标准化母版、提取音轨、增强、探测画幅、取封面与平台字幕 | `raw/video.mp4`、`raw/audio.wav`、`raw/audio_enhanced.wav`、`raw/metadata.json` |
| `transcribe` | 取平台原生字幕轨，或走 ASR 回退链 | `raw/subtitle.srt`、`raw/transcript.json`、`raw/transcript.txt` |
| `translate` | 句子级翻译、碎片重构、生成双语/中文轨 | `cooked/subtitle_bilingual.{srt,ass}`、`cooked/subtitle_zh.{srt,ass}` |
| `burn` | 用 libass 烧录硬字幕 | `cooked/video_bilingual.mp4`、`cooked/video_zh.mp4` |

每个阶段消费上一阶段的内存结果，同时把自己的产物写到磁盘。失败时已完成的阶段
不会白跑，这是阶段复用和 `--only-phase` 语义的基础。

## 阶段复用与 `--force`

每个阶段在开始前检查自己的产物是否已经存在且可用；可用就跳过实际工作。
`--force` 是唯一关闭复用的开关：它让所有阶段重新执行，而不是“从断点继续”。

注意：`--force` **不启用**“只用磁盘产物跳到某个阶段”的功能。后者尚未实现。

## `--only-phase` 是“到此为止”

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

取消不是后端失败：ASR/翻译链看到 `JobCancelled` 会立即停止，而不是尝试下一个
后端。

## 失败如何返回

`Pipeline.run()` 对**预期失败**不抛异常：错误被捕获成 `JobResult.error`
（`ErrorInfo`），状态为 `FAILED`，这样 MCP 可以把失败当数据返回，而不是让
工具调用崩溃。真正的编程错误（例如端口返回了不可能的状态）仍会向上抛。
