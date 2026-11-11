# Porter Workflow 文档

本目录是**仓库维护者与人类读者**的文档中心。它和随技能分发的 Agent 文档
（`skills/porter-skill/references/`）分工明确：这里解释项目如何组织、如何开发、
为什么这样设计；那里是 Agent 在运行时需要的配置、MCP 与宿主契约。

## 阅读路径

| 你是 | 从这里开始 |
| --- | --- |
| 第一次使用 | 根目录的 [README](../README_zh.md)，然后 [CLI 参考](reference/cli.md) |
| 想接入自己的脚本 | [公开 API](reference/api.md) 与 [配置解析](reference/configuration.md) |
| 要提交代码 | [开发入口](development/README.md)，先读 [环境搭建](development/setup.md) 和 [门禁](development/gate.md) |
| 要改引擎内部 | [架构总览](architecture/overview.md)，再按需读具体子系统 |
| 要改 WebUI | [WebUI 设计基线](design/webui.md) |
| 是 AI Agent | [`skills/porter-skill/SKILL.md`](../skills/porter-skill/SKILL.md)，不是这里 |

## 文档地图

```
docs/
├── README.md                 你在这里
├── development/              开发规范：环境、门禁、发布、扩展、文档维护
├── architecture/             架构规范：阶段、字幕、分层、端口、作业、媒体、启动器
├── reference/                参考规范：CLI、公开 API、配置解析
├── design/                   设计规范：不可妥协的约束、WebUI 基线
└── images/                   文档与 README 使用的源图片
```

## 单一事实源

文档会过时，代码不会。每个主题只在一个地方拥有权威版本：

| 主题 | 权威位置 | 这里怎么处理 |
| --- | --- | --- |
| CLI 命令与参数 | `porter <cmd> --help`、`src/porter/cli/` | [reference/cli.md](reference/cli.md) 讲结构与约定，不逐字复制帮助文本 |
| 稳定公开 API | `src/porter/__init__.py` 的 `__all__` | [reference/api.md](reference/api.md) 给最小用法 |
| 配置键全表 | [`skills/porter-skill/references/CONFIG.md`](../skills/porter-skill/references/CONFIG.md) | [reference/configuration.md](reference/configuration.md) 只讲优先级与来源 |
| MCP 工具契约 | [`skills/porter-skill/references/MCP.md`](../skills/porter-skill/references/MCP.md) | 不在 `docs/` 复制 |
| 受限宿主与 Windows | [`skills/porter-skill/references/HOSTS.md`](../skills/porter-skill/references/HOSTS.md) | 不在 `docs/` 复制 |
| 架构决策 | 模块 docstring + `docs/architecture/` | 二者不一致时，以代码为准并修文档 |
| 变更历史 | [CHANGELOG.md](../CHANGELOG.md) | 行为变更必须在此登记 |

## 维护规则

1. **语言**：`docs/` 以简体中文为主；代码标识符、命令、路径和错误信息保留英文原文。
2. **链接**：`docs/`、`src/`、`skills/` 内的目标用相对链接；`tests/`、`.github/`
   等不会进入源码分发包的目标用 GitHub 绝对 URL。
3. **图片**：一律放 `docs/images/`。根目录的 `assets/` 已并入这里。
4. **同一 PR 更新**：行为变更必须同时更新受影响的文档与 `CHANGELOG.md`。
   只改文档的 PR 不需要改代码，但需要保证链接可点。
5. **不复制**：配置键、MCP 契约、宿主限制在技能 references 中有权威版本，
   `docs/` 只做索引和解释，避免两处维护。
6. **新增文档**：先读 [docs/development/docs.md](development/docs.md) 的归属规则，
   再决定放在 `development/`、`architecture/`、`reference/` 还是 `design/`。
