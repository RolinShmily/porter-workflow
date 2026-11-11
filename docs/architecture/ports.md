# 端口

`porter.core.ports` 定义管线与实现之间的接缝。`Pipeline` 只依赖这些 Protocol，
从不依赖具体的下载器、ASR 后端、翻译器或渲染器。这带来两个直接结果：

- 引擎可以在没有网络、没有 FFmpeg 的情况下用假实现测试；
- `videocaptioner`（GPL-3.0）可以作为“另一个可选实现”留在子进程边界外。

## 管线端口

| 协议 | 阶段 | 职责 |
| --- | --- | --- |
| `Downloader` | PREPARE（URL） | `can_handle(url)`、`probe(url)`（只取元数据）、`fetch(url)`（下载并标准化） |
| `LocalPreparer` | PREPARE（本地文件） | `prepare(path)`，把已有文件标准化进 `raw/` |
| `Transcriber` | TRANSCRIBE | `available()`、`transcribe(raw)`，返回源语言字幕 |
| `TranscriptRefiner` | TRANSCRIBE 之后 | `available()`、`refine(sentences)`，修复 ASR 同音字与标点 |
| `Translator` | TRANSLATE | `available()`、`translate(subtitles, target_lang)`，返回双语字幕并写文件 |
| `Renderer` | BURN | `render(raw, subtitles, mode)`，返回 `BurnResult` |

`Downloader` 与 `LocalPreparer` 分开，而不是合并成一个“source”协议：
`can_handle("/home/me/video.mp4")` 没有意义，本地文件也不需要猜类型——
`JobRequest` 已经明确说了是哪一种。

## 后端窄协议

`Transcriber` 和 `Translator` 是**链**：它们拥有回退顺序。链背后的单个引擎实现
更窄的协议：

- `core.asr.base.AsrBackend`：`name`、`available(ctx)`、`transcribe(audio, ctx)`；
- `core.translate.base.TranslationBackend`：`name`、`available(ctx)`、
  `translate(texts, source_lang, target_lang, ctx)`。

后端作者只需要关心窄协议；链负责探测、排序、错误收集和结果校验。

## 链的契约

1. `available()` 必须廉价、永不抛异常。探测失败按“不可用”处理。
2. `available()` 为假的后端被跳过，不进入尝试。
3. 后端抛错或返回空结果：记录，继续下一个。
4. `JobCancelled` 不是后端失败：立即向上抛，不再尝试后续后端。
5. 第一个成功的结果胜出；翻译链还会做 CJK 自检，拒绝“原文回显”。
6. `--asr-engine` / `--translator` 只把指定后端提升到最前，其余仍保留为回退。

## 组装

`Pipeline.default(ctx)` 在**方法内部** import 具体实现，避免 `--help` 冷启动时
加载 yt-dlp、openai 等重依赖。它接收可选的 `downloader`、`renderer`、`local`，
方便前端传入已构造实例，测试传入假实现。

```python
pipeline = Pipeline.default(ctx)
result = pipeline.run(request, ctx)
```

注入时用 `is not None` 判断，不能用 `or`：`PlatformDownloader` 定义了
`__len__`，空注册表是 falsy，`or` 会静默丢弃调用者的实例。
