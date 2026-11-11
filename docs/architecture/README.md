# 架构

本目录解释 porter 的内部结构，以及“为什么这样设计”。日常使用只需要根目录
README；要改引擎、排查生产问题或判断改动落点时再读这里。

| 页面 | 内容 |
| --- | --- |
| [overview.md](overview.md) | 一个引擎、四个前端，目录映射，请求生命周期，新代码放哪 |
| [pipeline.md](pipeline.md) | 四个阶段、产物、阶段复用、`--only-phase` 语义 |
| [subtitles.md](subtitles.md) | 平台字幕与 ASR 两条路径、回退链、句子级翻译 |
| [layers.md](layers.md) | import-linter 契约、真实层序、已知缺口 |
| [ports.md](ports.md) | 端口协议、链式回退、扩展点 |
| [jobs.md](jobs.md) | 进程内 `JobStore` 与跨进程 `JobRegistry`、取消、HITL |
| [media.md](media.md) | FFmpeg 边界、标准化、硬件编码探测、原子压制 |
| [launcher.md](launcher.md) | 发布用启动器、为什么不冻结引擎 |

设计约束（不是实现说明）在 [../design/principles.md](../design/principles.md)。

## 先记住三件事

1. **一个引擎，多个前端。** 业务逻辑只在 `porter.core`；CLI、MCP、WebUI 是适配层。
2. **依赖方向单向。** 前端 → 引擎，引擎不得 import 任何前端；三个前端互不依赖。
3. **stdout 是协议通道。** 引擎不 `print()`，日志走 stderr。MCP 的 stdio 传输
   对此零容忍。
