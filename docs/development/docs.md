# 文档维护

## 信息架构

| 目录 | 回答的问题 | 读者 |
| --- | --- | --- |
| `docs/development/` | 怎么搭建、怎么过门禁、怎么发布、怎么扩展 | 贡献者 |
| `docs/architecture/` | 系统由什么组成、依赖往哪走、为什么这样设计 | 维护者 |
| `docs/reference/` | 命令、API、配置的权威查找入口 | 使用者与集成者 |
| `docs/design/` | 不可妥协的设计约束与 WebUI 基线 | 设计与实现者 |
| `docs/images/` | 文档与 README 的源图片 | — |

Agent 运行时文档不在这里。它们随技能分发：

- `skills/porter-skill/SKILL.md`：技能入口与工作流；
- `skills/porter-skill/references/CONFIG.md`：配置键全表；
- `skills/porter-skill/references/MCP.md`：MCP 工具契约；
- `skills/porter-skill/references/HOSTS.md`：受限宿主与 Windows；
- `skills/porter-skill/references/ARCHITECTURE.md`：给 Agent 的架构摘要。

`docs/` 对这些主题只做索引，不复制内容。

## 语言

- `docs/` 以简体中文为主。
- 命令、路径、代码标识符、错误信息保留英文。
- `README.md` 是英文，`README_zh.md` 是中文；用户可见变更两边都改。
- 治理文件（`CONTRIBUTING`、`CODE_OF_CONDUCT`、`SECURITY`、`CHANGELOG`、
  `THIRD_PARTY_NOTICES`）与 GitHub 模板保持英文。
- `THIRD_PARTY_NOTICES.md` 是法律声明，只写英文。

## 链接规则

- 指向 `docs/`、`src/`、`skills/` 的目标：用相对链接，保证 sdist 内也能点开。
- 指向 `tests/`、`.github/` 等不进入 sdist 的目标：用 GitHub 绝对 URL。
- 指向代码时优先写 `path:line` 或模块路径，不粘贴大段会过期的代码。
- 外部链接用 HTTPS；不要链接到会变的搜索结果页。

## 图片

- 所有文档图片放 `docs/images/`，不要新建根级 `assets/`。
- 图片是源媒体：`.gitignore` 对 `*.webp`/`*.png` 的全局忽略通过
  `!/docs/images/**` 放行。
- README 的预览图使用绝对 raw URL，因为 PyPI 的长描述无法解析仓库相对路径；
  GitHub 上同一 URL 也能显示。

## 新增或修改文档

1. 先判断它属于哪个目录：开发流程 → `development/`；内部结构 → `architecture/`；
   查找型信息 → `reference/`；约束与基线 → `design/`。
2. 更新对应目录的 `README.md` 索引。
3. 如果文档描述的是行为，确认代码、`CHANGELOG.md` 和 README 同步。
4. 本地检查相对链接目标是否存在；坏链会让文档 PR 失效。
5. 不要在 `docs/` 里放本地私有笔记，也不要创建第二个 `SKILL.md`。

## 何时更新文档

| 变更 | 必须更新 |
| --- | --- |
| CLI 参数、退出码 | `docs/reference/cli.md`、`CHANGELOG.md` |
| 公开 API | `docs/reference/api.md`、`CHANGELOG.md` |
| 配置优先级或环境变量 | `docs/reference/configuration.md`；配置键表在技能 references |
| 分层、端口、阶段 | `docs/architecture/` 对应页面 |
| 新增依赖 | `THIRD_PARTY_NOTICES.md`、`CHANGELOG.md` |
| 用户可见行为 | `README.md` 与 `README_zh.md` |
