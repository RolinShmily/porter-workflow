# 公开 API

## 稳定性承诺

```python
from porter import JobOptions, JobRequest, Pipeline, RunContext
```

这是受支持的库接口。`porter.__all__` 里的符号属于公开 API；`porter.core.*`
是内部实现，可以随版本调整。内部 import 布局（例如 `porter.models`）不再是
公开路径。

三个 console script 的名称也是稳定的：`porter`、`porter-mcp`、`porter-web`。

## 可用符号

| 类别 | 符号 |
| --- | --- |
| 编排 | `Pipeline` |
| 请求/结果 | `JobOptions`、`JobRequest`、`JobResult` |
| 上下文 | `RunContext` |
| 事件 | `Event`、`EventSink`、`Phase`、`JobState`、`ArtifactKind`、`AwaitingUserAction` |
| 字幕 | `SubtitleItem`、`SubtitleSet`、`TranscriptSentence`、`BurnMode`、`BurnResult` |
| 素材/元数据 | `RawMaterials`、`TaskLayout`、`VideoMetadata` |
| 错误 | `PorterError` 及其子类（`AsrError`、`MediaError`、`ConfigError` 等） |
| 日志 | `get_logger`、`configure_logging`、`__version__` |

`import porter` 本身只加载标准库与 `porter.core.errors` / `core.logging`；
`JobOptions`、`Pipeline` 等符号在第一次访问时才 import 对应模块。

## 最小用法

```python
from pathlib import Path

from porter import JobOptions, JobRequest, Pipeline, RunContext, configure_logging

configure_logging("info")

options = JobOptions(output_dir=Path("./porter_output"))
ctx = RunContext(job_id="example", options=options)
request = JobRequest(url="https://www.youtube.com/watch?v=...", options=options)

result = Pipeline.default(ctx).run(request, ctx)

if not result.ok:
    print(result.error.code, result.error.message)
else:
    print(result.task_dir)
    print(result.subtitles.subtitle_bilingual_srt)
```

`Pipeline.run()` 对预期失败不抛异常：检查 `result.ok` / `result.error`。
`result` 是 Pydantic 模型，可以 `model_dump(mode="json")` 序列化。

## 本地文件

```python
request = JobRequest(local_video=Path("/data/video.mp4"), options=options)
```

`JobRequest` 要求 `url` 与 `local_video` **恰好设置一个**。

## 事件

```python
from porter import Event


def sink(event: Event) -> None:
    print(type(event).__name__, event.model_dump(mode="json"))


ctx.events = sink
```

事件 sink 抛异常不会让作业失败；错误会被记录并丢弃。事件是 JSON 可序列化的，
适合转发给前端或日志收集器。

## 取消

```python
ctx.request_cancel()        # 任意线程安全
```

管线在阶段之间和单位工作之间检查，抛出 `JobCancelled`；CLI 将其映射为退出码
130。取消不是错误，不要把它当成后端失败处理。

## 日志

- `get_logger(name)` 返回 `porter.<name>` 命名空间的 logger，输出到 stderr。
- `get_logger("asr.whisper_local")` → `porter.asr.whisper_local`（logger 名，不是模块路径）。
- `configure_logging(level)` 安装 handler；库调用者通常不需要调用它，宿主应用
  应自行配置日志。

## 错误

```python
from porter import PorterError

try:
    ...
except PorterError as exc:
    print(exc.code, exc.message, exc.details)
```

`PorterError` 子类覆盖配置、媒体、ASR、翻译、渲染、平台不支持、取消等类别。
`JobResult.error` 是 `ErrorInfo`，包含 `code`、`message`、`details`，跨进程可序列化。

## 可选依赖

`Pipeline.default()` 会按安装的 extras 组装后端：

- `[llm]`：OpenAI 兼容的 Whisper 与 LLM 翻译；
- `[asr-local]`：本地 faster-whisper；
- `[images]`：封面处理；
- `[mcp]`、`[web]`：仅前端需要。

只 import `porter` 或使用 CLI 的基础功能不需要这些 extras。
