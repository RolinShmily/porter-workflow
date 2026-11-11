# 设计

本目录记录**约束**，不是实现说明。约束回答“哪些事情即使能做也不该做”，
实现可以变，约束不应随便变。

| 页面 | 内容 |
| --- | --- |
| [principles.md](principles.md) | 不可妥协的设计约束 |
| [webui.md](webui.md) | Gradio 工作台的设计基线 |

具体结构与数据流见 [../architecture/](../architecture/README.md)。

## 待改进

以下问题已在结构梳理中确认，但按要求留作独立改动，避免与目录迁移混在一起：

1. **前端业务逻辑边界**：CLI/MCP/WebUI 各自承担了一部分作业展示逻辑；
   如果出现第三处重复，再抽出 frontend-neutral 的 job presentation/service
   层，而不是现在过度设计。
2. **多发行包**：如果三个前端将来需要独立版本/独立发布，再拆成
   `porter-core`、`porter-cli`、`porter-mcp`、`porter-webui` 四个 distribution，
   共享 PEP 420 `porter` namespace，并用 `porter-workflow` 作为 meta 包。
   当前同版本、同发布节奏，不需要承担这个成本。

已完成的加固项（WebUI 拆分、`core.refine`/`core.mirrors` 入层、前端独立性契约）
记录在 [architecture/layers.md](../architecture/layers.md) 和
[webui.md](webui.md)。
