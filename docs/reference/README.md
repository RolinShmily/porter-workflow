# 参考

查找型文档。这里描述稳定的外部接口；实现细节在代码与
[architecture/](../architecture/README.md) 中。

| 页面 | 内容 |
| --- | --- |
| [cli.md](cli.md) | 命令树、全局选项、退出码、`--json` 约定 |
| [api.md](api.md) | `from porter import ...` 稳定 API 与最小用法 |
| [configuration.md](configuration.md) | 配置优先级、环境变量、`porter config` |

## 权威归属

| 主题 | 权威位置 |
| --- | --- |
| 每个命令的完整参数 | `porter <cmd> --help` |
| 公开符号 | `src/porter/__init__.py` 的 `__all__` |
| 配置键全表与默认值 | [`skills/porter-skill/references/CONFIG.md`](../../skills/porter-skill/references/CONFIG.md) |
| MCP 工具与资源 | [`skills/porter-skill/references/MCP.md`](../../skills/porter-skill/references/MCP.md) |
| 受限宿主 | [`skills/porter-skill/references/HOSTS.md`](../../skills/porter-skill/references/HOSTS.md) |

文档与 `--help` 冲突时，以 `--help` 为准并修文档。
