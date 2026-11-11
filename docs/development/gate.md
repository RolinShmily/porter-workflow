# 门禁

每次改动必须在本地通过 CI 会跑的同一组检查。四个命令都从仓库根目录执行：

```bash
uv run ruff check src tests scripts     # 风格、导入排序、T20（引擎不得 print）
uv run mypy src scripts                 # strict 类型检查
uv run lint-imports                     # 引擎 / 前端边界与层序
uv run pytest -m "not network"          # 完整离线测试
```

`--fix` 可以交给 ruff：`uv run ruff check src tests scripts --fix`。
提交前再跑一次不带 `--fix` 的版本，确认没有剩余问题。

## 测试分层

| 目录 | 内容 | 依赖 |
| --- | --- | --- |
| `tests/unit/` | 快速、打桩、无系统依赖 | 无 |
| `tests/regression/` | v0.1 行为的等价性回归，部分使用真实 FFmpeg | 部分需要 FFmpeg |
| `tests/integration/` | 端到端，真实 FFmpeg、真实进程 | FFmpeg、文件系统 |

标记：

- `slow`：会启动真实 FFmpeg 或完整管线。CI 的主门禁仍会运行它们。
- `network`：访问公网。默认排除；离线时会 skip。不要把它当作门禁的一部分。

CI 的 `hermetic` 任务会隐藏 PATH 上的 `ffmpeg`/`ffprobe`，再运行
`pytest -m "not slow"`（并排除唯一一个刻意使用真实二进制的回归文件），
以证明测试没有偷偷依赖开发机环境。新增测试不得假设本机有 FFmpeg，除非它标了
`slow`。

## 架构规则

这些规则由工具强制，不是约定。破坏其中任何一条，门禁会失败。

### 1. 引擎不得 import 前端

依赖方向只有一个：`porter.cli` / `porter.mcp` / `porter.webui` → `porter.core`。
`src/porter/core/` 和根门面 `src/porter/__init__.py` 都不得 import 三个前端。
`lint-imports` 与 `tests/unit/test_architecture.py` 双重检查；后者会解析
`from porter import cli`、`import porter.cli as c` 和相对 import 的完整模块名，
不会因为顶层包名统一是 `porter` 而漏判。

### 2. 引擎不得写 stdout

MCP 的 stdio 传输用 stdout 传 JSON-RPC。引擎里一行 `print()` 就会破坏协议。
规则由 ruff 的 `T20` 执行，作用范围是 `src/porter/`：

- 豁免：`src/porter/cli/**` 与 `src/porter/webui/**`，它们面向终端/浏览器；
- 不豁免：`src/porter/core/**`、`src/porter/__init__.py` 和 `src/porter/mcp/**`
  （MCP 另有 `stdout_guard` 把意外写入转成日志）。

日志一律走 `porter.core.logging.get_logger`，输出到 stderr。

### 3. 遵守层序

`[tool.importlinter]` 的 `layers` 契约编码了真实依赖图。高层可以 import 低层，
不能反向，也不能在同层内互相依赖。为什么每个模块在那一层，写在
`pyproject.toml` 的注释里；整体说明见 [architecture/layers.md](../architecture/layers.md)。

### 4. 零配置路径保持轻量

纯 Python + FFmpeg 必须在没有 `openai`、`pillow`、`gradio`
或 `fastmcp` 的情况下工作。新能力放进可选 extra，并且延迟 import：

- `porter.core.pipeline.Pipeline.default()` 在方法内部 import 具体后端；
- `porter.mcp.server.create_server()` 延迟 import `fastmcp`；
- `porter.webui.app.create_app()` 延迟 import `gradio`。

### 5. 文本写入显式指定编码

`Path.write_text()` 和文本模式 `open()` 必须传 `encoding="utf-8"`。
`tests/unit/test_architecture.py` 会扫描整个 `src/porter/`。不指定编码会退回
locale 编码；在 Windows 上写入 CJK 字幕或带 emoji 的标题会抛
`UnicodeEncodeError`，而且 CI 的 UTF-8 环境无法复现。

### 6. 公开 API 保持懒加载

`from porter import Pipeline` 是稳定接口。根包的重符号通过 PEP 562
`__getattr__` 懒加载，`import porter` 只依赖标准库。新增公开符号时同时更新
`_LAZY_EXPORTS`、`TYPE_CHECKING` 块和 `__all__`；架构测试会逐项验证映射。

## 许可证规则

Porter 使用 MIT。贡献按同样条款接受。

- **不要新增 copyleft 硬依赖。** GPL/AGPL 代码只能留在子进程边界之外，
  例如 `videocaptioner` 那样由用户自行安装、porter 只 `subprocess` 调用。
- **新增或删除依赖时更新 [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md)**，
  写明许可证以及 porter 是依赖、可选安装还是仅外部调用。
- **不要把 copyleft 项目的代码粘贴进本仓库。**
- 不确定时先在 issue 里问，再写代码。

### Licence rules

The English heading above is kept stable so GitHub anchors used by issue
templates (`docs/development/gate.md#licence-rules`) do not break. The rules are:

- No hard dependency on a copyleft package. GPL/AGPL code stays behind a
  subprocess boundary, as `videocaptioner` does.
- Update `THIRD_PARTY_NOTICES.md` whenever a dependency is added or removed.
- Do not paste code from a copyleft project into this repository.

## 提交前自查

- [ ] 四个门禁命令通过。
- [ ] 行为变更带测试；修复类改动有能在修复前失败的测试。
- [ ] 新测试不依赖本机 FFmpeg，除非标了 `slow`。
- [ ] 引擎仍然不 import 前端、不 `print()`。
- [ ] 文本写入都指定了 UTF-8。
- [ ] 依赖变更同时更新了 `uv.lock` 和 `THIRD_PARTY_NOTICES.md`。
- [ ] `README.md` / `README_zh.md` / `docs/` / `CHANGELOG.md` 已按需更新。
