# 设计原则

这些约束比具体实现更稳定。改动实现前先确认不违反它们；要违反，需要单独的
设计讨论和迁移说明，而不是顺手改掉。

## 1. stdout 是协议通道

在 MCP stdio server 中，stdout 传 JSON-RPC。引擎里任何一行 `print()`、任何
把日志写到 stdout 的依赖，都会让客户端解析失败并断开连接。

因此：

- 引擎只写 stderr，使用 `get_logger`；
- CLI/WebUI 可以打印，但它们是终端/浏览器前端；
- MCP 用 `stdout_guard` 把意外写入转成日志，作为第二道防线。

## 2. 引擎无部署假设

`porter.core` 不知道调用方是 CLI、MCP、WebUI、Agent Skill，还是别人的脚本。
它不探测 `SKILL.md`，不假设配置文件在工作目录，不假设输出目录可写。

配置来源只有：显式参数、环境变量、项目配置、平台用户目录。宿主需要特殊路径时，
通过 `--config` / `PORTER_CONFIG` / `PORTER_CACHE_DIR` 显式传入。

## 3. 零配置路径保持轻量

纯 Python + FFmpeg 必须能完成下载、转录（有平台字幕或配置了 Key）、翻译和压制。
`openai`、`pillow`、`faster-whisper`、`gradio`、`fastmcp`
都是可选依赖，必须延迟 import。

这条约束的现实意义：用户第一次运行 `porter doctor` 不应该先下载一个 Web 框架。

## 4. 端口与适配器

管线依赖协议，不依赖实现。每个阶段一个端口（`Downloader`、`Transcriber`、
`Translator`、`Renderer`，以及 `LocalPreparer`、`TranscriptRefiner`），
后端实现窄协议。

收益不是“架构好看”，而是：

- 管线可以在没有网络、没有 FFmpeg 的情况下测试；
- GPL 的 `videocaptioner` 可以作为外部进程适配器存在，不污染 MIT 代码；
- 新增平台/引擎不需要改管线。

## 5. 原子产物

写文件先写临时文件，探测/校验通过后再改名。压制成片尤其如此：截断的编码不能
被当成成品发布。失败时留下的是临时文件，不是半成品。

## 6. 协作式取消

取消是标志，不是信号或 `kill`。管线在阶段之间和单位工作之间检查；后端看到
`JobCancelled` 立即停止，不尝试下一个。长跑子进程注册句柄，以便尽快终止。

跨进程取消通过共享注册表的 `cancel_requested` 字段，由看门狗轮询；不依赖
“工作恰好在上报进度”。

## 7. 许可证边界

Porter 是 MIT。GPL/AGPL 代码只能：

- 作为用户自行安装的外部程序，通过 `subprocess` 调用；
- 或者仅借鉴思路，不复制代码。

不能成为硬依赖，不能 import 进进程。新增依赖必须更新
`THIRD_PARTY_NOTICES.md`。

## 8. 前端只做适配

CLI、MCP、WebUI 不实现业务逻辑。它们负责：

- 解析输入、构造 `JobRequest` / `JobOptions`；
- 调用引擎；
- 把事件渲染成终端进度、MCP 状态或网页组件；
- 映射退出码/状态。

同一能力在三个前端出现不同行为，是引擎边界泄漏的信号，不是前端该各自修的 bug。

## 9. 配置只解析一次

一次运行只解析一次配置，结果放进 `RunContext.config`。阶段读取该对象，不重复
读文件。否则配置文件在运行中途被修改，会让前后阶段使用不同设置。

## 10. 公开 API 窄而稳定

稳定接口是 `from porter import ...`。`porter.core.*` 是内部实现，可以调整。
新增公开符号需要同时更新 `_LAZY_EXPORTS`、`TYPE_CHECKING`、`__all__` 和文档；
架构测试会逐项验证。

## 11. 诚实的失败与数据

- 计划不提供编造的时间预估；
- 字幕合并会报告 `input_cue_count` / `cue_count` / `cues_merged`；
- 未知视频尺寸保持 `None`，不伪造 1920x1080；
- 不可用链接是一次成功的探测，但退出码仍为非零，方便脚本判断。
