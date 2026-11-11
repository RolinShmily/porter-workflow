# 扩展点

平台、ASR 后端和翻译后端是最常见的三类贡献。它们各自有注册表、协议和一个
“失败要让链继续走”的约定。

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

## 新增 ASR 后端

ASR 层在 `src/porter/core/asr/`：

1. 实现 `asr/base.py` 的窄协议：`name`、`available(ctx)`、`transcribe(audio, ctx)`。
2. `available()` 必须是廉价、永不抛异常的探测；`transcribe()` 失败时抛
   `AsrError`，让 `AsrChain` 尝试下一个后端。
3. 在 `asr/chain.py` 的候选列表中注册，并决定默认位置。
4. 如果后端需要可选依赖或外部二进制，放进 extra，并在
   `doctor/probes.py` 加对应的事实探测。
5. 测试：`available()` 的真假分支、成功路径、失败后链继续走到下一个后端。

不要在后端里写字幕文件或做翻译；它只返回 `SubtitleItem` 列表。

## 新增翻译后端

翻译层在 `src/porter/core/translate/`，形状与 ASR 镜像：

1. 实现 `translate/base.py` 的协议：`name`、`available(ctx)`、
   `translate(texts, source_lang, target_lang, ctx)`。
2. 在 `translate/chain.py` 的 `FALLBACK_ORDER` 中注册。
3. 链会做 CJK 自检：目标语言是中文但输出没有 CJK 字符时，视为“假翻译”并
   继续下一个后端。新后端必须返回真实目标语言，不能把源文原样带回。
4. 批量请求失败时按句降级，而不是整条后端失败；这是链的既有契约。
5. 测试：正常翻译、限流/超时、CJK 自检拒绝、回退顺序。

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

- 新后端必须**可失败**：错误要能让链继续，不能把整个管线拖垮。
- 不要新增 copyleft 硬依赖；见 [gate.md](gate.md#licence-rules)。
- 新能力放进可选 extra，保持零配置路径可用。
- 行为变更同时更新 `CHANGELOG.md` 和相关文档。
