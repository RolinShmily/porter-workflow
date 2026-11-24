# 分层与边界

分层由 `pyproject.toml` 里的三条 import-linter 契约强制：

```toml
[tool.importlinter]
root_package = "porter"

[[tool.importlinter.contracts]]
name = "engine must not import frontends"
type = "forbidden"
source_modules = ["porter.core"]
forbidden_modules = ["porter.cli", "porter.mcp", "porter.webui"]

[[tool.importlinter.contracts]]
name = "frontends are independent"
type = "independence"
modules = ["porter.cli", "porter.mcp", "porter.webui"]

[[tool.importlinter.contracts]]
name = "engine layers"
type = "layers"
```

## 真实层序

自上而下，高层可以 import 低层，不能反向，也不能在同层内互相依赖：

| 层 | 模块 | 为什么在这里 |
| --- | --- | --- |
| 1 | `core.workflow` | 位于最顶层：探测源、列举能力、解析 `WorkflowPlan`，并在执行器里驱动底层管线 |
| 2 | `core.pipeline` | 底层四阶段执行编排，依赖下面所有端口和后端 |
| 3 | `core.doctor` | 跨切面服务，观察媒体、平台、ASR/翻译后端；被前端与工作流层消费，但不被管线无条件调用 |
| 4 | `core.asr` / `core.translate` / `core.platforms` / `core.refine` | 后端适配与转录文本精炼 |
| 5 | `core.media` / `core.subtitles` | 媒体与字幕处理 |
| 6 | `core.ports` | 管线与实现之间的协议 |
| 7 | `core.jobs` | 前端面向的服务，只需要模型、上下文和事件 |
| 8 | `core.context` | 单次运行上下文 |
| 9 | `core.models` | 领域模型 |
| 10 | `core.config` | 配置模型与解析 |
| 11 | `core.events` | 结构化事件 |
| 12 | `core.logging` / `core.errors` / `core.utils` / `core.mirrors` | 叶子工具 |

注释里的历史说明（例如 `plan`、`jobs` 为什么曾被漏掉）保留在
`pyproject.toml`，因为那些是“为什么不是更直觉的顺序”的证据。

## 前端边界

三个前端是兄弟，不是层：

- 都可以 import `porter.core.*` 和根包公开 API；
- 互相之间不得 import；
- 根门面 `porter/__init__.py` 与 `core/` 一样受“不得 import 前端”约束。

前端互相独立由 `independence` 契约强制；`tests/unit/test_architecture.py`
保留同一检查，因为它能在普通测试套件里直接指出违规文件，而无需等到
`lint-imports` 才失败。

## 引擎纯度

- 引擎（`core/` 与根门面）不得 `print()`；ruff `T20` 与架构测试双重检查。
- 日志只走 stderr。
- 文本写入必须显式 `encoding="utf-8"`；架构测试扫描整个 `src/porter/`。
- 公开 API 懒加载，`import porter` 只依赖标准库。

## 已知缺口

目前没有已知的层契约缺口：`core.refine` 已归入后端层，`core.mirrors` 已归入
叶子工具层。新增模块时必须同步放入某一层，否则它会落在契约之外、可以自由
import；`lint-imports` 不会对此告警。
