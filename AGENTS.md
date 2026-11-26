# 维护钩子

| 触发 | 同步动作 |
| --- | --- |
| 功能新增 / 行为变更 | 更新 `CHANGELOG.md` 未发布段；README 三模块（是什么 / 需要什么 / 怎么用）；`docs/architecture.md` 或 `docs/design.md`；`tests/` 对应层级的样例 |
| CLI / 参数 / API / JSON Schema 变更 | 更新 `docs/reference.md` |
| 构建 / 测试流程变更 | 更新 `docs/development.md`；`.github/workflows/test.yml` |
| 依赖 / 第三方代码 / 资源变更 | 依赖声明更新 `pyproject.toml` 与 `uv.lock`；引入 vendored 内容时新增 `THIRD_PARTY_NOTICES` |
| 版本发布（tag） | 归档 `CHANGELOG.md` 对应版本；Release notes 由 `.github/workflows/release.yml` 从根 `CHANGELOG.md` 提取 |
| 根目录入口文件 / 目录或资产位置变更 | 更新 README 文档导航、本文件、workflow 中的路径 |
| workflow 新增 / 触发变更 | 更新 `docs/development.md` 的发布说明；核对本文件对应钩子 |
| `tests/` 结构或样例变更 | 更新 `.github/workflows/test.yml` 的路径与环境 |
| Skill 参考内容（配置键 / 计划契约 / 宿主限制）变更 | 更新 `skills/porter-workflow/references/` 对应文件；`docs/reference.md` 保持路由不复制 |

## 备注

- 不设 `THIRD_PARTY_NOTICES`：仓库无 vendored 第三方代码或资源，依赖以 `pyproject.toml` 为准。
- 不维护 `CODE_OF_CONDUCT.md` 与 PR 模板：暂无社区治理需求。
- Agent Skill 不单独版本化：`skills/porter-workflow/` 随 `main` 分发，不写 `metadata.version`，不维护独立 CHANGELOG，不打 `skill-v*` tag。
