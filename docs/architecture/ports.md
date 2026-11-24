# 端口

`porter.core.ports` 定义底层管线与实现之间的接缝。`Pipeline` 只依赖这些 Protocol，
从不依赖具体的下载器、ASR 后端、翻译器或渲染器。这带来两个直接结果：

- 引擎可以在没有网络、没有 FFmpeg 的情况下用假实现测试；
- 外部媒体工具（FFmpeg / yt-dlp）留在子进程边界外，且新增后端只需实现窄协议。

## 管线端口

| 协议 | 底层阶段 | 职责 |
| --- | --- | --- |
| `Downloader` | PREPARE（URL） | `can_handle(url)`、`probe(url)`（只取元数据）、`fetch(url)`（下载并标准化） |
| `LocalPreparer` | PREPARE（本地文件） | `prepare(path)`，把已有文件标准化进 `raw/` |
| `Transcriber` | TRANSCRIBE | `available()`、`transcribe(raw)`，返回源语言字幕 |
| `TranscriptRefiner` | TRANSCRIBE 之后 | `available()`、`refine(sentences)`，文本精炼（新执行器固定用 `PassthroughRefiner`） |
| `Translator` | TRANSLATE | `available()`、`translate(subtitles, target_lang)`，返回双语字幕并写文件 |
| `Renderer` | BURN | `render(raw, subtitles, mode)`，返回 `BurnResult` |

`Downloader` 与 `LocalPreparer` 分开，而不是合并成一个“source”协议：
`can_handle("/home/me/video.mp4")` 没有意义，本地文件也不需要猜类型——
源探测和 `JobRequest` 已经明确区分了来源类型。

## 后端窄协议与单能力执行

`AsrChain` 与 `TranslationChain` 背后是更窄的对等能力协议：

- `core.asr.base.AsrBackend`：`name`、`available(ctx)`、`transcribe(audio, ctx)`；
- `core.translate.base.TranslationBackend`：`name`、`available(ctx)`、
  `translate(texts, source_lang, target_lang, ctx)`。

在工作流执行器（`porter.core.workflow.executor`）中，**不存在运行期跨能力回退**：

1. 计划解析阶段（`resolver.py`）已为每个阶段选定**恰好一个能力**。
2. `executor.py` 只把这一个能力装进单元素 `AsrChain` / `TranslationChain`（或通过
   `build_translation_chain(capability)` 构造），不附加其他候选后端。
3. `available()` 必须廉价、永不抛异常；`JobCancelled` 立即向上抛。
4. 翻译仍执行 CJK 自检，拒绝“原文回显”；失败直接归因于该能力并终止，返回
   `alternatives` 供调用方重新 `plan`。
5. `--transcript` / `--translate`（旧别名 `--asr-engine` / `--translator`）在计划阶段
   **严格锁定**该能力，而不是“排到最前、其余做回退”。

## 组装

前端统一通过 `resolve_plan()` + `execute_plan()` 组装并运行单能力管线。
底层的 `Pipeline.default(ctx)` 仍保留为库级兼容入口与测试工具，并在**方法内部**
延迟 import 具体实现，避免 `--help` 冷启动时加载 yt-dlp、openai 等重依赖。

```python
from porter.core.workflow import PlanOverrides, execute_plan, resolve_plan

plan = resolve_plan(source, PlanOverrides(), ctx)
outcome = execute_plan(plan, ctx)
```

向 `Pipeline` 注入实例时用 `is not None` 判断，不能用 `or`：`PlatformDownloader`
定义了 `__len__`，空注册表是 falsy，`or` 会静默丢弃调用者的实例。
