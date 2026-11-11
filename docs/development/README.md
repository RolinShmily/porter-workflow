# 开发

面向要修改 porter 的贡献者。按顺序读：

1. [环境搭建](setup.md) —— Python、uv、FFmpeg、Deno/Node，以及锁定文件规则。
2. [门禁](gate.md) —— 每次改动必须通过的四个命令、测试分层、架构与许可证规则。
3. [扩展点](extending.md) —— 新增平台、ASR 后端或翻译后端的步骤。
4. [发布](release.md) —— 版本来源、GitHub Release、PyPI 与 EXE 启动器。
5. [文档维护](docs.md) —— `docs/` 的信息架构、语言与链接规则。

## 快速开始

```bash
git clone https://github.com/RolinShmily/porter-workflow
cd porter-workflow
uv sync --extra all --extra dev
```

改完代码后：

```bash
uv run ruff check src tests scripts
uv run mypy src scripts
uv run lint-imports
uv run pytest -m "not network"
```

## 代码放在哪里

| 改动 | 位置 |
| --- | --- |
| 业务逻辑、阶段、后端 | `src/porter/core/` |
| CLI 参数、终端输出 | `src/porter/cli/` |
| MCP 工具与协议适配 | `src/porter/mcp/` |
| Gradio 工作台 | `src/porter/webui/` |
| 发布辅助脚本 | `scripts/` |
| Agent 技能 | `skills/porter-skill/` |
| 人类文档 | `docs/` |

引擎与前端之间是单向依赖：前端可以 import 引擎，引擎不能 import 前端。
详见 [架构总览](../architecture/overview.md) 和 [分层](../architecture/layers.md)。

## 提交约定

- 分支从 `main` 切出，名字描述改动：`fix/burn-truncation`、`feat/rumble-platform`。
- 提交信息用 [Conventional Commits](https://www.conventionalcommits.org/)：
  `feat:`、`fix:`、`docs:`、`refactor:`、`test:`、`chore:`、`perf:`、`build:`、`ci:`。
  中英文均可。
- 行为变更必须带测试，并在同一个 PR 里更新文档与 `CHANGELOG.md`。
- 提交前先跑门禁；CI 会在 Python 3.11、3.12、3.13 上重复这些检查。

## 安全与隐私

- 不要把 API Key、cookie、session token 贴进 issue、日志或测试夹具。
- `porter config list` 会对密钥打码，报告问题时用它，而不是原始配置文件。
- 发现漏洞不要开公开 issue，按 [SECURITY.md](../../SECURITY.md) 私下报告。
