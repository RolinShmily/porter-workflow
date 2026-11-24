# 扩展点

平台、ASR 能力和翻译能力是最常见的三类贡献。每个阶段的能力都是**对等**的：
它们各自实现窄协议，并在工作流能力注册表里声明前提、成本与外发方向；执行期不做
跨能力回退，失败时由引擎返回结构化替代项供重新 `plan`。

## 新增平台

平台层在 `src/porter/core/platforms/`：

1. 在 `spec.py` 的 `PlatformSpec` 里声明新平台的 URL 模式、标题清理规则、
   字幕能力和画幅默认值。
2. 如果通用 yt-dlp 提取器足够，只加一个 `PlatformSpec` 并在
   `registry.py` 注册；需要特殊处理时，参照 `bilibili.py` 等实现一个提取器。
3. 在 `urls.py` 补 URL 归一化/短链展开规则（纯函数，不发网络请求）。
4. 加单元测试：URL 识别、标题清理、字幕轨选择；网络部分打桩。
5. 如果平台有环境前提（cookie、登录），在 `doctor/probes.py` 加探测，并在
   `doctor/guides.py` 写修复步骤。

平台提取器只负责“拿到素材”；标准化、音频提取、增强、封面和元数据由
`media/prepare.py` 统一完成。不要把这段流程复制到新平台里。

## 新增 ASR 能力

ASR 层在 `src/porter/core/asr/`，注册在 `src/porter/core/workflow/`：

1. 实现 `asr/base.py` 的窄协议：`name`、`available(ctx)`、`transcribe(audio, ctx)`。
2. `available()` 必须是廉价、永不抛异常的探测；`transcribe()` 失败时抛
   `AsrError`（返回空结果同样按失败处理）。
3. 在 `core/workflow/capabilities.py` 声明新的 `transcript.*` 能力描述符
   （`cost`、`egress`、`requires`、`download_mb`），并在 `resolver.py` 与
   `executor.py` 中接入解析与单能力构造。
4. 如果能力需要可选依赖，放进 `pyproject.toml` 对应 extra，并在
   `doctor/probes.py` 加事实探测。
5. 测试：`available()` 的真假分支、成功路径、失败报错以及计划解析/付费门控行为。

不要在后端里写字幕文件或做翻译；它只返回 `SubtitleItem` 列表。

## 新增翻译能力

翻译层在 `src/porter/core/translate/`，注册在 `src/porter/core/workflow/`：

1. 实现 `translate/base.py` 的协议：`name`、`available(ctx)`、
   `translate(texts, source_lang, target_lang, ctx)`。
2. 在 `core/workflow/capabilities.py` 声明新的 `translate.*` 能力描述符，并在
   `resolver.py` 与 `executor.py`（含 `build_translation_chain`）中接入。
3. 执行层会做 CJK 自检：目标语言是中文但输出没有 CJK 字符时，视为“假翻译”并
   判定该能力失败。新能力必须返回真实目标语言，不能把源文原样带回。
4. 批量请求失败时可在**同一能力内**按句降级重试，而不是整条能力立即崩溃。
5. 测试：正常翻译、限流/超时、CJK 自检拒绝、计划解析与付费门控。

翻译后端只接收文本、返回文本；句子合并、断句和 ASS 渲染属于
`core/subtitles/`，不要在翻译后端里重做。

## 新增 MCP 工具

MCP 层在 `src/porter/mcp/tools/`：

1. 新建模块，暴露 `register(server)`，在 `tools/__init__.py` 的 `_MODULES`
   里显式加入。
2. 工具函数体用 `stdout_guard.protect` 包装；工具名是 Agent 的公开 API，
   改名需要迁移说明。
3. 业务逻辑放在引擎里，工具只做参数校验、调用和序列化。
4. 长任务用 job 工具（start/status/result/cancel），不要写阻塞式工具。

## 新增 CLI 命令

1. 在 `src/porter/cli/commands/` 新建模块，提供 `configure(subparsers)` 和
   `run(args)`。
2. 在 `cli/app.py` 的 `SUBCOMMANDS` 与 `configure` 循环中注册。
3. 输出走 `cli/render.py`；退出码遵循 0 成功、1 失败、2 用法错误、130 取消。
4. 需要结构化输出时支持 `--json`，并确保 stdout 只有 JSON。

## 通用约束

- 新能力必须诚实失败：不吞异常、不把源文原样当译文；错误附带可操作的上下文。
- 不要新增 copyleft 硬依赖；见 [gate.md](gate.md#licence-rules)。
- 重型或付费能力放进可选 extra，并在能力描述符中声明 `cost` 与 `egress`，保持零配置路径可用。
- 行为变更同时更新 `CHANGELOG.md` 和相关文档。
